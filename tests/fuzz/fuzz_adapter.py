"""Atheris target: arbitrary bytes through the raw packet decoder.

Not collected by pytest. Run on Linux:
    python tests/fuzz/fuzz_adapter.py -max_total_time=300
"""
import sys

import atheris

from openm454 import mso_adapter, packet, scope


def _layout():
    s = scope.Settings(analog=['A0', 'A1'], digital=['D0', 'D1'], duration=1e-3, samples=10_000)
    hal, _, _, _ = scope.hal_config(s)
    prog, geom = scope._compile(hal, scope._cached_zero())
    return packet.layout_from_regs(geom['_regs'], rates={'analog': geom['analog_rate'],
                                                         'digital': geom['digital_rate']}), geom, hal


LAYOUT, GEOM, HAL = _layout()


def TestOneInput(data):
    try:
        mso_adapter.decode_bytes(bytes(data), GEOM, HAL, LAYOUT, None)
    except (packet.PacketError, ValueError):
        pass


atheris.Setup(sys.argv, TestOneInput)
atheris.Fuzz()
