"""Shared loop-mode test fakes/builders, moved out of test_logger in Phase 2."""
import numpy as np

from openm454 import logger, packet
from openm454 import scope as scopemod
from openm454.acquire import RawCapture

A2D8 = (['A2', 'A3'], ['D%d' % i for i in range(8)])

def settings(analog, digital):
    st = scopemod.Settings()
    st.analog, st.digital = list(analog), list(digital)
    st.vdiv = {c: 1.0 for c in ('A0', 'A1', 'A2', 'A3')}
    st.offset = {c: 1.5 for c in ('A0', 'A1', 'A2', 'A3')}
    return st


def make_plan(analog, digital, rate, duration, tmp_path=None, **kw):
    kw.setdefault('storage', 'ram' if tmp_path is not None else 'none')
    return logger.plan(settings(analog, digital), rate, duration, out_dir=str(tmp_path) if tmp_path else None,
                       zero_info=scopemod._cached_zero(), **kw)



A4D8 = (['A0', 'A1', 'A2', 'A3'], ['D%d' % i for i in range(8)])

def encode(layout, analog, digital):
    """Inverse of packet._decode_blocks: per-channel codes / bits -> the block stream."""
    n = len(next(iter(analog.values()))) if analog else len(next(iter(digital.values())))
    nb = n // 64
    B = np.zeros((nb, layout.block_bytes), np.uint8)
    ab = layout.analog_block_bytes
    if layout.analog:
        A = B[:, :ab].reshape(nb, 8, len(layout.analog), 8)
        for i, ch in enumerate(layout.analog):
            A[:, :, i, :] = analog[ch][:nb * 64].reshape(nb, 8, 8)
    if layout.digital:
        D = B[:, ab:].reshape(nb, 1, len(layout.digital), 8)
        for i, ch in enumerate(layout.digital):
            D[:, 0, i, :] = np.packbits(digital[ch][:nb * 64].reshape(nb, 64), axis=1, bitorder='little')
    return B.tobytes()


def square(n, period, lo=60, hi=200):
    return np.where((np.arange(n) % period) < period // 2, hi, lo).astype(np.uint8)


class FakeScope:
    """Feeds a synthetic loop stream through on_chunk in transfers of the planned packet size."""

    def __init__(self, stream, pkt, small_blk=None, clock=None, max_transfers=None, pass_len=1):
        self.stream, self.pkt, self.small_blk = stream, pkt, small_blk
        self.clock, self.max_transfers, self.pass_len = clock, max_transfers, pass_len
        self.calls = []
        self.delivered = 0

    def _run(self, prog, geom, trigger_timeout, collect_timeout, **kw):
        self.calls.append((geom, collect_timeout, kw))
        raw = RawCapture()
        head = bytearray(4096)
        head[:4] = b'\x55\x55\x55\x55'
        head[8:12] = (0xC0000007).to_bytes(4, 'little')
        raw.head = bytes(head)
        kw['on_chunk'](raw.head)
        step = self.small_blk or self.pkt
        pos, k = 0, 0
        while True:
            if k % self.pass_len == 0 and kw['stop_flag']():
                raw.stopped = raw.aborted = True
                break
            if self.max_transfers is not None and k >= self.max_transfers:
                break
            d = self.stream[pos % len(self.stream):][:step]
            if len(d) < step:
                d = d + self.stream[:step - len(d)]
            pos += step
            k += 1
            if self.small_blk:
                d = d + bytes(4096 - len(d))                  # one block, then filler
            if self.clock:
                self.clock.tick()
            self.delivered += len(d)
            kw['on_chunk'](d)
            assert not kw.get('keep_data', True)
        raw.stop = 'normal'
        return raw


def fake_signal(pl, seconds):
    rate = pl['rate_hz']
    layout = packet.layout_from_regs(pl['_geom']['_regs'], rates={'analog': rate, 'digital': rate})
    n = int(round(seconds * rate / 64)) * 64
    an = {ch: square(n, 100 + 2 * ch) for ch in layout.analog}
    dg = {ch: (square(n, 50 + ch) > 100).astype(np.uint8) for ch in layout.digital}
    return layout, an, dg, encode(layout, an, dg)


