"""Endless, gap-free logging over loop mode (HW_BASE_MODE 6, DSWave's Data Acquisition mode).

What the hardware does (measured 2026-11-01/02, docs/PROTOCOL.md section 8.1):
  * after the head the device streams plain sample blocks until it is stopped; nothing in the stream marks time,
    so a loss could only be seen in the signal;
  * the 512 MiB sample memory is a ring: the host may fall behind by up to ~512 MiB without loss; past that the
    device overwrites unread data without any flag and the block alignment is lost for the rest of the run;
  * readout while acquiring: 196-266 MB/s depending on the channel set; 150 MB/s ran 3 min (27 GB) with a constant
    host lag;
  * fold 0: one rate for every channel, at most 100 MSa/s each.

So the host decides everything. plan() checks that the rate is sustainable (above the ceiling a run is allowed only
when it is short enough for the ring to absorb the excess), works out how long the log can run and where the data
goes. run() streams with the engine keeping nothing in memory: the USB loop only counts and queues the raw transfers,
a writer thread decodes them, prints the live statistics and keeps or writes the samples, and the backlog (data the
device has acquired but the host has not read) is watched so a run stops well before the ring could overwrite data.

Storage (most logs are short: RAM is the default, an SSD written at 100+ MB/s for hours wears out):
  'ram'  (default) the samples stay in RAM within the budget (state/host.json allowance, else min(4 GB, a quarter of
         the free RAM)) and are written once, at the end, as <stem>.npz. A log longer than the budget is refused
         unless keep_last (or 'forever'): then the RAM is a ring and the newest budget's worth is kept.
  'disk' a directory <stem>/ with chunk_00000.npz ... and index.json, dumped while running (for records that must be
         kept in full); chunk k starts at t0 = k x chunk_s.
  'none' statistics only.
Every .npz is a normal capture file (load_capture / analyze / plot); t = 0 is the first sample of the log.
"""
import collections
import json
import math
import os
import queue
import shutil
import threading
import time
import zipfile

import numpy as np

from . import packet
from .errors import DscopeError

RING_BYTES = 512 * 2 ** 20           # device sample memory = loop ring
BACKLOG_STOP = 384 * 2 ** 20         # stop a run once the device is this far ahead (the ring overwrites at ~512 MiB)
SUSTAINED_BPS = 150e6                # verified: 3 min, 27 GB, host lag constant
CEILING_BPS = 190e6                  # planning ceiling; the readout while acquiring measured 196-266 MB/s
MAX_RATE = 100_000_000               # fold 0: no interleaving in loop mode
LOOP_DEPTH = 400_000                 # :STORAGEdepth sent with the loop program (DSWave keeps the last OSC value)
CHUNK_TARGET_BYTES = 256e6           # default size of one dumped chunk file
QUEUE_LIMIT_BYTES = 768 * 2 ** 20    # raw data waiting for the writer thread; more means disk or CPU are too slow
PIECE_BYTES = 32 * 2 ** 20           # the USB loop hands the writer pieces of at most this (statistics lines are
                                     # assembled from pieces, so --every does not size the queue)
CHUNK_MAX_BYTES = 2 ** 30            # one disk chunk is collected in RAM before it is written
CLOCK_TOL = 200e-6                   # scope vs PC clock: backlog growth below this is not counted (a 30 ppm slower
                                     # scope clock would otherwise look like a growing backlog after ~a day)
RAM_BUDGET_MAX = 4 * 2 ** 30         # default RAM budget: min(this, a quarter of the free memory)
DISK_WEAR_BYTES = 100e9              # warn when a disk log writes more than this (or this per day when unbounded)
STORAGES = ('ram', 'disk', 'none')


def available_ram():
    """Free physical memory in bytes (best effort; 8 GiB when it cannot be read)."""
    try:
        if os.name == 'nt':
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                            ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                            ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                            ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                            ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]
            m = MS()
            m.dwLength = ctypes.sizeof(MS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                return int(m.ullAvailPhys)
        return int(os.sysconf('SC_AVPHYS_PAGES') * os.sysconf('SC_PAGE_SIZE'))
    except (OSError, ValueError, AttributeError):
        return 8 * 2 ** 30


def default_ram_budget():
    """The owner's allowance (dscope bench --ram), capped by 80 % of the free RAM; else min(4 GB, free / 4)."""
    from . import host
    free = available_ram()
    allowed = host.allow().get('ram_bytes')
    if allowed:
        return int(min(allowed, 0.8 * free))
    return int(min(RAM_BUDGET_MAX, free // 4))


def loop_hal(st, rate):
    """HAL parameters of a loop run: the front-end settings of `st`, trigger off, one explicit rate."""
    from .scope import hal_config
    import copy
    s = copy.copy(st)
    s.duration, s.rate, s.trig_source, s.trig_mode = 1.0, None, None, 'force'
    hal, scale, warnings, _ = hal_config(s)
    hal['trigger']['enable'] = 0
    hal.update(loop=1, rate=int(rate), auto_rate=0, storage_depth=LOOP_DEPTH, sample_time_ps=10 ** 12)
    return hal, scale, [w for w in warnings if 'samples' not in w and 'window' not in w]


def plan(st, rate, duration=None, every=1.0, out_dir=None, storage='ram', ram_budget=None, chunk_s=None,
         zero_info=None, force=False, keep_last=False, disk_budget=None):
    """Offline plan of a loop log. duration None = until stopped. Raises INVALID_ARG for what cannot work.
    force: run above the planning ceiling for longer than burst_max_s anyway (the measured readout is 196-266 MB/s
    depending on the channel set); the backlog guard still stops the run before data could be lost.
    keep_last: a RAM log longer than the budget keeps the newest budget's worth (ring) instead of being refused.
    disk_budget: bytes a disk log may use (default: the owner's allowance in state/host.json, else 90 % of free)."""
    from .scope import RATES, _cached_zero
    from . import mso_adapter, host
    na, nd = len(st.analog), len(st.digital)
    if not na and not nd:
        raise DscopeError('INVALID_ARG', 'no channels enabled')
    if storage not in STORAGES:
        raise DscopeError('INVALID_ARG', '--storage must be one of %s' % ', '.join(STORAGES))
    if not rate or rate <= 0:
        raise DscopeError('INVALID_ARG', '--rate must be > 0')
    warnings = []
    if rate > MAX_RATE * (1 + 1e-9):
        raise DscopeError('INVALID_ARG', 'log samples every channel at one rate of at most 100 MSa/s (loop mode has no '
                          'interleaving)', 'use capture/stream for 200/400 MSa/s on 1-2 channels')
    r = max([x for x in RATES if x <= rate * (1 + 1e-9)], default=None)
    if r is None:
        raise DscopeError('INVALID_ARG', 'the lowest log rate is %d Sa/s' % RATES[0])
    if abs(r - rate) > 1e-6 * rate:
        warnings.append('rate set to %g Sa/s: only the 1-2-5 rates are verified dividers (a nominal 33.3 MSa/s '
                        'samples at 25 MSa/s)' % r)
    if duration is not None and duration <= 0:
        raise DscopeError('INVALID_ARG', '--duration must be > 0 (or "forever")')
    if every <= 0:
        raise DscopeError('INVALID_ARG', '--every must be > 0')
    hal, scale, fe_warnings = loop_hal(st, r)
    warnings += fe_warnings
    prog, geom = mso_adapter.compile(hal, zero_info if zero_info is not None else _cached_zero())
    blk = geom['blk_bytes']
    small = bool(geom['small_pkt'])
    data_bps = r * (na + nd / 8.0)                       # device bytes per second = bytes kept per second
    usb_bps = data_bps * (packet.HEAD_BYTES / blk) if small else data_bps
    block_s = packet.BLOCK_SAMPLES / r
    every_blocks = max(1, int(round(every / block_s)))
    every = every_blocks * block_s
    pkt_s = (geom['pkt_bytes'] / usb_bps) if not small else block_s
    hb = host.load().get('bench', {})

    if data_bps <= SUSTAINED_BPS:
        verdict = 'sustained'
    elif data_bps <= CEILING_BPS:
        verdict = 'near_limit'
        warnings.append('%.0f MB/s is near the readout limit (verified sustained: 150 MB/s); the backlog is watched '
                        'and the run stops before data could be lost' % (data_bps / 1e6))
    else:
        verdict = 'burst'
    # a burst of T s ends when the host has read data_bps x T at the ceiling R, at t = data_bps x T / R; the backlog
    # then is (data_bps - R) x data_bps x T / R, which must stay below BACKLOG_STOP
    burst_s = BACKLOG_STOP * CEILING_BPS / (data_bps * (data_bps - CEILING_BPS)) if verdict == 'burst' else None
    if burst_s is not None and (duration is None or duration > burst_s) and not force:
        raise DscopeError('INVALID_ARG', '%.0f MB/s is above what the scope sustains (~190 MB/s while acquiring): the '
                          '512 MiB ring absorbs the excess for about %.1f s only' % (data_bps / 1e6, burst_s),
                          'use a lower --rate or fewer channels, --duration <= %.1fs, or --force (the run stops '
                          'with HOST_TOO_SLOW if the scope gets 384 MiB ahead)' % burst_s)
    if burst_s is not None:
        warnings.append('burst: above the sustained rate the ring absorbs the excess for ~%.1f s at the planning '
                        'ceiling of 190 MB/s%s' % (burst_s, '; --force: the backlog guard ends the run if the host '
                                                  'falls 384 MiB behind' if force else ''))
    if hb.get('decode_mb_s') and data_bps > 0.8e6 * hb['decode_mb_s'] and storage != 'none':
        warnings.append('%.0f MB/s is close to what this PC decodes (%.0f MB/s, dscope bench): the run may stop with '
                        'HOST_TOO_SLOW' % (data_bps / 1e6, hb['decode_mb_s']))

    if ram_budget is None:
        ram_budget = default_ram_budget()
    total = None if duration is None else data_bps * duration
    ram_window = ram_budget / data_bps
    ring = False
    disk_bytes = None
    file_bytes = None
    max_s = burst_s
    disk_free = None
    d = out_dir or '.'
    if storage == 'ram':
        if total is None or total > ram_budget:
            if total is not None and not keep_last:
                raise DscopeError('INVALID_ARG', '%.2f GB (%.0f s at %.1f MB/s) does not fit the RAM budget of %.2f GB'
                                  % (total / 1e9, duration, data_bps / 1e6, ram_budget / 1e9),
                                  '--keep-last keeps the newest %.0f s; --storage disk writes everything (it wears an SSD: '
                                  'only for records needed in full); --ram raises the budget; or a lower --rate / '
                                  'fewer channels' % ram_window)
            ring = True
            warnings.append('RAM ring: keeps the newest %.0f s (%.2f GB budget), written once at the end'
                            % (ram_window, ram_budget / 1e9))
        file_bytes = ram_budget if ring else total
        if out_dir is not None:
            os.makedirs(d, exist_ok=True)
            disk_free = shutil.disk_usage(d).free
            if file_bytes > 0.9 * disk_free:
                raise DscopeError('INVALID_ARG', 'not enough disk space in %s for the final file: %.2f GB free, %.2f GB '
                                  'needed' % (d, disk_free / 1e9, file_bytes / 1e9),
                                  'free space, another --out, a smaller --ram or a lower --rate')
    elif storage == 'disk':
        os.makedirs(d, exist_ok=True)
        disk_free = shutil.disk_usage(d).free
        allowed = disk_budget if disk_budget is not None else host.allow().get('disk_bytes')
        budget = min(0.9 * disk_free, allowed) if allowed else 0.9 * disk_free
        disk_bytes = budget
        disk_s = budget / data_bps
        max_s = disk_s if max_s is None else min(max_s, disk_s)
        if duration is not None and duration > disk_s:
            raise DscopeError('INVALID_ARG', 'not enough disk allowance in %s: %.1f GB hold %.0f s at %.1f MB/s'
                              % (d, budget / 1e9, disk_s, data_bps / 1e6),
                              'use RAM (--storage ram, --keep-last), a lower --rate, fewer channels, or raise the '
                              'allowance with dscope bench --disk')
        per_day = data_bps * 86400
        if (total if total is not None else per_day) > DISK_WEAR_BYTES:
            warnings.append('disk log writes %s to %s (%.0f GB per hour); SSDs are rated for a few hundred TB written: '
                            'use RAM unless the whole record is needed' % (
                                '%.0f GB' % (total / 1e9) if total is not None else 'until stopped', d,
                                data_bps * 3600 / 1e9))
        if hb.get('disk_write_mb_s') and hb.get('disk_dir') and os.path.abspath(hb['disk_dir']) == os.path.abspath(d) \
                and data_bps > 0.7e6 * hb['disk_write_mb_s']:
            warnings.append('%.0f MB/s is close to the measured write speed of %s (%.0f MB/s)'
                            % (data_bps / 1e6, d, hb['disk_write_mb_s']))
    if chunk_s is None:
        chunk_s = min(max(CHUNK_TARGET_BYTES / data_bps, every), 3600.0)
    chunk_blocks = max(every_blocks, int(round(chunk_s / every)) * every_blocks)
    if storage == 'disk' and chunk_blocks * blk > CHUNK_MAX_BYTES:
        chunk_blocks = max(1, CHUNK_MAX_BYTES // blk)
        warnings.append('chunk limited to %.0f MB (%.3g s): a chunk is collected in RAM before it is written'
                        % (chunk_blocks * blk / 1e6, chunk_blocks * block_s))
    chunk_s = chunk_blocks * block_s
    if storage == 'ram' and not ring and duration is not None:
        max_s = duration if max_s is None else min(max_s, duration)
    pub = {
        'rate_hz': r, 'channels': list(st.analog) + list(st.digital), 'data_mb_s': round(data_bps / 1e6, 3),
        'usb_mb_s': round(usb_bps / 1e6, 3), 'verdict': verdict,
        'sustained_mb_s': SUSTAINED_BPS / 1e6, 'ceiling_mb_s': CEILING_BPS / 1e6,
        'duration_s': duration, 'burst_max_s': None if burst_s is None else round(burst_s, 2),
        'ring_s': round(RING_BYTES / data_bps, 3), 'storage': storage,
        'ram_budget_mb': round(ram_budget / 1e6), 'ram_window_s': round(ram_window, 3), 'ram_ring': ring,
        'file_mb': None if file_bytes is None else round(file_bytes / 1e6, 1),
        'total_mb': None if total is None else round(total / 1e6, 1),
        'chunk_s': round(chunk_s, 6) if storage == 'disk' else None,
        'chunk_mb': round(chunk_blocks * blk / 1e6, 1) if storage == 'disk' else None,
        'chunks': (math.ceil(duration / chunk_s) if duration else None) if storage == 'disk' else None,
        'disk_free_gb': None if disk_free is None else round(disk_free / 1e9, 1),
        'max_duration_s': None if max_s is None else round(max_s, 1), 'every_s': round(every, 6),
        'small_packets': small, 'packet_bytes': geom['pkt_bytes'], 'transfers': geom['transfer_count'],
        'warnings': warnings}
    return dict(pub, _hal=hal, _scale=scale, _prog=prog, _geom=geom, _every_blocks=every_blocks,
                _chunk_blocks=chunk_blocks, _data_bps=data_bps, _pkt_s=pkt_s, _ram_budget=ram_budget,
                _disk_bytes=disk_bytes)


def public(pl):
    return {k: v for k, v in pl.items() if not k.startswith('_')}


def write_npz(path, meta, members):
    """np.savez-compatible file written member by member from lists of uint8 parts (no concatenated copy).
    members: [(name, [parts...]), ...]."""
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED, allowZip64=True) as zf:
        for name, parts in [('meta', [np.frombuffer(json.dumps(meta).encode(), dtype=np.uint8)])] + list(members):
            n = sum(int(p.size) for p in parts)
            with zf.open(name + '.npy', 'w', force_zip64=True) as f:
                np.lib.format.write_array_header_2_0(f, {'descr': '|u1', 'fortran_order': False, 'shape': (n,)})
                for p in parts:
                    f.write(memoryview(np.ascontiguousarray(p, dtype=np.uint8)))
    return path


class _Writer(threading.Thread):
    """Decodes queued raw blocks, prints a statistics line per block, keeps (RAM) or dumps (disk) the samples."""

    def __init__(self, layout, rate, scale, storage, chunk_blocks, stem, meta_base, emit, heartbeat,
                 ram_budget=None, ring=False, every_blocks=1):
        super().__init__(daemon=True)
        self.layout, self.rate, self.scale = layout, rate, scale
        self.storage, self.chunk_blocks, self.stem = storage, chunk_blocks, stem
        self.meta_base, self.emit, self.heartbeat = meta_base, emit, heartbeat
        self.ram_budget, self.ring = ram_budget, ring
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.queued = 0
        self.error = None
        self.carry = b''
        self.blocks = 0                   # blocks processed
        self.line = 0
        self.an = ['A%d' % i for i in layout.analog]
        self.dg = ['D%d' % i for i in layout.digital]
        self.every_blocks = every_blocks
        self.lacc, self.line_blocks, self.line_info = {}, 0, {}    # the statistics line being assembled
        self.acc = {c: {'min': 255, 'max': 0, 'sum': 0, 'n': 0} for c in self.an}
        self.dacc = {c: {'ones': 0, 'edges': 0, 'n': 0, 'last': None} for c in self.dg}
        self.cur = {c: [] for c in self.an + self.dg}
        self.cur_blocks = 0
        self.pieces = collections.deque()  # RAM: (blocks, nbytes, {channel: array})
        self.kept_bytes = 0
        self.stored_blocks = 0             # RAM: blocks appended so far
        self.dropped_blocks = 0            # RAM ring: blocks overwritten by newer data
        self.chunk = 0
        self.chunk_t0 = 0.0
        self.files = []
        self.index = None
        if storage == 'disk':
            os.makedirs(stem, exist_ok=True)
            self.index = {'version': 1, 'cmd': 'log', 'status': 'running', 'meta': meta_base, 'chunks': []}
            self._write_index()

    # -------------------------------------------------------------- producer side (USB loop thread)
    def put(self, bufs, n, info):
        with self.lock:
            self.queued += n
        self.q.put((bufs, n, info))

    def finish(self, info=None):
        """Stop the thread, print the last (partial) statistics line and close the files. After a processing error
        the data kept so far is still written and the index records the error."""
        self.q.put(None)
        self.join()
        info = dict(info or {})
        if self.error is not None:
            info.update(status='error', stop_reason=info.get('stop_reason') or 'writer_error')
        try:
            if self.error is None:
                self._emit_line(final=True)
            self._close(info)
        except Exception as e:                       # noqa: BLE001 - reported, the data so far is kept
            self.error = self.error or e
        return self.error

    # -------------------------------------------------------------- consumer side
    def run(self):
        while True:
            item = self.q.get()
            if item is None:
                return
            bufs, n, info = item
            try:
                if self.error is None:
                    self._process(bufs, info)
            except Exception as e:                   # noqa: BLE001 - stops the run (stop_flag sees it)
                self.error = e
            finally:
                with self.lock:
                    self.queued -= n

    def _process(self, bufs, info):
        L = self.layout
        bb = L.block_bytes
        raw = self.carry + b''.join(bufs)
        nb = len(raw) // bb
        self.carry = raw[nb * bb:]
        if nb == 0:
            return
        B = np.frombuffer(raw, np.uint8, count=nb * bb).reshape(nb, bb)
        first = self.blocks == 0
        an, dg = packet._decode_blocks(B, L, packed=True)
        out = {}
        for i, c in zip(L.analog, self.an):
            out[c] = an[i]
        for i, c in zip(L.digital, self.dg):
            p = dg[i]                                       # packed: np.packbits(bits, bitorder='little')
            if first and L.digital_stale:
                p = p.copy()                                # may be a view of the read-only USB bytes
                bits = np.unpackbits(p[:8], bitorder='little')
                bits[:L.digital_stale] = bits[L.digital_stale]   # stale level from the previous run
                p[:8] = np.packbits(bits, bitorder='little')
            out[c] = p
        per = {c: x.size // nb for c, x in out.items()}              # array elements per block
        if self.storage == 'ram':
            nbytes = sum(int(x.size) for x in out.values())
            self.pieces.append((nb, nbytes, out))
            self.kept_bytes += nbytes
            self.stored_blocks += nb
            while self.ring and self.kept_bytes > self.ram_budget:
                b, nbytes0, arrs = self.pieces[0]           # the RAM ring drops its oldest samples
                per_block = nbytes0 // b
                drop = min(b, -(-(self.kept_bytes - self.ram_budget) // per_block))
                if drop >= b:
                    self.pieces.popleft()
                else:
                    self.pieces[0] = (b - drop, nbytes0 - drop * per_block,
                                      {c: x[drop * (x.size // b):] for c, x in arrs.items()})
                self.kept_bytes -= drop * per_block
                self.dropped_blocks += drop
        elif self.storage == 'disk':          # cut at exact chunk boundaries: chunk k starts at k x chunk_s
            b = 0
            while b < nb:
                take = min(nb - b, self.chunk_blocks - self.cur_blocks)
                for c, x in out.items():
                    self.cur[c].append(x[b * per[c]:(b + take) * per[c]])
                self.cur_blocks += take
                b += take
                if self.cur_blocks >= self.chunk_blocks:
                    self._save_chunk()
                    self.lacc['_chunk_saved'] = self.chunk - 1
        self.line_info = info
        b = 0
        while b < nb:                         # statistics, cut at exact line boundaries (every_s)
            take = min(nb - b, self.every_blocks - self.line_blocks)
            self._stats({c: x[b * per[c]:(b + take) * per[c]] for c, x in out.items()}, take)
            b += take
            if self.line_blocks >= self.every_blocks:
                self._emit_line()
        if self.heartbeat:
            self.heartbeat()

    def _stats(self, seg, nb):
        n_s = nb * packet.BLOCK_SAMPLES
        for c in self.an:
            x = seg[c]
            mn, mx, sm = int(x.min()), int(x.max()), int(x.sum(dtype=np.uint64))
            a = self.acc[c]
            a['min'], a['max'], a['sum'], a['n'] = min(a['min'], mn), max(a['max'], mx), a['sum'] + sm, a['n'] + x.size
            la = self.lacc.setdefault(c, [255, 0, 0, 0])
            la[0], la[1], la[2], la[3] = min(la[0], mn), max(la[1], mx), la[2] + sm, la[3] + x.size
        for c in self.dg:
            d = self.dacc[c]
            ones, edges, d['last'] = packet.bit_stats(seg[c], d['last'])
            d['ones'] += ones
            d['edges'] += edges
            d['n'] += n_s
            la = self.lacc.setdefault(c, [0, 0, 0])
            la[0], la[1], la[2] = la[0] + ones, la[1] + edges, la[2] + n_s
        self.blocks += nb
        self.line_blocks += nb

    def _emit_line(self, extra=None, final=False):
        if not self.line_blocks:
            return
        info = self.line_info
        line = {'i': self.line, 't_signal_s': round(self.blocks * packet.BLOCK_SAMPLES / self.rate, 6),
                't_wall_s': round(info.get('t_wall', 0.0), 3), 'backlog_mb': round(info.get('backlog', 0) / 1e6, 2)}
        for c in self.an:
            a = self.lacc.get(c)
            if a:
                line[c] = {'vmin_v': self._v(c, a[0]), 'vmax_v': self._v(c, a[1]), 'vmean_v': self._v(c, a[2] / a[3])}
        for c in self.dg:
            a = self.lacc.get(c)
            if a:
                line[c] = {'high_frac': round(a[0] / a[2], 4), 'edges': a[1]}
        if '_chunk_saved' in self.lacc:
            line['chunk_saved'] = self.lacc['_chunk_saved']
        if self.ring:
            line['ram_kept_s'] = round((min(self.blocks, self.stored_blocks) - self.dropped_blocks)
                                       * packet.BLOCK_SAMPLES / self.rate, 3)
        line.update(extra or {})
        if final:
            line['final'] = True
        self.lacc, self.line_blocks = {}, 0
        self.line += 1
        if self.emit:
            self.emit(line)

    def _v(self, c, code):
        s = self.scale[c]
        return round((code - s['zero_code']) * s['volts_per_code'] * s['probe'], 4)

    def _save(self, path, t0, parts, nblocks, info=None):
        n = nblocks * packet.BLOCK_SAMPLES
        meta = json.loads(json.dumps(self.meta_base))
        meta['acq'] = dict(meta['acq'], samples=n, duration_s=n / self.rate, t_start_s=t0, **(info or {}))
        meta['t0_analog_s'] = meta['t0_digital_s'] = t0
        meta['digital_lengths'] = {c: n for c in self.dg}
        members = [('analog_' + c, parts[c]) for c in self.an] + [('digital_' + c, parts[c]) for c in self.dg]
        write_npz(path, meta, members)
        return path, n

    def _save_chunk(self):
        name = 'chunk_%05d.npz' % self.chunk
        _, n = self._save(os.path.join(self.stem, name), self.chunk_t0, self.cur, self.cur_blocks,
                          {'chunk': self.chunk})
        self.cur = {c: [] for c in self.cur}
        self.cur_blocks = 0
        self.index['chunks'].append({'file': name, 't0_s': self.chunk_t0, 'samples': n})
        self.files.append(os.path.join(self.stem, name))
        self.chunk += 1
        self.chunk_t0 += n / self.rate
        self._write_index()

    def _write_index(self):
        tmp = os.path.join(self.stem, 'index.json.tmp')
        with open(tmp, 'w') as f:
            json.dump(self.index, f, indent=1)
        os.replace(tmp, os.path.join(self.stem, 'index.json'))

    def kept_t0(self):
        return self.dropped_blocks * packet.BLOCK_SAMPLES / self.rate

    def _close(self, info):
        if self.storage == 'ram' and self.pieces and self.stem:
            parts = {c: [p[2][c] for p in self.pieces] for c in self.an + self.dg}
            nblocks = sum(p[0] for p in self.pieces)
            extra = {'ram_ring': True, 'ram_dropped_s': round(self.kept_t0(), 6)} if self.ring else {}
            path, _ = self._save(self.stem + '.npz', self.kept_t0(), parts, nblocks, extra)
            self.files.append(path)
        elif self.storage == 'disk':
            if self.cur_blocks:
                self._save_chunk()
            self.index.update(status=info.get('status', 'done'), stop_reason=info.get('stop_reason'),
                              samples=self.blocks * packet.BLOCK_SAMPLES)
            self._write_index()

    def summary(self):
        out = {'analog': {}, 'digital': {}}
        for c, a in self.acc.items():
            if a['n']:
                out['analog'][c] = {'vmin_v': self._v(c, a['min']), 'vmax_v': self._v(c, a['max']),
                                    'vmean_v': self._v(c, a['sum'] / a['n'])}
        for c, d in self.dacc.items():
            if d['n']:
                out['digital'][c] = {'high_frac': round(d['ones'] / d['n'], 4), 'edges': d['edges'],
                                     'state': 'toggling' if d['edges'] else ('high' if d['ones'] else 'low')}
        return out


STOP_ERRORS = {
    'backlog': ('HOST_TOO_SLOW', 'the host fell behind the scope by %.0f MB; the run was stopped before the 512 MiB ring '
                                 'could overwrite unread data', 'use a lower --rate or fewer channels'),
    'host_slow': ('HOST_TOO_SLOW', 'decoding / writing fell behind (%.0f MB queued); the run was stopped',
                  'use RAM storage, a faster disk, a lower --rate or fewer channels'),
    'stalled': ('TIMEOUT_DATA', 'no data from the scope for %.1f s', 'run "dscope release"; if it persists replug the scope'),
}


def run(scope, st, pl, out_stem=None, emit=None, stop_flag=None, heartbeat=None, on_final=None):
    """Run a planned log on an open Scope. Returns the result document (ok False with an error on a data-safety
    stop; the data up to the stop is kept). Device errors raise DscopeError after the files were closed.
    out_stem None: nothing is written (RAM data is dropped at the end). on_final(nbytes) is called before the
    final RAM file is written (the CLI extends its watchdog for the write)."""
    geom = dict(pl['_geom'])
    rate = pl['rate_hz']
    layout = packet.layout_from_regs(geom['_regs'], rates={'analog': rate, 'digital': rate})
    small = bool(geom['small_pkt'])
    blk = layout.block_bytes
    if small:
        geom['transfer_count'] = max(4, geom['transfer_count'])   # DSWave queues 1 read of one block
    data_bps = pl['_data_bps']
    piece_bytes = max(blk, min(pl['_every_blocks'] * blk, PIECE_BYTES // blk * blk))
    disk_limit = pl.get('_disk_bytes') if pl['storage'] == 'disk' else None
    target = None if pl['duration_s'] is None else int(math.ceil(pl['duration_s'] * rate / packet.BLOCK_SAMPLES)) * blk
    stall_s = max(2.0, 50 * pl['_pkt_s'])
    storage = pl['storage']
    if storage == 'disk' and not out_stem:
        raise DscopeError('INVALID_ARG', 'a disk log needs an output folder')
    stem = out_stem if storage in ('ram', 'disk') else None
    t0_wall = time.time()
    applied = scope_applied(st, pl)
    meta_base = {'version': 1, 'device': 'DSCope M454', 'time': time.strftime('%Y-%m-%dT%H:%M:%S'),
                 'applied': applied, 'channels': {c: pl['_scale'][c] for c in st.analog},
                 'analog_rate_hz': rate if st.analog else None, 'digital_rate_hz': rate if st.digital else None,
                 'acq': {'mode': 'loop', 'analog_rate_hz': rate if st.analog else None,
                         'digital_rate_hz': rate if st.digital else None, 'triggered': False},
                 'trigger': {'source': None, 'mode': 'force'}, 'warnings': list(pl['warnings'])}
    w = _Writer(layout, rate, pl['_scale'], storage, pl['_chunk_blocks'], stem, meta_base, emit, heartbeat,
                pl['_ram_budget'], pl['ram_ring'], pl['_every_blocks'])
    w.start()
    s = {'head': False, 't_head': None, 't_last': None, 'n_usb': 0, 'n_data': 0, 'pend': [], 'pend_n': 0,
         'reason': None, 'backlog': 0, 'backlog_max': 0, 'queued_at_stop': 0}

    def flush(limit=None):
        bufs, n = s['pend'], s['pend_n']
        if limit is not None and n > limit:          # trim the end of the log to the requested duration
            keep, out = limit, []
            for b in bufs:
                if keep <= 0:
                    break
                out.append(b[:keep])
                keep -= len(out[-1])
            bufs, n = out, limit
        s['pend'], s['pend_n'] = [], 0
        if n:
            w.put(bufs, n, {'t_wall': time.time() - t0_wall, 'backlog': s['backlog']})

    def on_chunk(buf):
        now = time.monotonic()
        if not s['head']:
            s['head'], s['t_head'], s['t_last'] = True, now, now
            return
        s['n_usb'] += len(buf)
        d = buf[:blk] if small else buf             # small packets: one block, then filler, per 4 KiB read
        s['n_data'] += len(d)
        s['t_last'] = now
        s['pend'].append(d)
        s['pend_n'] += len(d)
        if s['pend_n'] >= piece_bytes and (target is None or s['n_data'] < target):
            flush()                                  # the block that reaches the target is trimmed at the end

    def stop():
        if w.error is not None:
            s['reason'] = 'writer_error'
            return True
        if stop_flag is not None and stop_flag():
            s['reason'] = 'stop_request'
            return True
        if not s['head']:
            return False
        now = time.monotonic()
        if target is not None and s['n_data'] >= target:
            s['reason'] = 'duration'
            return True
        if disk_limit is not None and s['n_data'] >= disk_limit:
            s['reason'] = 'disk_allowance'
            return True
        s['backlog'] = max(0.0, data_bps * (1.0 - CLOCK_TOL) * (now - s['t_head']) - s['n_data'])
        s['backlog_max'] = max(s['backlog_max'], s['backlog'])
        if s['backlog'] > BACKLOG_STOP:
            s['reason'] = 'backlog'
            return True
        if now - s['t_last'] > stall_s:
            s['reason'] = 'stalled'
            return True
        if w.queued > QUEUE_LIMIT_BYTES:
            s['reason'] = 'host_slow'
            s['queued_at_stop'] = w.queued
            return True
        return False

    raw = None
    err = None
    try:
        raw = scope._run(pl['_prog'], geom, 3.0, None, on_chunk=on_chunk, stop_flag=stop, poll=False, keep_data=False)
    except BaseException as e:                       # device error: close the files first, then re-raise
        err = e
    already = sum(len(b) for b in s['pend'])
    sent = s['n_data'] - already
    flush(None if target is None else max(0, target - sent))
    if isinstance(err, KeyboardInterrupt):
        status, reason = 'interrupted', 'interrupted'
    else:
        status = 'error' if err is not None or s['reason'] in STOP_ERRORS or s['reason'] == 'writer_error' else 'done'
        reason = s['reason'] or ('device_error' if err else None)
    if on_final is not None and storage == 'ram' and stem:
        on_final(w.kept_bytes + w.queued)
    werr = w.finish({'status': status, 'stop_reason': reason})
    if err is not None:
        try:
            err.log_files = _files(storage, w, stem, rate)
        except Exception:                            # noqa: BLE001 - only an annotation
            pass
        raise err
    if (raw is None or raw.head is None) and s['reason'] == 'stop_request':
        return {'ok': True, 'cmd': 'log', 'plan': public(pl), 'files': _files(storage, w, stem, rate),
                'acq': {'mode': 'loop', 'samples': 0, 'stop_reason': 'stop_request'},
                'note': 'stopped on request before any data arrived'}
    if raw is None or raw.head is None:
        raise DscopeError('PROTOCOL_ERROR', 'no data from the scope (no head within 3 s)',
                          'run "dscope release"; if it persists replug the scope')
    n_samples = w.blocks * packet.BLOCK_SAMPLES
    span = (s['t_last'] - s['t_head']) if s['t_head'] and s['t_last'] else 0.0
    acq = {'mode': 'loop', 'rate_hz': rate, 'samples': n_samples, 'duration_s': round(n_samples / rate, 6),
           'data_mb': round(s['n_data'] / 1e6, 3), 'usb_mb': round(s['n_usb'] / 1e6, 3),
           'host_mb_s': round(s['n_usb'] / span / 1e6, 3) if span > 0 else None,
           'backlog_max_mb': round(s['backlog_max'] / 1e6, 2), 'stop_reason': s['reason'],
           'head_word': '%08X' % int.from_bytes(raw.head[8:12], 'little'), 'stop': raw.stop,
           'elapsed_s': round(time.time() - t0_wall, 3)}
    res = {'ok': True, 'cmd': 'log', 'plan': public(pl), 'acq': acq}
    res.update(w.summary())
    res['files'] = _files(storage, w, stem, rate)
    res['warnings'] = list(pl['warnings'])
    if s['reason'] == 'disk_allowance':
        res['warnings'].append('stopped at the disk allowance (%.1f GB); raise it with dscope bench --disk'
                               % (disk_limit / 1e9))
    if werr is not None:
        res['ok'] = False
        res['error'] = {'code': 'WRITE_FAILED', 'message': 'writing the log failed: %r' % (werr,),
                        'hint': 'check the free space and permissions of --out'}
    elif s['reason'] in STOP_ERRORS:
        code, msg, hint = STOP_ERRORS[s['reason']]
        val = {'backlog': s['backlog'] / 1e6, 'host_slow': s['queued_at_stop'] / 1e6, 'stalled': stall_s}[s['reason']]
        res['ok'] = False
        res['error'] = {'code': code, 'message': msg % val + ' (data up to the stop is kept)', 'hint': hint}
    return res


def _files(storage, w, stem, rate):
    files = {}
    if storage == 'ram' and w.files:
        files['npz'] = w.files[0]
        files['t0_s'] = round(w.kept_t0(), 6)
        files['kept_s'] = round((w.stored_blocks - w.dropped_blocks) * packet.BLOCK_SAMPLES / rate, 6)
    elif storage == 'disk' and stem:
        files['dir'] = stem
        files['index'] = os.path.join(stem, 'index.json')
        files['chunks'] = len(w.files)
    return files


def scope_applied(st, pl):
    sc = pl['_scale']
    return {'channels': list(st.analog) + list(st.digital),
            'vdiv_v': {c: round(sc[c]['vdiv_v'] * sc[c]['probe'], 6) for c in st.analog},
            'offset_v': {c: round((128 - sc[c]['zero_code']) * sc[c]['volts_per_code'] * sc[c]['probe'], 5)
                         for c in st.analog},
            'probe': {c: sc[c]['probe'] for c in st.analog},
            'coupling': {c: st.coupling.get(c, 'dc') for c in st.analog}, 'bw20': st.bw20, 'invert': st.invert,
            'threshold_v': pl['_hal']['threshold_uv'] / 1e6, 'rate_hz': pl['rate_hz'], 'mode': 'loop'}
