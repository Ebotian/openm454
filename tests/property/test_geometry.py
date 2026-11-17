"""Hypothesis laws for mso.geometry / the offline plan (no hardware)."""
import pytest
from hypothesis import assume, given, settings, strategies as st

from openm454 import scope
from openm454.errors import DscopeError

ANALOG = st.lists(st.sampled_from(['A0', 'A1', 'A2', 'A3']), unique=True, min_size=1, max_size=4)
DIGITAL = st.lists(st.sampled_from(['D%d' % i for i in range(8)]), unique=True, min_size=1, max_size=8)


@settings(max_examples=20, deadline=None)
@given(analog=ANALOG, digital=DIGITAL, duration=st.floats(1e-6, 0.1), samples=st.integers(1024, 200_000))
def test_program_bounds(analog, digital, duration, samples):
    s = scope.Settings(analog=list(analog), digital=list(digital), duration=duration, samples=samples)
    hal, _, _, _ = scope.hal_config(s)
    prog, geom = scope._compile(hal, scope._cached_zero())
    assert prog
    assert all(0 <= a <= 0xFF and 0 <= v <= 0xFFFF for a, v in prog)
    assert geom['pkt_bytes'] > 0


@settings(max_examples=40, deadline=None)
@given(analog=ANALOG, digital=DIGITAL, duration=st.floats(1e-6, 0.1),
       samples=st.integers(1024, 200_000), repeat=st.integers(0, 3))
def test_geometry_invariants(analog, digital, duration, samples, repeat):
    s = scope.Settings(analog=list(analog), digital=list(digital), duration=duration,
                       samples=samples, repeat=repeat)
    try:
        p = scope.Scope().plan(s)
    except DscopeError:
        assume(False)
        return
    g = p['geometry']
    assert g['hw_mode'] in (0, 1, 2, 3, 6, 7)
    assert g['pkt_bytes'] > 0
    assert g['need_bytes'] > 0
    assert g['blk_bytes'] > 0
    assert g['acqu_bytes'] > 0 and g['acqu_bytes'] % 4096 == 0
    assert 0 <= g['pretrig']
    assert g['record_s'] > 0
    assert bool(g['repeat']) == bool(repeat)
    if repeat:
        assert g['hw_mode'] in (1, 3)
        assert g['pkt_bytes'] % 1024 == 0
