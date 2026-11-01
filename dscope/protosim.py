"""Synthetic protocol traffic as 0/1 sample arrays (offline decoder tests, the logic bench's PATTERN command and
its --fake mode). Levels are logical: UART idle 1, CAN recessive 1 (dominant 0)."""
import numpy as np

from .decode import crc15


def render(segs, unit):
    """segs: [(level or tuple of levels, length in units)] -> array (or tuple of arrays); unit = samples per unit."""
    bounds = np.rint(np.cumsum([0.0] + [float(l) for _, l in segs]) * unit).astype(np.int64)
    multi = isinstance(segs[0][0], tuple)
    levels = np.array([lv for lv, _ in segs], np.uint8).reshape(len(segs), -1)
    out = np.repeat(levels, np.diff(bounds), axis=0).T
    return tuple(np.ascontiguousarray(x) for x in out) if multi else np.ascontiguousarray(out[0])


# ------------------------------------------------------------------------------------------- UART
def uart_bits(value, bits=8, parity='none', order='lsb'):
    d = [(value >> i) & 1 for i in range(bits)]
    if order == 'msb':
        d = d[::-1]
    out = [0] + d
    if parity in ('even', 'odd'):
        out.append((sum(d) + (1 if parity == 'odd' else 0)) % 2)
    return out


def uart(data, baud, rate, bits=8, parity='none', stop=1, gap_bits=0, lead_bits=10, tail_bits=10, invert=False,
         order='lsb', stop_errors=()):
    """data: iterable of ints. stop_errors: indices of characters sent with a dominant (0) stop bit."""
    segs = [(1, lead_bits)]
    for i, v in enumerate(data):
        segs += [(b, 1) for b in uart_bits(v, bits, parity, order)]
        segs.append((0 if i in stop_errors else 1, stop))
        if gap_bits:
            segs.append((1, gap_bits))
    segs.append((1, tail_bits))
    out = render(segs, rate / baud)
    return 1 - out if invert else out


# ------------------------------------------------------------------------------------------- SPI
def spi(mosi, miso=None, mode=0, bits=8, order='msb', half=5, frame=None, cs_lead=3, cs_lag=3, gap=8, lead=10,
        tail=10, cs_active=0):
    """-> (cs, clk, mosi, miso) arrays; `half` = samples per clock half-period; `frame` words per CS assertion."""
    cpol, cpha = mode >> 1, mode & 1
    miso = list(miso) if miso is not None else [0] * len(mosi)
    frame = frame or len(mosi)
    idle_cs = 1 - cs_active
    segs = [((idle_cs, cpol, 0, 0), lead)]
    for f0 in range(0, len(mosi), frame):
        segs.append(((cs_active, cpol, 0, 0), cs_lead))
        for wo, wi in zip(mosi[f0:f0 + frame], miso[f0:f0 + frame]):
            for k in range(bits):
                sh = bits - 1 - k if order == 'msb' else k
                bo, bi = (wo >> sh) & 1, (wi >> sh) & 1
                if cpha == 0:
                    segs += [((cs_active, cpol, bo, bi), 1), ((cs_active, 1 - cpol, bo, bi), 1)]
                else:
                    segs += [((cs_active, 1 - cpol, bo, bi), 1), ((cs_active, cpol, bo, bi), 1)]
        segs.append(((cs_active, cpol, 0, 0), cs_lag))
        segs.append(((idle_cs, cpol, 0, 0), gap))
    segs.append(((idle_cs, cpol, 0, 0), tail))
    return render(segs, half)


# ------------------------------------------------------------------------------------------- I2C
def i2c(transactions, quarter=5, lead=10, tail=10):
    """transactions: [{'addr', 'read', 'data': [...], 'nack_addr': bool, 'restart': {...} or None}] -> (scl, sda).
    Each bit takes 4 quarter-periods; acks: the receiver pulls SDA low (master NACKs the last read byte)."""
    segs = [((1, 1), lead)]

    def bit(v):
        segs.extend([((0, v), 1), ((1, v), 2), ((0, v), 1)])

    def start():
        segs.extend([((1, 1), 1), ((1, 0), 1), ((0, 0), 1)])

    def byte(v, ack):
        for k in range(7, -1, -1):
            bit((v >> k) & 1)
        bit(0 if ack else 1)

    for t in transactions:
        start()
        seg = t
        while seg is not None:
            byte((seg['addr'] << 1) | (1 if seg.get('read') else 0), not seg.get('nack_addr'))
            if not seg.get('nack_addr'):
                d = seg.get('data', [])
                for i, v in enumerate(d):
                    byte(v, i < len(d) - 1 if seg.get('read') else not seg.get('nack_data'))
            seg = seg.get('restart')
            if seg is not None:
                segs.extend([((0, 1), 1), ((1, 1), 1)])   # SDA high, SCL high, then START again
                segs.extend([((1, 0), 1), ((0, 0), 1)])
        segs.extend([((0, 0), 1), ((1, 0), 1), ((1, 1), 2)])  # STOP
    segs.append(((1, 1), tail))
    return render(segs, quarter)


# ------------------------------------------------------------------------------------------- CAN
def _bits(v, n):
    return [(v >> (n - 1 - i)) & 1 for i in range(n)]


def stuff(bits):
    out, run_v, run_n = [], None, 0
    for b in bits:
        out.append(b)
        if b == run_v:
            run_n += 1
        else:
            run_v, run_n = b, 1
        if run_n == 5:
            out.append(1 - b)
            run_v, run_n = 1 - b, 1
    return out


def can_frame_bits(ident, data=b'', extended=False, remote=False, dlc=None, ack=True, crc_xor=0):
    """Bit levels on the bus (dominant 0) from SOF to the end of EOF."""
    data = bytes(data)
    dlc = len(data) if dlc is None else dlc
    if extended:
        b = [0] + _bits(ident >> 18, 11) + [1, 1] + _bits(ident & 0x3FFFF, 18) + [1 if remote else 0, 0, 0]
    else:
        b = [0] + _bits(ident, 11) + [1 if remote else 0, 0, 0]
    b += _bits(dlc, 4)
    if not remote:
        for v in data[:min(dlc, 8)]:
            b += _bits(v, 8)
    crc = crc15(b) ^ crc_xor
    return stuff(b + _bits(crc, 15)) + [1, 0 if ack else 1, 1] + [1] * 7


def can(frames, bitrate, rate, idle_bits=12, between_bits=3, rate_error=0.0):
    """frames: [dict(ident=..., data=..., extended=..., remote=..., ack=..., crc_xor=...)] or raw bit lists.
    rate_error: relative transmitter clock error (tests resynchronisation)."""
    segs = [(1, idle_bits)]
    for f in frames:
        bits = f if isinstance(f, list) else can_frame_bits(**f)
        segs += [(b, 1) for b in bits]
        segs.append((1, between_bits))
    segs.append((1, idle_bits))
    return render(segs, rate / bitrate * (1 + rate_error))
