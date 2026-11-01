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
samples are removed first (default: 1/8 of a UART/CAN bit; 1 sample less than a third of the fastest SPI clock
half-period; 50 ns for I2C). Times are seconds on the capture's time base (t = 0 at the trigger).
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


def deglitch(b, n):
    """Remove pulses shorter than n samples (they take the level before them)."""
    n = int(n)
    if n <= 1 or len(b) < 3:
        return b
    e = _edges(b)
    if len(e) < 2:
        return b
    short = np.flatnonzero(np.diff(e) < n)
    if not len(short):
        return b
    out = b.copy()
    done = -1
    for i in short:
        a, z = e[i], e[i + 1]
        if a < done:
            continue
        out[a:z] = out[a - 1]
        done = z
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
    e = _edges(clk)
    if p['glitch'] == 'auto':
        runs = np.diff(e)
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
    g = int(round(50e-9 * L.rate)) if p['glitch'] == 'auto' else int(p['glitch'])
    scl, sda = deglitch(L.lines['scl'], g), deglitch(L.lines['sda'], g)
    rise = np.flatnonzero((scl[:-1] == 0) & (scl[1:] == 1)) + 1
    de = _edges(sda)
    de = de[scl[de] == 1]                                   # SDA changes while SCL is high: START / STOP
    ev = [(int(i), 'S' if sda[i] == 0 else 'P') for i in de] + [(int(i), 'B') for i in rise]
    ev.sort()
    frames, nerr, cur, bitbuf = [], 0, None, []

    def close(end_idx, stop):
        nonlocal cur, nerr
        if cur is None:
            return
        cur['t_end_s'] = _fmt_t(L.t(end_idx))
        cur['stop'] = stop
        if len(bitbuf) > 1:                                 # SCL always rises once before a STOP
            cur.setdefault('errors', []).append('%d stray bits' % len(bitbuf))
        if cur.get('errors'):
            nerr += 1
        frames.append(cur)
        cur = None

    for i, kind in ev:
        if kind == 'S':
            restart = cur is not None
            if restart:
                bitbuf.clear()
                cur.setdefault('segments', 1)
                cur['segments'] += 1
                cur['restart'] = True
                cur['_first'] = True
            else:
                cur = {'t_s': _fmt_t(L.t(i)), 'bytes': [], '_first': True}
            bitbuf.clear()
        elif kind == 'P':
            close(i, True)
            bitbuf.clear()
        elif cur is not None:
            bitbuf.append(int(sda[i]))
            if len(bitbuf) == 9:
                val = int(''.join(map(str, bitbuf[:8])), 2)
                ack = bitbuf[8] == 0
                if cur.pop('_first', False):
                    if val >> 3 == 0b11110:
                        cur['addr10_hi'] = (val >> 1) & 3
                        cur['_addr10'] = True
                    else:
                        cur['addr'] = val >> 1
                    cur['read'] = bool(val & 1)
                    cur['addr_ack'] = ack
                    if not ack:
                        cur.setdefault('errors', []).append('address NACK')
                elif cur.pop('_addr10', False) and not cur['read']:
                    cur['addr'] = (cur.pop('addr10_hi') << 8) | val
                    cur['addr_10bit'] = True
                    if not ack:
                        cur.setdefault('errors', []).append('address NACK')
                else:
                    cur['bytes'].append(val)
                    cur.setdefault('acks', []).append(ack)
                bitbuf.clear()
    if cur is not None:
        cur.setdefault('errors', []).append('no STOP in the window')
        close(L.n - 1, False)
    for f in frames:
        for k in ('_first', '_addr10', 'addr10_hi'):
            f.pop(k, None)
        f['data_hex'] = bytes(f.pop('bytes')).hex()
        acks = f.pop('acks', [])
        if acks and not all(acks[:-1]):
            f.setdefault('errors', []).append('data NACK before the last byte')
        f['last_ack'] = acks[-1] if acks else None
    return _result('i2c', p, frames, nerr, limit, {'glitch_samples': g})


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
