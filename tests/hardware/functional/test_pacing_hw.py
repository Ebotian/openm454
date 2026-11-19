"""The capture-spacing profile on the real device: the offline suite only validates the keys and floors (`hw`)."""
import time

import pytest

from openm454 import timing
from openm454.errors import DscopeError

pytestmark = pytest.mark.hw

RATE, WINDOW, RUNS = 10e6, 1e-3, 3
FLOOR = 'gap=0.005,quiet=0.005,submit=0.005,setup=0.002,final=0.001'


def _timed(scope, link, runs=RUNS):
    scope.capture(link.settings(WINDOW, RATE))          # warm-up: the first capture of a session pays the front end
    t0 = time.time()
    for _ in range(runs):
        assert scope.capture(link.settings(WINDOW, RATE)).meta['acq']['complete']
    return time.time() - t0


def test_the_floor_profile_shortens_a_capture_on_the_device(bench, scope):
    """Measured on this bench: 186 ms per 1 ms capture with the traced spacing, 32 ms with the floor profile."""
    link = bench.link('awg_to_a0')
    link.program(scope)
    try:
        timing.reset()
        slower = _timed(scope, link)
        assert timing.activate(FLOOR)
        faster = _timed(scope, link)
    finally:
        timing.reset()
    assert faster < 0.6 * slower, 'the floor profile has to cut the per-capture time (%.2f vs %.2f s for %d)' % (
        faster, slower, RUNS)
    cap = scope.capture(link.settings(3e-3, RATE))     # 3 periods: enough for a frequency, unlike the 1 ms timing window
    link.assert_shows_the_declared_signal(cap.measure(link.channel))   # faster, not wrong


def test_a_repeat_run_keeps_the_traced_hold_even_with_the_floor_profile(bench, scope):
    """The untriggered stop of a repeat run always holds the traced 100 ms: quiet=5 ms must not shorten it."""
    link = bench.link('awg_to_a0')
    link.program(scope)
    try:
        timing.activate(FLOOR)
        scope.capture(link.settings(WINDOW, RATE))
        after_single = scope.capture(link.settings(WINDOW, RATE)).meta['acq']['elapsed_ms']
        st = link.settings(WINDOW, RATE)
        st.repeat = 2
        scope.capture(st)
        after_repeat = scope.capture(link.settings(WINDOW, RATE)).meta['acq']['elapsed_ms']
    finally:
        timing.reset()
    # the hold itself is the traced 100 ms; the floor profile only shortens gap/quiet, never an untriggered stop
    assert after_repeat >= 100, 'a capture after a repeat run still pays the 100 ms hold (%.0f ms)' % after_repeat
    assert after_repeat >= after_single + 70, ('the hold after a repeat run is at least the traced 100 ms '
                                               '(%.0f vs %.0f ms)' % (after_repeat, after_single))


def test_a_fixed_key_is_refused_before_the_device_is_touched(bench, scope):
    """start/read are fixed by design: the refusal has to happen offline, and leave the scope idle."""
    with pytest.raises(DscopeError) as e:
        timing.activate('start=0.002')
    assert e.value.code == 'INVALID_ARG'
    assert scope.engine.active is False and scope.dev.t.running is False
    cap = scope.capture(bench.link('awg_to_a0').settings(WINDOW, RATE))
    assert cap.meta['acq']['complete']