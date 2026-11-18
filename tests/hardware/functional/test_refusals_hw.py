"""Refusals the offline suite checks must happen before the device is touched (`hw`)."""
import pytest

from openm454 import scope as scopemod
from openm454.errors import DscopeError

pytestmark = pytest.mark.hw


@pytest.mark.parametrize('mode', ['stream', 'log'])
def test_a_mode_that_cannot_do_repeat_refuses_without_the_device(bench, scope, mode):
    st = scopemod.Settings(analog=['A0'], digital=[], duration=1e-3, samples=1000, repeat=2)
    with pytest.raises(DscopeError) as e:
        if mode == 'stream':
            scope.stream(st)
        else:
            scope.record(st, 1e6, duration=0.1, storage='none')
    assert e.value.code == 'INVALID_ARG'
    assert scope.engine.active is False and scope.dev.t.running is False
    cap = scope.capture(bench.link('awg_to_a0').settings(1e-3, 10e6))
    assert cap.meta['acq']['complete'], 'the device has to be untouched by a refused plan'


def test_a_negative_repeat_is_refused_before_the_device(bench, scope):
    with pytest.raises(DscopeError) as e:
        scopemod.repeat_plan(scopemod.Settings(repeat=-1))
    assert e.value.code == 'INVALID_ARG'
    assert scope.engine.active is False