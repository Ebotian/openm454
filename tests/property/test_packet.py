"""Packet layout and repeat-frame splitting laws (no hardware)."""
import pytest
from hypothesis import given, settings, strategies as st

from openm454 import packet, scope


def _layout():
    s = scope.Settings(analog=['A0', 'A1'], digital=['D0', 'D1'], duration=1e-3, samples=10_000, repeat=2)
    hal, _, _, _ = scope.hal_config(s, repeat=scope.repeat_plan(s))
    prog, geom = scope._compile(hal, scope._cached_zero())
    return packet.layout_from_regs(geom['_regs'], rates={'analog': geom['analog_rate'],
                                                         'digital': geom['digital_rate']})


def test_layout_channels_and_rates():
    layout = _layout()
    assert layout.analog == (0, 1)
    assert layout.digital == (0, 1)
    assert layout.analog_rate > 0 and layout.digital_rate > 0
    assert layout.hw_mode in (1, 3)
    assert layout.acq_words and layout.acq_words > 0

