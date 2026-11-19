"""Packet geometry on a live capture: what the golden tests read from recordings (`hw`)."""
import pytest

from openm454 import scope as scopemod

pytestmark = pytest.mark.hw


def test_a_live_frame_has_the_planned_depth(bench, scope):
    link = bench.link('awg_to_a0')
    link.program(scope)
    st = link.settings(1e-3, 100e6)
    geom = scopemod.Scope().plan(st)['geometry']
    cap = scope.capture(st)
    assert cap.meta['acq']['analog_rate_hz'] == geom['analog_rate']
    assert cap.analog[link.channel].size == geom['analog_depth'] == cap.meta['acq']['samples']


def test_the_trigger_index_sits_where_the_window_was_asked(bench, scope):
    """trig_pos 50 % of a 2 ms window: every frame of a triggered repeat run has to say so."""
    link = bench.link('awg_to_a0')
    link.program(scope)
    st = link.settings(2e-3, 10e6)
    st.trig_source, st.trig_edge, st.trig_level, st.trig_mode, st.timeout = 'A0', 'rise', 1.5, 'normal', 2.0
    st.repeat = 2
    cap = scope.capture(st)
    depth = cap.meta['acq']['samples']
    for i, f in enumerate(cap.frames):
        assert f['trig_index'] is not None
        assert abs(f['trig_index'] / depth - 0.5) <= 0.02, 'frame %d: trig_index %s of %d samples' % (
            i, f['trig_index'], depth)