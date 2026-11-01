"""Decoder for the DSCope M454 ("v3" protocol) capture stream on bulk IN EP 0x86.

Stream structure (all acquisition modes):

    [head 4096 B]  then, per frame:  [data: acq_bytes][tail 4096 B]  (+ zero padding after the last frame)

* head: u32[0] = 0x55555555, u32[2] = trigger word (bit31 = NOT triggered, bit30 = flag seen in
  HW_BASE_MODE 2 runs, bits 0..29 = trigger position relative to the pretrigger offset).
* data: a sequence of fixed-size blocks.  One block = analog part then digital part:
    analog : len(analog) * amtxs * 8 u64 words, word w -> channel analog[w % nA], 8 consecutive
             8-bit samples per word (byte k = sample k).  amtxs = analog samples per block / 64.
    digital: len(digital) * dmtxs u64 words (little endian), word w -> channel digital[w % nD],
             bit i = sample i of that word's 64-sample chunk.  dmtxs = digital samples per block / 64.
  amtxs/dmtxs are the analog/digital rates divided by their gcd (registers HORI_AMTXS/DMTXS + 1), so a
  block always covers the same time span for both domains, but analog and digital can have different
  sample rates and therefore different lengths.  Folded (interleaved-ADC) channels arrive already in
  time order.
* acq_bytes = align_up(acq_words * 4, 4096), acq_words = HORI_RWORDS + 1.  Only the first
  tail.valid_words * 4 bytes hold samples; the bytes up to acq_bytes are filler.
* tail: u16 id 0xA500 (+ byte 0x5A), per-channel FPGA measurement records at k*0x400, u32 @0x800 =
  valid data words, u32 @0xC00 = trigger word (same layout as the head's u32[2]).
* HW_BASE_MODE 0 (single) and 2 (stream / slow): exactly one frame; packetisation of the USB
  transfers is arbitrary (1048320, 2000000, 1000000, 500160, 100160, 10240, 5120 ... bytes) and a
  tail may be split across transfers.  HW_BASE_MODE 1 (repeat): back-to-back frames, each USB
  transfer = one frame (acq_bytes + 4096); the per-frame trigger is in that frame's tail.

Analog codes: 8-bit, increasing code = increasing voltage, 0 V at the channel's offset code (VOFF,
default 128); 29.375 codes per division (235 codes = 8 div).  Digital: the first DIGITAL_STALE
samples of a capture repeat the previous acquisition's level (decode(..., fill_stale=True)).

Pure numpy; no USB access here.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import struct

import numpy as np

HEAD_BYTES = 4096
TAIL_BYTES = 4096
HEAD_MAGIC = 0x55555555
TAIL_SIG = b"\x00\xa5\x5a"          # first 3 bytes of a tail: u16 0xA500 then 0x5A
TAIL_ID = 0xA500
BLOCK_SAMPLES = 64                   # samples per u64 word / per "mtx" unit
ACQ_ALIGN = 4096                     # UsbBsp3x alignment of the acquisition byte count
BASE_RATE = 100_000_000              # sample rate at fold 1, divider 1
CODES_PER_DIV = 29.375               # 235 codes = 8 divisions

# register addresses (dscope/regs.py names)
R_HW_BASE_MODE = 0x00
R_HORI_BASE = 0x10
R_HORI_RWORDS0, R_HORI_RWORDS1 = 0x13, 0x14
R_HORI_ADIVL, R_HORI_ADIVH, R_HORI_DDIVL, R_HORI_DDIVH = 0x15, 0x16, 0x17, 0x18
R_HORI_AMTXS, R_HORI_DMTXS = 0x19, 0x1A
R_VERT_DFOLD, R_VERT_DEN0 = 0x20, 0x23
R_VERT_AFOLD = 0x28
R_TRIG_PRES0, R_TRIG_PRES1 = 0x31, 0x32
R_ANA_BASE = (0x40, 0x50, 0x60, 0x70)

MODE_SINGLE, MODE_REPEAT, MODE_STREAM = 0, 1, 2

# The first samples of every digital channel in a capture repeat the level the input had at the end
# of the previous acquisition (a digital pipeline that is not flushed).  Observed on all 125 runs:
# 7 samples at dfold 1 (any rate 50 kSa/s..100 MSa/s), 6 at dfold 2, 4 at dfold 4.  Analog has none.
DIGITAL_STALE = {1: 7, 2: 6, 4: 4}


class PacketError(ValueError):
    pass


def _align_up(n, a):
    return -(-n // a) * a


# ----------------------------------------------------------------------------------------------- layout
@dataclass(frozen=True)
class Layout:
    """Everything needed to cut the data stream into channels.

    analog / digital : enabled channel indices, ascending (= order of the u64 words in a block)
    afold / dfold    : interleave factor (1, 2, 4) of the analog / digital front end
    amtxs / dmtxs    : 64-sample units per block for analog / digital (rate ratio after gcd)
    analog_rate / digital_rate : samples per second of each domain (None if unknown)
    hw_mode          : HW_BASE_MODE (0 single, 1 repeat, 2 stream/slow)
    acq_words        : HORI_RWORDS + 1 (32-bit words of sample data per frame), None if unknown
    pretrig          : TRIG_PRES (pretrigger offset in trigger-domain samples), None if unknown
    trig_domain      : 'analog' or 'digital' -- sample domain of trigger positions / pretrig
    """
    analog: tuple = ()
    digital: tuple = ()
    afold: int = 1
    dfold: int = 1
    amtxs: int = 1
    dmtxs: int = 1
    analog_rate: float | None = None
    digital_rate: float | None = None
    hw_mode: int = MODE_SINGLE
    acq_words: int | None = None
    pretrig: int | None = None
    trig_domain: str = "analog"

    # --- block geometry
    @property
    def analog_block_samples(self):
        return BLOCK_SAMPLES * self.amtxs

    @property
    def digital_block_samples(self):
        return BLOCK_SAMPLES * self.dmtxs

    @property
    def analog_block_bytes(self):
        return len(self.analog) * self.analog_block_samples          # 8-bit samples

    @property
    def digital_block_bytes(self):
        return len(self.digital) * self.digital_block_samples // 8   # 1 bit per sample

    @property
    def block_bytes(self):
        return self.analog_block_bytes + self.digital_block_bytes

    @property
    def primary(self):
        """Domain the HAL counts samples in ('MSO: send samples'): the one with more bytes/block."""
        if not self.analog:
            return "digital"
        return "analog" if self.digital_block_bytes <= self.analog_block_bytes else "digital"

    @property
    def primary_block_samples(self):
        return self.analog_block_samples if self.primary == "analog" else self.digital_block_samples

    @property
    def acq_bytes(self):
        """Bytes of the data section of one frame (tail follows at this offset)."""
        if self.acq_words is None:
            return None
        return _align_up(self.acq_words * 4, ACQ_ALIGN)

    @property
    def digital_stale(self):
        """Leading digital samples of a capture that hold a stale level (see DIGITAL_STALE)."""
        return DIGITAL_STALE.get(self.dfold, 7) if self.digital else 0

    @property
    def frame_bytes(self):
        """Repeat mode: bytes of one frame (= one USB transfer) = data + tail."""
        a = self.acq_bytes
        return None if a is None else a + TAIL_BYTES

    def block_samples(self, domain):
        return self.analog_block_samples if domain == "analog" else self.digital_block_samples

    def rate(self, domain):
        return self.analog_rate if domain == "analog" else self.digital_rate


def _fold_reg(v):
    """VERT_AFOLD / VERT_DFOLD = (n_channels << 8) | log2(fold)."""
    return (v >> 8) & 0xFF, 1 << (v & 0xFF)


def layout_from_regs(regs: dict, rates=None, trig_domain=None) -> Layout:
    """Build a Layout from a setup register program ``{addr: value}`` (last write wins).

    rates: optional override, ``{'analog': Hz, 'digital': Hz}`` or ``(analog_Hz, digital_Hz)``;
    by default rates are computed as 100 MSa/s * fold / divider (HORI_ADIV / HORI_DDIV).
    trig_domain: 'analog'/'digital' override; default from HORI_BASE bit0 (1 = analog trigger
    source), else analog when analog channels are enabled.
    """
    g = regs.get
    analog = tuple(i for i, a in enumerate(R_ANA_BASE) if g(a, 0) & 1)
    den = g(R_VERT_DEN0, 0) & 0xFF
    digital = tuple(i for i in range(8) if den >> i & 1)
    na, afold = _fold_reg(g(R_VERT_AFOLD, 0))
    nd, dfold = _fold_reg(g(R_VERT_DFOLD, 0))
    if R_VERT_AFOLD in regs and na != len(analog):
        raise PacketError("VERT_AFOLD says %d analog channels, ANAn_BASE enables %r" % (na, analog))
    if R_VERT_DFOLD in regs and nd != len(digital):
        raise PacketError("VERT_DFOLD says %d digital channels, VERT_DEN0 enables %r" % (nd, digital))
    adiv = g(R_HORI_ADIVL, 1) | g(R_HORI_ADIVH, 0) << 16
    ddiv = g(R_HORI_DDIVL, 1) | g(R_HORI_DDIVH, 0) << 16
    arate = BASE_RATE * afold / adiv if adiv else None
    drate = BASE_RATE * dfold / ddiv if ddiv else None
    if rates is not None:
        if isinstance(rates, dict):
            arate = rates.get("analog", arate)
            drate = rates.get("digital", drate)
        else:
            arate, drate = rates
    if R_HORI_AMTXS in regs or R_HORI_DMTXS in regs:
        amtxs = g(R_HORI_AMTXS, 0) + 1
        dmtxs = g(R_HORI_DMTXS, 0) + 1
    else:
        amtxs, dmtxs = _mtxs(arate, drate, analog, digital)
    acq_words = None
    if R_HORI_RWORDS0 in regs:
        acq_words = (g(R_HORI_RWORDS0, 0) | g(R_HORI_RWORDS1, 0) << 16) + 1
    pretrig = None
    if R_TRIG_PRES0 in regs:
        pretrig = g(R_TRIG_PRES0, 0) | g(R_TRIG_PRES1, 0) << 16
    if trig_domain is None:
        if R_HORI_BASE in regs:
            trig_domain = "analog" if g(R_HORI_BASE) & 1 else "digital"
        else:
            trig_domain = "analog" if analog else "digital"
    return Layout(analog=analog, digital=digital, afold=afold, dfold=dfold, amtxs=amtxs, dmtxs=dmtxs,
                  analog_rate=arate, digital_rate=drate, hw_mode=g(R_HW_BASE_MODE, 0) & 3,
                  acq_words=acq_words, pretrig=pretrig, trig_domain=trig_domain)


def _mtxs(arate, drate, analog, digital):
    """Mso_ComputeGeometry: mtxs = rate / gcd(arate, drate); 1/1 when either domain is empty."""
    if not analog or not digital or not arate or not drate:
        return 1, 1
    a, d = int(round(arate)), int(round(drate))
    gg = math.gcd(a, d)
    return a // gg, d // gg


def make_layout(analog=(), digital=(), analog_rate=None, digital_rate=None, afold=1, dfold=1,
                hw_mode=MODE_SINGLE, acq_words=None, pretrig=None, trig_domain=None) -> Layout:
    """Layout without a register program (block multipliers from the rates)."""
    analog, digital = tuple(sorted(analog)), tuple(sorted(digital))
    amtxs, dmtxs = _mtxs(analog_rate, digital_rate, analog, digital)
    if trig_domain is None:
        trig_domain = "analog" if analog else "digital"
    return Layout(analog=analog, digital=digital, afold=afold, dfold=dfold, amtxs=amtxs, dmtxs=dmtxs,
                  analog_rate=analog_rate, digital_rate=digital_rate, hw_mode=hw_mode,
                  acq_words=acq_words, pretrig=pretrig, trig_domain=trig_domain)


# ------------------------------------------------------------------------------------------ head / tail
def _trig_word(w):
    return {"triggered": not (w >> 31 & 1), "trig_flag30": bool(w >> 30 & 1),
            "trig_pos_raw": (w & 0x3FFFFFFF) if not (w >> 31 & 1) else None,
            "trig_low_bits": w & 0x3FFFFFFF, "trig_word": w}


def parse_head(buf) -> dict:
    """Parse the 4096-byte head packet (MsoPkt_ParseHead).

    Returns triggered, trig_pos_raw (None when not triggered), trig_word (raw u32[2]),
    trig_flag30, trig_low_bits, word1, word3 (raw, meaning unknown).
    """
    if len(buf) < 16:
        raise PacketError("head too short: %d bytes" % len(buf))
    w = struct.unpack_from("<4I", buf, 0)
    if w[0] != HEAD_MAGIC:
        raise PacketError("bad head magic 0x%08x" % w[0])
    d = {"magic": w[0], "word1": w[1], "word3": w[3]}
    d.update(_trig_word(w[2]))
    return d


def is_head(buf) -> bool:
    return len(buf) >= 4 and struct.unpack_from("<I", buf, 0)[0] == HEAD_MAGIC


def is_tail(buf, off=0) -> bool:
    return len(buf) >= off + 4 and bytes(buf[off:off + 3]) == TAIL_SIG


def parse_tail(buf, off=0) -> dict:
    """Parse a 4096-byte tail (MsoPkt_ParseTail).

    valid_words: number of 32-bit data words that hold samples (<= HORI_RWORDS+1, rounded down to
    a multiple of 16).  Trigger fields as in parse_head.  channels: per-analog-input FPGA
    measurement records (flag, min, max, base, top codes).  word_400: raw u16 at +0x400.
    """
    t = bytes(buf[off:off + TAIL_BYTES])
    if len(t) < 0xC04 or not t.startswith(TAIL_SIG):
        raise PacketError("not a tail at offset %d" % off)
    u32 = lambda o: struct.unpack_from("<I", t, o)[0]
    u16 = lambda o: struct.unpack_from("<H", t, o)[0]
    d = {"id": u16(0), "word_400": u16(0x400), "valid_words": u32(0x800)}
    d.update(_trig_word(u32(0xC00)))
    chans = []
    for k in range(4):
        r = 4 + k * 0x400
        chans.append({"flag": t[r] & 1, "min": u16(r + 4), "max": u16(r + 6), "base": u16(r + 8),
                      "top": u16(r + 10)})
    d["channels"] = chans
    return d


def find_tail(data, layout: Layout | None = None, start=0):
    """Offset of the tail in ``data`` (stream after the head), or None.

    With a layout that knows acq_words the tail is at layout.acq_bytes (also tried with 1024-byte
    alignment).  Otherwise every 512-byte boundary is scanned and the candidate is accepted only if
    its valid_words fits just before it.
    """
    mv = _mv(data)
    n = len(mv)
    if layout is not None and layout.acq_words is not None:
        for a in (ACQ_ALIGN, 1024, 512):
            off = start + _align_up(layout.acq_words * 4, a)
            if off + 0xC04 <= n and is_tail(mv, off):
                return off
    for off in range(start + 512, n - 0xC03, 512):
        if mv[off] == 0 and mv[off + 1] == 0xA5 and mv[off + 2] == 0x5A:
            vw = struct.unpack_from("<I", mv, off + 0x800)[0]
            if 0 < vw * 4 <= off - start and off - start - vw * 4 < ACQ_ALIGN + 64:
                return off
    return None


def _mv(data):
    m = memoryview(data)
    if m.ndim != 1 or m.format != "B":
        m = m.cast("B")
    return m


# ----------------------------------------------------------------------------------------------- decode
def _decode_blocks(B: np.ndarray, layout: Layout):
    """B: (nblk, block_bytes) uint8 -> (analog dict, digital dict)."""
    nblk = B.shape[0]
    na, nd = len(layout.analog), len(layout.digital)
    analog, digital = {}, {}
    ab = layout.analog_block_bytes
    if na:
        A = B[:, :ab].reshape(nblk, layout.amtxs * 8, na, 8).transpose(2, 0, 1, 3).reshape(na, -1)
        for i, ch in enumerate(layout.analog):
            analog[ch] = np.array(A[i])              # own (writable) copy, not a view of the input
    if nd:
        D = B[:, ab:].reshape(nblk, layout.dmtxs, nd, 8)
        D = np.unpackbits(D, axis=3, bitorder="little")              # (nblk, dmtxs, nd, 64)
        D = D.transpose(2, 0, 1, 3).reshape(nd, -1)
        for i, ch in enumerate(layout.digital):
            digital[ch] = np.ascontiguousarray(D[i])
    return analog, digital


def decode(data, layout: Layout, nsamples=None, fill_stale=False) -> dict:
    """Decode the stream that follows the head (one frame; tail and padding may be included).

    nsamples: number of samples to keep, counted in the *primary* domain (the HAL's 'MSO: send
    samples' count); the other domain is cut to the same time span.  Default: everything the tail
    marks valid (tail.valid_words), else layout.acq_words, else all complete blocks.
    fill_stale: overwrite the first layout.digital_stale digital samples (stale level from the
    previous acquisition) with the first valid sample; default False = raw data, the count is
    returned as 'digital_stale'.

    Returns dict:
      analog  {ch: uint8 codes},  digital {ch: uint8 0/1}
      analog_rate, digital_rate   -- each dict is at its own rate; when the folds differ
                                     (e.g. 1 analog @400 MSa/s + 8 digital @100 MSa/s) the arrays
                                     have different lengths (ratio = rate ratio = amtxs:dmtxs)
      analog_samples, digital_samples, nblocks, valid_bytes, tail (dict or None), tail_offset,
      primary ('analog'/'digital').
    """
    if layout.block_bytes == 0:
        raise PacketError("layout has no enabled channels")
    mv = _mv(data)
    n = len(mv)
    toff = find_tail(mv, layout)
    tail = parse_tail(mv, toff) if toff is not None else None
    if tail is not None:
        valid = min(tail["valid_words"] * 4, toff)
    elif layout.acq_words is not None:
        valid = min(layout.acq_words * 4, n)
    else:
        valid = n
    nblk = valid // layout.block_bytes
    if nsamples is not None:
        limit = toff if toff is not None else n
        nblk = min(-(-int(nsamples) // layout.primary_block_samples), limit // layout.block_bytes)
    d = np.frombuffer(mv, dtype=np.uint8, count=nblk * layout.block_bytes)
    analog, digital = _decode_blocks(d.reshape(nblk, layout.block_bytes), layout)
    na_s = nblk * layout.analog_block_samples if layout.analog else 0
    nd_s = nblk * layout.digital_block_samples if layout.digital else 0
    if nsamples is not None:
        pb = layout.primary_block_samples
        na_s = min(na_s, int(nsamples) * layout.analog_block_samples // pb)
        nd_s = min(nd_s, int(nsamples) * layout.digital_block_samples // pb)
        analog = {k: v[:na_s] for k, v in analog.items()}
        digital = {k: v[:nd_s] for k, v in digital.items()}
    stale = min(layout.digital_stale, nd_s)
    if fill_stale and stale and nd_s > stale:
        for v in digital.values():
            v[:stale] = v[stale]
    return {"analog": analog, "digital": digital, "analog_rate": layout.analog_rate,
            "digital_rate": layout.digital_rate, "analog_samples": na_s, "digital_samples": nd_s,
            "nblocks": nblk, "valid_bytes": nblk * layout.block_bytes, "tail": tail,
            "tail_offset": toff, "primary": layout.primary, "digital_stale": stale}


# -------------------------------------------------------------------------------------------- triggers
def trigger_index(info: dict, layout: Layout):
    """Stream index of the trigger sample (in layout.trig_domain samples), or None if not triggered.

    info: parse_head() or parse_tail() result.  index = trig_pos_raw + TRIG_PRES, where TRIG_PRES =
    the HAL's m_mso+0xa0 = B * (sample_index // B + 1), B = 64 * mtxs of the trigger domain.
    The sample at that index is the first one at/over the trigger level (verified to 0 samples).
    """
    if not info.get("triggered"):
        return None
    if layout.pretrig is None:
        raise PacketError("layout.pretrig unknown (TRIG_PRES not in the register program)")
    return info["trig_pos_raw"] + layout.pretrig


def pretrigger_offset(sample_index, layout: Layout, domain=None):
    """m_mso+0xa0 / TRIG_PRES for a given :TRIGger:SAMPLEindex (Mso_SetTriggerChannel)."""
    b = layout.block_samples(domain or layout.trig_domain)
    return b * (int(sample_index) // b + 1)


def trigger_window(decoded: dict, layout: Layout, trig_index, sample_index, depth, domain=None) -> dict:
    """Cut every channel to [trig_index - sample_index, + depth) of the trigger domain.

    trig_index: trigger_index(...) result; pass None for an untriggered capture to centre on the
    pretrigger offset (layout.pretrig) instead.  sample_index / depth are in trigger-domain samples
    (:TRIGger:SAMPLEindex, :<domain>:SAMPLE:Depth?).  The other domain is cut to the same time
    span at its own rate.  Returns {'analog': {...}, 'digital': {...}, 'start': {...},
    'length': {...}, 'trig_index': {...}} with per-domain values.
    """
    domain = domain or layout.trig_domain
    if trig_index is None:
        trig_index = layout.pretrig if layout.pretrig is not None else int(sample_index)
    start = int(trig_index) - int(sample_index)
    if start < 0:
        raise PacketError("window starts before the stream (start=%d)" % start)
    out = {"analog": {}, "digital": {}, "start": {}, "length": {}, "trig_index": {}}
    tb = layout.block_samples(domain)
    for dom in ("analog", "digital"):
        chans = decoded[dom]
        if not chans:
            continue
        scale = layout.block_samples(dom) / tb
        s, n, t = int(round(start * scale)), int(round(depth * scale)), int(round(trig_index * scale))
        avail = min(len(v) for v in chans.values())
        if s + n > avail:
            raise PacketError("%s window [%d, %d) exceeds %d decoded samples" % (dom, s, s + n, avail))
        out[dom] = {k: v[s:s + n] for k, v in chans.items()}
        out["start"][dom], out["length"][dom], out["trig_index"][dom] = s, n, t
    return out


# ------------------------------------------------------------------------------------ repeat frames
def split_frames(stream, layout: Layout, complete_only=True):
    """Repeat mode (HW_BASE_MODE 1): split the stream after the head into frames.

    Each frame is acq_bytes of data followed by its 4096-byte tail; the frames are back to back
    (one USB transfer per frame).  Returns a list of memoryviews; a trailing partial frame is
    dropped unless complete_only=False.
    """
    fb = layout.frame_bytes
    if fb is None:
        toff = find_tail(stream, layout)
        if toff is None:
            raise PacketError("cannot find the first tail to size repeat frames")
        fb = toff + TAIL_BYTES
    mv = memoryview(stream)
    out = []
    for off in range(0, len(mv), fb):
        f = mv[off:off + fb]
        if len(f) < fb and complete_only:
            break
        out.append(f)
    return out


def decode_frames(stream, layout: Layout, nsamples=None):
    """Repeat mode: decode every complete frame; each result gets 'trig_index' from its own tail."""
    res = []
    for f in split_frames(stream, layout):
        dec = decode(f, layout, nsamples=nsamples)
        t = dec["tail"]
        dec["trig_index"] = trigger_index(t, layout) if (t is not None and layout.pretrig is not None) else None
        res.append(dec)
    return res


# --------------------------------------------------------------------------------- incremental assembly
class StreamAssembler:
    """Incremental decoder for an acquisition in progress (any packetisation, any mode).

    feed(chunk) takes successive EP 0x86 transfers (any split; the stream starts with the 4096-byte
    head) and returns a list of events:
      {'kind': 'head', 'head': parse_head(...)}
      {'kind': 'samples', 'frame': f, 'block': first block index, 'nblocks': n,
       'analog': {...}, 'digital': {...}}
      {'kind': 'frame', 'frame': f, 'tail': parse_tail(...), 'nblocks': valid blocks of the frame,
       'trig_index': trigger index or None}
      {'kind': 'end'}   (single / stream mode after the tail; later bytes are padding)
    Samples are released as soon as whole blocks arrive, except for the blocks within the last 64
    bytes before acq_words*4 (the tail may mark them invalid); they come with the 'frame' event.
    continuous=True: no tail is expected (loop mode, never observed): every complete block is
    released.  max_frames bounds repeat mode (None = unbounded).  flush() releases what is left
    of an acquisition that was stopped before its tail arrived.  Samples are raw: the first
    layout.digital_stale digital samples of frame 0 hold a stale level (see DIGITAL_STALE).
    """

    def __init__(self, layout: Layout, continuous=False, max_frames=None):
        if layout.block_bytes == 0:
            raise PacketError("layout has no enabled channels")
        if layout.acq_words is None and not continuous:
            raise PacketError("StreamAssembler needs layout.acq_words (HORI_RWORDS)")
        self.layout = layout
        self.continuous = continuous
        self.max_frames = max_frames
        self.head = None
        self.frame = 0
        self.done = False
        self._hbuf = bytearray()     # head bytes until 4096 have arrived
        self._buf = bytearray()      # bytes of the current frame not yet consumed
        self._pos = 0                # frame byte offset of _buf[0]
        self._blk = 0                # next block index to release in this frame
        bb = layout.block_bytes
        if not continuous:
            self._safe_blocks = max(0, (layout.acq_words * 4 - 64) // bb)
            self._acq = layout.acq_bytes

    def feed(self, chunk) -> list:
        ev = []
        if self.done:
            return ev
        chunk = bytes(chunk)
        if self.head is None:
            self._hbuf += chunk
            if len(self._hbuf) >= 4 and not is_head(self._hbuf):
                raise PacketError("stream does not start with a head packet")
            if len(self._hbuf) < HEAD_BYTES:
                return ev
            self.head = parse_head(self._hbuf[:HEAD_BYTES])
            ev.append({"kind": "head", "head": self.head})
            chunk = bytes(self._hbuf[HEAD_BYTES:])
            self._hbuf = bytearray()
        self._buf += chunk
        while not self.done:
            if not self._step(ev):
                break
        return ev

    def flush(self) -> list:
        """Release every complete block received so far (acquisition aborted before its tail)."""
        ev = []
        if self.done or self.head is None:
            return ev
        have = self._pos + len(self._buf)
        limit = have if self.continuous else min(have, self.layout.acq_words * 4)
        self._release(ev, limit // self.layout.block_bytes)
        self._trim()
        return ev

    def _release(self, ev, upto_blocks):
        bb = self.layout.block_bytes
        n = upto_blocks - self._blk
        if n <= 0:
            return
        a = self._blk * bb - self._pos
        B = np.frombuffer(bytes(self._buf[a:a + n * bb]), dtype=np.uint8).reshape(n, bb)
        an, dg = _decode_blocks(B, self.layout)
        ev.append({"kind": "samples", "frame": self.frame, "block": self._blk, "nblocks": n,
                   "analog": an, "digital": dg})
        self._blk = upto_blocks

    def _step(self, ev) -> bool:
        bb = self.layout.block_bytes
        have = self._pos + len(self._buf)            # frame bytes received so far
        if self.continuous:
            self._release(ev, have // bb)
            self._trim()
            return False
        self._release(ev, min(have // bb, self._safe_blocks))
        if have < self._acq + TAIL_BYTES:
            self._trim()
            return False
        t0 = self._acq - self._pos
        tail = parse_tail(self._buf, t0)
        nblk = min(tail["valid_words"] * 4, self._acq) // bb
        self._release(ev, nblk)
        ti = trigger_index(tail, self.layout) if self.layout.pretrig is not None else None
        ev.append({"kind": "frame", "frame": self.frame, "tail": tail, "nblocks": nblk, "trig_index": ti})
        rest = bytes(self._buf[t0 + TAIL_BYTES:])
        self.frame += 1
        self._buf = bytearray()
        self._pos = 0
        self._blk = 0
        if self.layout.hw_mode != MODE_REPEAT or (self.max_frames is not None and self.frame >= self.max_frames):
            self.done = True
            ev.append({"kind": "end"})
            return False
        self._buf += rest
        return bool(rest)

    def _trim(self):
        """Drop consumed bytes (keep the unreleased block remainder and anything from the tail on)."""
        bb = self.layout.block_bytes
        keep_from = self._blk * bb
        if not self.continuous:
            keep_from = min(keep_from, self._acq)
        cut = keep_from - self._pos
        if cut > 0:
            del self._buf[:cut]
            self._pos += cut


# ------------------------------------------------------------------------------------------- helpers
def codes_to_volts(codes, vdiv_uv=1_000_000, voff_code=128):
    """8-bit code -> volts at the probe tip of a 1x probe (vdiv in microvolts per division)."""
    return (np.asarray(codes, dtype=float) - voff_code) * (vdiv_uv / 1e6) / CODES_PER_DIV


def hal_transfer_bytes(layout: Layout) -> int:
    """Bulk transfer size DSWave's HAL uses for the data transfers (m_pkt_bytes; empirical rule
    that reproduces every recorded run).  Any size works -- the stream is byte-continuous -- this
    only mirrors DSWave.  Repeat mode: one frame (acq_bytes + tail) per transfer.  Otherwise
    byte rate > 100 MB/s: floor(1 MiB / block) blocks; slower: ceil(20 ms of data / block) blocks
    (the switch-over lies between 100 and 250 MB/s; only those two rates were recorded)."""
    bb = layout.block_bytes
    if layout.hw_mode == MODE_REPEAT and layout.frame_bytes is not None:
        return layout.frame_bytes
    prate = layout.analog_rate if layout.primary == "analog" else layout.digital_rate
    byte_rate = bb * prate / layout.primary_block_samples
    if byte_rate <= 100e6:
        return -(-int(round(byte_rate * 0.02)) // bb) * bb
    return (0x100000 // bb) * bb
