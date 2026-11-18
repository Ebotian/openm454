"""Rate ceilings, the 1024-sample floor, and a refusal that has to happen before the device is opened (`hw`)."""
import pytest

from openm454 import scope as scopemod
from openm454.errors import DscopeError

pytestmark = pytest.mark.hw

WINDOW = 10e-6


@pytest.mark.parametrize('channels,rate', [
    (['A0'], 400e6),
    (['A0', 'A1'], 200e6),
    (['A0', 'A1', 'A2'], 100e6),
])
def test_the_advertised_rate_reaches_the_capture(bench, scope, channels, rate):
    link = bench.link('awg_to_a0')
    st = link.settings(WINDOW, rate)
    st.analog, st.digital = list(channels), []
    planned = scopemod.Scope().plan(st)['geometry']['analog_rate']
    cap = scope.capture(st)
    assert cap.meta['acq']['analog_rate_hz'] == planned
    assert cap.meta['acq']['samples'] >= 1024 and cap.meta['acq']['complete']


def test_the_sample_floor_is_1024(bench, scope):
    link = bench.link('awg_to_a0')
    st = link.settings(1e-3, 1e6)
    st.samples = 1000
    assert scopemod.Scope().plan(st)['geometry']['analog_depth'] == 1024
    cap = scope.capture(st)
    assert cap.meta['acq']['samples'] == 1024


def test_a_rate_above_the_ceiling_is_clamped_to_it(bench, scope):
    """500 MSa/s on one channel is above the 400 MSa/s ceiling: the plan clamps it, and the capture reports what the
    device really ran at (the number a measurement then divides by)."""
    link = bench.link('awg_to_a0')
    st = link.settings(1e-3, 500e6)
    assert scopemod.Scope().plan(st)['geometry']['analog_rate'] == 400e6
    cap = scope.capture(st)
    assert cap.meta['acq']['analog_rate_hz'] == 400e6 and cap.meta['acq']['complete']