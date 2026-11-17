"""Atheris target: arbitrary bytes through packet.split_frames/decode.

Not collected by pytest. Run on Linux:
    python tests/fuzz/fuzz_packet.py -max_total_time=300
"""
import sys

import atheris

from openm454 import packet, scope


def _layout():
    s = scope.Settings(analog=['A0', 'A1'], digital=['D0', 'D1'], duration=1e-3, samples=10_000, repeat=2)
    hal, _, _, _ = scope.hal_config(s, repeat=scope.repeat_plan(s))
    prog, geom = scope._compile(hal, scope._cached_zero())
    return packet.layout_from_regs(geom['_regs'], rates={'analog': geom['analog_rate'],
                                                         'digital': geom['digital_rate']})


LAYOUT = _layout()


def TestOneInput(data):
    try:
        frames = packet.split_frames(bytes(data), LAYOUT)
    except packet.PacketError:
        return
    for f in frames:
        try:
            packet.decode(f, LAYOUT, nsamples=min(len(f) // 2, 4096))
        except (packet.PacketError, ValueError):
            pass


atheris.Setup(sys.argv, TestOneInput)
atheris.Fuzz()
