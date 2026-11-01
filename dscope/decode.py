"""Protocol decoders for captured lines: UART, SPI, I2C, CAN.

A decoder spec is 'protocol:key=value,...' (CLI --decode, Capture.decode):
  uart:rx=D4,baud=115200,bits=8,parity=none,stop=1,invert=0,order=lsb      baud=auto measures the bit time
  spi:clk=D1,mosi=D3,miso=D2,cs=D0,mode=0,order=msb,bits=8,cs_active=low  mosi/miso/cs optional (one data line)
  i2c:scl=D6,sda=D7
  can:rx=D6,bitrate=500k,invert=0,sample_point=0.75                        bitrate=auto measures the bit time
invert=1 inverts a line: RS232 levels (idle negative) and CANH (dominant high) need it. Lines are digital channels
(the scope's threshold) or analog channels thresholded at `level` (default: midway between the 5th and 95th
percentile) with 10 % hysteresis. All lines of one decoder must be in the same group (all digital or all analog).

The digital inputs have no hysteresis, so slow edges chatter at high sample rates: pulses shorter than `glitch`
samples are removed first (default: 1/8 of a UART/CAN bit; a third of the fastest SPI clock half-period; for I2C
1/8 of the typical SCL half-period, at least 50 ns). Times are seconds on the capture's time base (t = 0 at the trigger).
"""
import numpy as np

from .errors import DscopeError
from .units import si

DEFAULTS = {
    'uart': {'rx': None, 'baud': 'auto', 'bits': '8', 'parity': 'none', 'stop': '1', 'invert': '0', 'order': 'lsb',
             'glitch': 'auto', 'level': None},
    'spi': {'clk': None, 'mosi': None, 'miso': None, 'cs': None, 'mode': '0', 'order': 'msb', 'bits': '8',
            'cs_active': 'low', 'glitch': 'auto', 'level': None},
    'i2c': {'scl': None, 'sda': None, 'glitch': 'auto', 'level': None},
    'can': {'rx': None, 'bitrate': 'auto', 'invert': '0', 'sample_point': '0.75', 'glitch': 'auto', 'level': None},
}
LINES = {'uart': ('rx',), 'spi': ('clk', 'mosi', 'miso', 'cs'), 'i2c': ('scl', 'sda'), 'can': ('rx',)}
ALIASES = {'tx': 'rx', 'line': 'rx', 'sck': 'clk', 'ss': 'cs', 'nss': 'cs', 'cpol_cpha': 'mode', 'rate': 'baud'}
STD_BAUDS = (300, 600, 1200, 2400, 4800, 9600, 14400, 19200, 28800, 38400, 57600, 76800, 115200, 230400, 250000,
             460800, 500000, 921600, 1000000, 1500000, 2000000, 2250000, 3000000, 4000000, 4500000)
STD_CAN = (10e3, 20e3, 33.333e3, 50e3, 83.333e3, 100e3, 125e3, 250e3, 500e3, 800e3, 1e6)


def _bad(msg):
    return DscopeError('INVALID_ARG', msg)


def parse_spec(spec):
    """'uart:rx=D4,baud=9600' -> ('uart', {...}). Unknown keys and protocols are errors."""
    proto, _, rest = str(spec).partition(':')
    proto = proto.strip().lower()
    if proto not in DEFAULTS:
        raise _bad('unknown decoder %r (uart, spi, i2c, can)' % proto)
    p = dict(DEFAULTS[proto])
    for part in filter(None, (x.strip() for x in rest.split(','))):
        k, eq, v = part.partition('=')
        k = ALIASES.get(k.strip().lower(), k.strip().lower())
        if not eq or k not in p:
            raise _bad('bad %s decoder option %r (known: %s)' % (proto, part, ', '.join(DEFAULTS[proto])))
        p[k] = v.strip()
    for k in LINES[proto]:
        if p.get(k):
            p[k] = p[k].upper()
    need = {'uart': ('rx',), 'spi': ('clk',), 'i2c': ('scl', 'sda'), 'can': ('rx',)}[proto]
    for k in need:
        if not p.get(k):
            raise _bad('%s decoder needs %s=<channel>' % (proto, k))
    if proto == 'spi' and not (p['mosi'] or p['miso']):
        raise _bad('spi decoder needs mosi= and/or miso=')
    return proto, p


def channels_of(spec):
    proto, p = parse_spec(spec)
    return [p[k] for k in LINES[proto] if p.get(k)]


# ------------------------------------------------------------------------------------------- line helpers
def _edges(b):
    """Indices i where b[i] != b[i-1] (the new level starts at i)."""
    return np.flatnonzero(b[1:] != b[:-1]) + 1


def deglitch(b, n, max_passes=32):
    """Remove pulses shorter than n samples: majority (median) filter passes of width 2n-1, applied only around the
    short runs and repeated until no inner run is shorter than n (one pass leaves dense chatter behind; a binary
    median filter converges to a signal whose inner runs are all >= n). Unlike filling a short run with the level
    before it, this keeps edges in place when a spike splits a run."""
    n = int(n)
    if n <= 1 or len(b) < 3:
        return b
    k = n - 1
    out = b
    nb = len(b)
    for _ in range(max_passes):
        e = _edges(out)
        if len(e) < 2:
            break
        short = np.flatnonzero(np.diff(e) < n)
        if not len(short):
            break
        # regions [first edge of a cluster of short runs - k, last edge + k], merged where they come within k
        lo = e[short] - k
        hi = e[short + 1] + k
        new = np.ones(len(lo), bool)
        new[1:] = lo[1:] > np.maximum.accumulate(hi)[:-1] + k
        first = np.flatnonzero(new)
        starts, ends = lo[first], np.maximum.reduceat(hi, first)
        results = []
        for a_, z_ in zip(starts, ends):                    # computed from this pass's input, then written
            a_, z_ = max(0, int(a_)), min(nb, int(z_))
            pa, pz = max(0, a_ - k), min(nb, z_ + k)
            seg = np.pad(out[pa:pz].astype(np.int32), (k - (a_ - pa), k - (pz - z_)), mode='edge')
            c = np.concatenate(([0], np.cumsum(seg)))
            win = c[2 * k + 1:] - c[:-(2 * k + 1)]          # window sums centred on a_ .. z_-1
            results.append((a_, z_, (win[:z_ - a_] > k).astype(b.dtype)))
        if out is b:
            out = b.copy()
        for a_, z_, v in results:
            out[a_:z_] = v
    return out


def _majority(b, centers, half):
    """Line level at the given (float) sample positions, majority of 3 samples spaced `half` apart."""
    c = np.clip(np.rint(centers).astype(np.int64), 0, len(b) - 1)
    if half < 1:
        return b[c].astype(np.uint8)
    lo = np.clip(c - half, 0, len(b) - 1)
    hi = np.clip(c + half, 0, len(b) - 1)
    return ((b[lo].astype(np.uint8) + b[c] + b[hi]) >= 2).astype(np.uint8)


class _Lines:
    """The lines of one decoder: 0/1 arrays on a common time base (window applied)."""

    def __init__(self, cap, p, keys, window=None):
        names = [p[k] for k in keys if p.get(k)]
        groups = {n[0] for n in names}
        if len(groups) > 1:
            raise _bad('decoder lines must all be digital or all analog: %s' % ', '.join(names))
        for n in names:
            if n not in (cap.digital if n[0] == 'D' else cap.analog):
                raise _bad('decoder line %s was not captured (add it to -c)' % n)
        ref = names[0]
        self.rate = float(cap.rate(ref))
        a = cap._win_start(ref, window)
        self.t_first = cap.t0(ref) + a / self.rate
        self.lines = {}
        for k in keys:
            n = p.get(k)
            if not n:
                continue
            if n[0] == 'D':
                arr = cap.digital[n]
                b = cap._win(n, arr, window) if window else arr
                self.lines[k] = np.asarray(b, dtype=np.uint8)
            else:
                v = cap.volts(n)
                v = cap._win(n, v, window) if window else v
                self.lines[k] = _schmitt(v, p.get('level'))
        self.n = min(len(x) for x in self.lines.values())

    def t(self, idx):
        return self.t_first + np.asarray(idx, dtype=np.float64) / self.rate


def _schmitt(v, level=None):
    if len(v) == 0:
        return np.zeros(0, np.uint8)
    lo_p, hi_p = np.percentile(v, [5, 95])
    th = si(level, 'V', 'level') if level not in (None, '') else (lo_p + hi_p) / 2
    h = max(0.1 * (hi_p - lo_p) / 2, 1e-6)
    hi, lo = v > th + h, v < th - h
    idx = np.where(hi | lo, np.arange(len(v)), 0)
    np.maximum.accumulate(idx, out=idx)
    out = hi[idx].astype(np.uint8)
    first = np.flatnonzero(hi | lo)
    if len(first):
        out[:first[0]] = hi[first[0]]
    return out


def _fmt_t(x):
    return float('%.10g' % x)


def _bit_time(b, rate, glitch=1):
    """Estimate one bit time (samples) from the run lengths of an NRZ line."""
    e = _edges(b)
    if len(e) < 3:
        return None
    r = np.diff(e).astype(np.float64)
    r = r[r >= max(2, glitch)]
    if not len(r):
        return None
    rs = np.sort(r)
    need = max(3, int(0.01 * len(rs)))
    t0 = float(rs[0])
    for c in np.unique(rs[:500]):          # shortest run length that occurs often (single glitches do not count)
        if np.count_nonzero((rs >= 0.8 * c) & (rs <= 1.2 * c)) >= need:
            t0 = float(c)
            break
    k = np.rint(r / t0)
    ok = (k >= 1) & (k <= 10) & (np.abs(r / t0 - k) < 0.3)
    if ok.sum() < 3:
        return t0
    return float(r[ok].sum() / k[ok].sum())


def _snap(value, table, tol=0.02):
    best = min(table, key=lambda x: abs(x - value) / x)
    return best if abs(best - value) / best <= tol else value


def _on(v):
    return str(v).strip().lower() in ('1', 'true', 'yes', 'on')


# ------------------------------------------------------------------------------------------- UART
def uart(L, p, limit):
    b = L.lines['rx']
    if _on(p['invert']):
        b = 1 - b
    bits, stop = int(p['bits']), float(p['stop'])
    par = {'n': 0, 'none': 0, 'e': 1, 'even': 1, 'o': 2, 'odd': 2}.get(str(p['parity']).lower())
    if par is None or not 5 <= bits <= 9 or stop not in (0.5, 1, 1.5, 2):
        raise _bad('uart: bits 5..9, parity none/even/odd, stop 0.5/1/1.5/2')
    measured = None
    if str(p['baud']).lower() == 'auto':
        spb = _bit_time(b, L.rate)
        if spb is None:
            return _result('uart', p, [], extra={'note': 'no activity on the line'})
        measured = L.rate / spb
        baud = _snap(measured, STD_BAUDS)
    else:
        baud = si(p['baud'], 'Hz', 'baud')
    spb = L.rate / baud
    if spb < 3:
        raise _bad('uart: %.1f samples per bit; capture at >= %g Sa/s (>= 10 per bit for reliable decoding)'
                   % (spb, 10 * baud))
    g = int(spb / 8) if p['glitch'] == 'auto' else int(p['glitch'])
    b = deglitch(b, g)
    nfield = 1 + bits + (1 if par else 0)
    falls = np.flatnonzero((b[:-1] == 1) & (b[1:] == 0)) + 1
    offs = (np.arange(nfield + 1) + 0.5) * spb
    half = max(0, int(spb / 16))
    frames, data, nerr = [], bytearray(), 0
    pos, k = 0, 0
    while True:
        k = np.searchsorted(falls, pos, side='left')
        if k >= len(falls):
            break
        s = falls[k]
        if s + offs[-1] + half >= L.n:
            break
        v = _majority(b, s + offs, half)
        if v[0] != 0:
            pos = s + 1
            continue
        d = v[1:1 + bits]
        val = int(sum(int(x) << i for i, x in enumerate(d))) if p['order'] == 'lsb' else \
            int(sum(int(x) << (bits - 1 - i) for i, x in enumerate(d)))
        f = {'t_s': _fmt_t(L.t(s)), 'value': val}
        errs = []
        if par:
            ones = int(d.sum()) + int(v[1 + bits])
            if (ones % 2 == 1) != (par == 2):
                errs.append('parity')
        if v[nfield] != 1:
            errs.append('framing' if d.any() or (par and v[1 + bits]) else 'break')
        if errs:
            f['errors'] = errs
            nerr += 1
        frames.append(f)
        data.append(val & 0xFF)
        pos = int(s + offs[nfield])          # middle of the (first) stop bit
    extra = {'baud': baud, 'samples_per_bit': round(spb, 2), 'data_hex': bytes(data[:4096]).hex(),
             'text': ''.join(chr(c) if 32 <= c < 127 else '.' for c in data[:1024])}
    if measured:
        extra['baud_measured'] = round(measured, 1)
    return _result('uart', p, frames, nerr, limit, extra)


# ------------------------------------------------------------------------------------------- SPI
def spi(L, p, limit):
    mode = int(p['mode'])
    if mode not in (0, 1, 2, 3):
        raise _bad('spi: mode 0..3')
    cpol, cpha = mode >> 1, mode & 1
    bits = int(p['bits'])
    if not 1 <= bits <= 64:
        raise _bad('spi: bits 1..64')
    clk = L.lines['clk']
    if p['glitch'] == 'auto':
        # from a provisionally cleaned clock (1-sample pulses removed): raw run lengths shrink exactly when the
        # edges chatter. Any clock that can be decoded has half periods of 2 samples or more.
        runs = np.diff(_edges(deglitch(clk, 2)))
        g = max(1, int(np.percentile(runs, 10) / 3)) if len(runs) > 4 else 1
    else:
        g = int(p['glitch'])
    clk = deglitch(clk, g)
    rising = cpol == cpha
    se = np.flatnonzero((clk[:-1] == (0 if rising else 1)) & (clk[1:] == (1 if rising else 0))) + 1
    data = {k: deglitch(L.lines[k], g) for k in ('mosi', 'miso') if k in L.lines}
    # transactions: CS active intervals, or bursts of clocks separated by long gaps
    if 'cs' in L.lines:
        cs = deglitch(L.lines['cs'], g)
        act = 0 if p['cs_active'] == 'low' else 1
        a = (cs == act).astype(np.int8)
        da = np.diff(a)
        starts = list(np.flatnonzero(da == 1) + 1)
        ends = list(np.flatnonzero(da == -1) + 1)
        if a[0]:
            starts.insert(0, 0)
        if a[-1]:
            ends.append(L.n)
        spans = [(s, z, s == 0, z == L.n) for s, z in zip(starts, ends)]
    else:
        if len(se) == 0:
            spans = []
        else:
            per = np.diff(se)
            gap = max(8 * float(np.median(per)), 4) if len(per) else 0
            cut = np.flatnonzero(per > gap) + 1
            bounds = [0] + list(cut) + [len(se)]
            spans = [(se[i0], se[i1 - 1] + 1, False, False) for i0, i1 in zip(bounds[:-1], bounds[1:])]
    frames, nerr, streams = [], 0, {k: bytearray() for k in data}
    periods = []
    for s, z, cut_start, cut_end in spans:
        i0, i1 = np.searchsorted(se, s, 'left'), np.searchsorted(se, z, 'left')
        idx = se[i0:i1]
        if len(idx) > 1:
            periods.append(np.diff(idx))
        nw = len(idx) // bits
        f = {'t_s': _fmt_t(L.t(s)), 't_end_s': _fmt_t(L.t(min(z, L.n - 1))), 'words': nw}
        for k, line in data.items():
            v = line[idx[:nw * bits]].reshape(nw, bits).astype(np.uint64)
            w = (1 << np.arange(bits - 1, -1, -1, dtype=np.uint64)) if p['order'] == 'msb' else \
                (1 << np.arange(bits, dtype=np.uint64))
            vals = (v * w).sum(axis=1) if nw else np.zeros(0, np.uint64)
            if bits == 8:
                bs = bytes(int(x) for x in vals)
                f[k] = bs.hex()
                streams[k] += bs
            else:
                f[k] = [int(x) for x in vals]
        errs = []
        if len(idx) % bits:
            errs.append('partial word (%d bits)' % (len(idx) % bits))
        if cut_start or cut_end:
            errs.append('cut by the capture window')
        if errs:
            f['errors'] = errs
            nerr += 1
        frames.append(f)
    extra = {'mode': mode, 'bits': bits, 'glitch_samples': g}
    if periods:
        per = np.concatenate(periods)
        per = per[per < 1.5 * np.median(per)]               # clock periods inside words, not the gaps
        extra['sck_hz'] = float('%.6g' % (L.rate / float(np.mean(per))))
        extra['samples_per_clock'] = round(float(np.mean(per)), 2)
    for k, s_ in streams.items():
        extra[k + '_hex'] = bytes(s_[:4096]).hex()
    return _result('spi', p, frames, nerr, limit, extra)


# ------------------------------------------------------------------------------------------- I2C
def i2c(L, p, limit):
    """Transactions from START to STOP. Each has `segments` (one per START / repeated START): addr (7 or 10 bit),
    read, addr_ack, data_hex, acks; plus the convenience fields addr (first segment), read (last segment),
    restart, data_hex (all data bytes), last_ack. Bits clocked outside a transaction are ignored."""
    if p['glitch'] == 'auto':
        # The slow pulled-up edges chatter at the threshold. A quarter of the SCL high time (set by the master; a
        # stretching slave only lengthens the lows), measured on a provisionally cleaned line, and at least 50 ns
        # (the fast-mode spike filter), stays far below any pulse that matters for START, STOP or data.
        # Undersampled lines (a few samples per SCL period) get no filter: there a 1-sample high is real.
        spike = int(round(50e-9 * L.rate))
        s0 = deglitch(L.lines['scl'], 2)                    # provisional: without 1-sample spikes
        e0 = _edges(s0)
        runs = np.diff(e0)
        high = runs[s0[e0[:-1]] == 1] if len(e0) > 1 else runs
        g = max(spike, int(np.median(high) / 4)) if len(high) > 4 else spike
    else:
        g = int(p['glitch'])
    scl, sda = deglitch(L.lines['scl'], g), deglitch(L.lines['sda'], g)
    rise = np.flatnonzero((scl[:-1] == 0) & (scl[1:] == 1)) + 1
    de = _edges(sda)
    de = de[scl[de] == 1]                                   # SDA changes while SCL is high: START / STOP
    ev = [(int(i), 'S' if sda[i] == 0 else 'P') for i in de] + [(int(i), 'B') for i in rise]
    ev.sort()
    frames, nerr, periods = [], 0, []
    st = {'cur': None, 'seg': None, 'bits': [], 'state': None, 'last_rise': None,
          'idle': bool(scl[0]) and bool(sda[0]) if len(scl) else True}

    def finish_segment(cut=False):
        """cut: the window ended inside this segment, so its last byte's ACK says nothing about the master."""
        seg = st['seg']
        if seg is None:
            return
        cur = st['cur']
        errs = cur.setdefault('errors', [])
        if len(st['bits']) > 1 and not cut:                 # SCL always rises once before a STOP / repeated START
            errs.append('%d stray bits' % len(st['bits']))
        if seg.get('addr_ack') is False:
            errs.append('address NACK')
        acks = seg['acks']
        if seg.get('read'):
            if acks and not all(acks[:-1]):
                errs.append('read NACK before the last byte')
            if acks and acks[-1] and not cut:
                cur.setdefault('notes', []).append('last read byte ACKed by the master')
        elif seg.get('addr_ack') and not all(acks):
            errs.append('write NACK at byte %d' % acks.index(False))
        st['seg'] = None

    def close(end_idx, stop):
        nonlocal nerr
        cur = st['cur']
        if cur is None:
            return
        finish_segment(cut=not stop)
        cur['t_end_s'] = _fmt_t(L.t(end_idx))
        cur['stop'] = stop
        if not stop:
            cur['errors'].append('no STOP in the window')
        segs = cur['segments']
        for s_ in segs:
            s_['data_hex'] = bytes(s_.pop('data')).hex()
        first = segs[0] if segs else {}
        cur['addr'] = first.get('addr')
        cur['addr_ack'] = first.get('addr_ack')
        cur['read'] = segs[-1].get('read') if segs else None
        cur['restart'] = len(segs) > 1
        cur['data_hex'] = ''.join(s_['data_hex'] for s_ in segs)
        lastacks = segs[-1]['acks'] if segs else []
        cur['last_ack'] = lastacks[-1] if lastacks else None
        if not cur['errors']:
            del cur['errors']
        else:
            nerr += 1
        frames.append(cur)
        st['cur'] = None

    def byte(val, ack):
        seg, state = st['seg'], st['state']
        if state == 'addr':
            seg['read'] = bool(val & 1)
            seg['addr_ack'] = ack
            if val >> 3 == 0b11110:                         # 10-bit address
                hi = (val >> 1) & 3
                seg['addr_10bit'] = True
                if seg['read']:                             # a read header is never followed by an address byte
                    prev = [s_ for s_ in st['cur']['segments'][:-1] if s_.get('addr_10bit')]
                    if prev and prev[-1]['addr'] >> 8 == hi:
                        seg['addr'] = prev[-1]['addr']
                    else:
                        seg['addr'] = hi << 8
                        st['cur'].setdefault('notes', []).append(
                            '10-bit read header without a preceding address write: low address byte unknown')
                    st['state'] = 'data'
                else:
                    seg['addr'] = hi << 8
                    st['state'] = 'addr2' if ack else 'data'
            else:
                seg['addr'] = val >> 1
                st['state'] = 'data'
        elif state == 'addr2':
            seg['addr'] |= val
            seg['addr_ack'] = ack
            st['state'] = 'data'
        else:
            seg['data'].append(val)
            seg['acks'].append(ack)

    for i, kind in ev:
        if kind == 'S':
            if st['cur'] is None:
                st['cur'] = {'t_s': _fmt_t(L.t(i)), 'segments': []}
                if not st['idle']:                          # SCL was clocking with no STOP since the window start
                    st['cur']['cut_start'] = True
                    st['cur'].setdefault('notes', []).append(
                        'window starts mid-transaction: this START may be a repeated START')
            else:
                finish_segment()
            st['seg'] = {'data': [], 'acks': []}
            st['cur']['segments'].append(st['seg'])
            st['state'], st['bits'] = 'addr', []
        elif kind == 'P':
            close(i, True)
            st['bits'] = []
            st['idle'] = True
        elif st['cur'] is None:
            st['idle'] = False                              # bits outside a transaction: we joined mid-transaction
        elif st['seg'] is not None:
            if st['last_rise'] is not None:
                periods.append(i - st['last_rise'])
            st['last_rise'] = i
            st['bits'].append(int(sda[i]))
            if len(st['bits']) == 9:
                b = st['bits']
                byte(int(''.join(map(str, b[:8])), 2), b[8] == 0)
                st['bits'] = []
            continue
        st['last_rise'] = None
    if st['cur'] is not None:
        close(L.n - 1, False)
    extra = {'glitch_samples': g}
    if len(periods) > 4:
        per = np.array(periods, np.float64)
        per = per[per < 1.5 * np.median(per)]               # SCL periods inside bytes, not the gaps
        extra['scl_hz'] = float('%.6g' % (L.rate / float(np.mean(per))))
    return _result('i2c', p, frames, nerr, limit, extra)


# ------------------------------------------------------------------------------------------- CAN
def crc15(bits):
    crc = 0
    for b in bits:
        nxt = int(b) ^ ((crc >> 14) & 1)
        crc = (crc << 1) & 0x7FFF
        if nxt:
            crc ^= 0x4599
    return crc


def can(L, p, limit):
    b = L.lines['rx']
    if _on(p['invert']):
        b = 1 - b
    sp = float(p['sample_point'])
    measured = None
    if str(p['bitrate']).lower() == 'auto':
        spb = _bit_time(b, L.rate)
        if spb is None:
            return _result('can', p, [], extra={'note': 'no activity on the line'})
        measured = L.rate / spb
        bitrate = _snap(measured, STD_CAN)
    else:
        bitrate = si(p['bitrate'], 'Hz', 'bitrate')
    T = L.rate / bitrate
    if T < 4:
        raise _bad('can: %.1f samples per bit; capture at >= %g Sa/s' % (T, 10 * bitrate))
    g = int(T / 8) if p['glitch'] == 'auto' else int(p['glitch'])
    b = deglitch(b, g)
    falls = np.flatnonzero((b[:-1] == 1) & (b[1:] == 0)) + 1
    half = max(0, int(T / 16))
    frames, nerr, pos = [], 0, 0
    while True:
        k = np.searchsorted(falls, pos, 'left')
        if k >= len(falls):
            break
        s = int(falls[k])
        if s < 6 * T or b[max(0, s - int(6 * T)):s].min() == 0:
            pos = s + 1                                     # 6 recessive bits cannot be inside a stuffed frame
            continue
        f, end = _can_frame(b, falls, s, T, sp, half, L.n)
        if f is None:
            break
        f['t_s'] = _fmt_t(L.t(s))
        if f.get('errors'):
            nerr += 1
        frames.append(f)
        pos = max(end, s + 1)
    extra = {'bitrate': bitrate, 'samples_per_bit': round(T, 2), 'sample_point': sp}
    if measured:
        extra['bitrate_measured'] = round(measured, 1)
    return _result('can', p, frames, nerr, limit, extra)


def _can_frame(b, falls, s, T, sp, half, n):
    """Decode one frame starting with the SOF edge at s. -> (frame dict or None if cut off, end index)."""
    sync, nb = float(s), 0               # last hard/re-synchronisation edge and bits since it
    last_sample = s - 1
    run_val, run_len = None, 0
    raw_count = 0
    bits = []                            # destuffed

    def next_raw():
        nonlocal sync, nb, last_sample, raw_count
        target = sync + (nb + sp) * T
        # resynchronise on a recessive -> dominant edge inside this bit
        j = np.searchsorted(falls, last_sample + 1, 'left')
        if j < len(falls) and falls[j] < target and falls[j] > sync + (nb - 0.5) * T:
            sync, nb = float(falls[j]), 0
            target = sync + sp * T
        if target + half >= n:
            return None
        v = int(_majority(b, np.array([target]), half)[0])
        last_sample = int(target)
        nb += 1
        raw_count += 1
        return v

    def stuffed():
        """Next destuffed bit (None at the end of the capture, 'stuff' on a stuff error)."""
        nonlocal run_val, run_len
        v = next_raw()
        if v is None:
            return None
        if run_len == 5:
            if v == run_val:
                return 'stuff'
            run_val, run_len = v, 1
            v = next_raw()
            if v is None:
                return None
        if v == run_val:
            run_len += 1
        else:
            run_val, run_len = v, 1
        return v

    def take(count):
        out = []
        for _ in range(count):
            v = stuffed()
            if v is None or v == 'stuff':
                return out, v
            out.append(v)
        return out, 0

    f = {}
    errs = []

    def done(err=None):
        if err:
            errs.append(err)
        if errs:
            f['errors'] = errs
        return f, int(sync + (nb + 1) * T)

    seg, st = take(1 + 11 + 1 + 1)       # SOF, ID, RTR/SRR, IDE
    if st is None or len(seg) < 14:
        return (None, n) if st is None else done('stuff error (error frame?)')
    bits += seg
    ide = seg[13]
    ident = int(''.join(map(str, seg[1:12])), 2)
    rtr = seg[12]
    if ide:
        seg, st = take(18 + 1 + 2)       # ID extension, RTR, r1, r0
        if st is None:
            return None, n
        if st == 'stuff':
            return done('stuff error (error frame?)')
        bits += seg
        ident = (ident << 18) | int(''.join(map(str, seg[:18])), 2)
        rtr = seg[18]
    else:
        seg, st = take(1)                # r0
        if st is None:
            return None, n
        if st == 'stuff':
            return done('stuff error (error frame?)')
        bits += seg
    seg, st = take(4)
    if st is None:
        return None, n
    if st == 'stuff':
        return done('stuff error (error frame?)')
    bits += seg
    dlc = int(''.join(map(str, seg)), 2)
    f.update({'id': ident, 'id_hex': '0x%X' % ident, 'extended': bool(ide), 'remote': bool(rtr), 'dlc': dlc})
    ndata = 0 if rtr else min(dlc, 8)
    seg, st = take(8 * ndata)
    if st is None:
        return None, n
    if st == 'stuff':
        return done('stuff error (error frame?)')
    bits += seg
    f['data'] = bytes(int(''.join(map(str, seg[i:i + 8])), 2) for i in range(0, len(seg), 8)).hex()
    seg, st = take(15)
    if st is None:
        return None, n
    if st == 'stuff':
        return done('stuff error (error frame?)')
    crc_rx = int(''.join(map(str, seg)), 2)
    crc_calc = crc15(bits)
    f['crc'] = '0x%04X' % crc_rx
    f['crc_ok'] = crc_rx == crc_calc
    if not f['crc_ok']:
        errs.append('CRC mismatch (computed 0x%04X)' % crc_calc)
    if run_len == 5:                     # a stuff bit can follow the last CRC bit
        v = next_raw()
        if v is None:
            return None, n
        if v == run_val:
            return done('stuff error after the CRC')
    tail = []
    for _ in range(3 + 7):               # CRC delimiter, ACK slot, ACK delimiter, EOF
        v = next_raw()
        if v is None:
            break
        tail.append(v)
    if len(tail) >= 2:
        f['ack'] = tail[1] == 0
    if tail[:1] and tail[0] != 1:
        errs.append('form error: CRC delimiter dominant')
    if len(tail) >= 3 and tail[2] != 1:
        errs.append('form error: ACK delimiter dominant')
    if len(tail) == 10 and min(tail[3:]) == 0:
        errs.append('form error: EOF not recessive')
    f['stuff_bits'] = raw_count - len(bits) - 15 - len(tail)
    return done()


# ------------------------------------------------------------------------------------------- entry points
def _result(proto, p, frames, nerr=0, limit=None, extra=None):
    out = {'protocol': proto, 'params': {k: v for k, v in p.items() if v not in (None, '')}, 'count': len(frames),
           'errors': nerr}
    out.update(extra or {})
    out['frames'] = frames
    return out


def trim(res, limit):
    """A copy listing at most `limit` frames (plus up to `limit` frames with errors); counts stay complete."""
    frames = res['frames']
    if limit is None or len(frames) <= limit:
        return res
    out = dict(res)
    out['frames'] = frames[:limit]
    out['truncated'] = True
    out['error_frames'] = [f for f in frames if f.get('errors')][:limit]
    return out


DECODERS = {'uart': uart, 'spi': spi, 'i2c': i2c, 'can': can}


def decode(cap, spec, window=None, limit=50):
    """Decode one spec on a Capture. limit: frames listed in full (None = all)."""
    proto, p = parse_spec(spec)
    lines = _Lines(cap, p, LINES[proto], window)
    res = DECODERS[proto](lines, p, None)
    res['spec'] = spec
    return trim(res, limit)


def check(specs, channels):
    """Validate decoder specs against the channels a capture will have (before the scope is touched)."""
    for spec in specs or ():
        missing = [c for c in channels_of(spec) if c not in channels]
        if missing:
            raise _bad('decoder %r uses %s, which is not captured (add it to -c)' % (spec, ', '.join(missing)))
