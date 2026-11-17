"""Atheris target: arbitrary bytes through packet.split_frames/decode.

Not collected by pytest. Run on Linux:
    python tests/fuzz/fuzz_packet.py -max_total_time=300 tests/fuzz/corpus/fuzz_packet
"""
import sys

import atheris

with atheris.instrument_imports():
    from openm454 import packet, scope


def _layout():
    s = scope.Settings(analog=['A0', 'A1'], digital=['D0', 'D1'], duration=1e-3, samples=10_000, repeat=2)
    hal, _, _, _ = scope.hal_config(s, repeat=scope.repeat_plan(s))
    prog, geom = scope._compile(hal, scope._cached_zero())
    return packet.layout_from_regs(geom['_regs'], rates={'analog': geom['analog_rate'],
                                                         'digital': geom['digital_rate']})


LAYOUT = _layout()


def TestOneInput(data):
    data = bytes(data)
    try:
        frames = packet.split_frames(data, LAYOUT, complete_only=False)
    except packet.PacketError:
        frames = []
    # The split is cheap; the decoder is the interesting surface, so feed it both the split frames and the
    # raw bytes (a frame's tail/head rules live in decode).
    candidates = list(frames[:3]) + [data]
    for f in candidates:
        try:
            packet.decode(f, LAYOUT, nsamples=min(len(f) // 2, 4096))
        except (packet.PacketError, ValueError):
            pass


atheris.Setup(sys.argv, TestOneInput)
atheris.Fuzz()
