"""Trigger paths on a live signal: level, edge, holdoff, and the run that never triggers (`hw`)."""
import time

import pytest

from openm454.errors import DscopeError

pytestmark = pytest.mark.hw

RATE = 10e6
WINDOW = 2e-3


def _st(link, **kw):
    st = link.settings(WINDOW, RATE)
    for key, value in kw.items():
        setattr(st, key, value)
    return st


@pytest.mark.parametrize('edge', ['rise', 'fall'])
def test_a_level_trigger_fires(bench, scope, edge):
    link = bench.link('awg_to_a0')
    link.program(scope)
    st = _st(link, trig_source='A0', trig_edge=edge, trig_level=1.5, trig_mode='normal', timeout=2.0)
    cap = scope.capture(st)
    assert cap.meta['acq']['triggered'] is True and cap.meta['acq']['complete']


def test_the_holdoff_is_waited_out_and_the_capture_still_fires(bench, scope):
    link = bench.link('awg_to_a0')
    link.program(scope)
    st = _st(link, trig_source='A0', trig_edge='rise', trig_level=1.5, trig_mode='normal', timeout=2.0,
             holdoff=0.3)
    t0 = time.time()
    cap = scope.capture(st)
    assert time.time() - t0 >= 0.3, 'the holdoff has to be waited out before the first trigger'
    assert cap.meta['acq']['triggered'] is True


def test_a_level_the_signal_never_reaches_reports_no_trigger(bench, scope):
    """The error has to be about the trigger, not about the device; and the scope has to be usable afterwards."""
    link = bench.link('awg_to_a0')
    scope.awg_set('square', 1e3, 1.0, 0.0)                 # 0..1 V: the 2.9 V level is never reached
    st = _st(link, trig_source='A0', trig_edge='rise', trig_level=2.9, trig_mode='normal', timeout=0.2)
    with pytest.raises(DscopeError) as e:
        scope.capture(st)
    assert e.value.code == 'TIMEOUT_NO_TRIGGER'
    assert 'did not start' not in (e.value.message or '').lower()
    link.program(scope)                                    # back to the declared signal
    again = scope.capture(link.settings(WINDOW, RATE))
    assert again.meta['acq']['complete'], 'the scope has to be usable again after a no-trigger'