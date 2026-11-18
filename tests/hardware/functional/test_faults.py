"""Faults that are real on a bench: a second run, an overdriven front end, a repeat that runs out of triggers, a
stop while streaming. Each one has to leave the device usable (`hw`)."""
import threading
import time

import pytest

from openm454.errors import DscopeError

pytestmark = pytest.mark.hw

RATE = 10e6
WINDOW = 2e-3


def test_a_second_run_while_one_is_active_is_busy(bench, scope):
    link = bench.link('awg_to_a0')
    link.program(scope)
    st = link.settings(0.05, RATE)
    out = {}

    def first():
        try:
            out['cap'] = scope.capture(st)
        except BaseException as e:                      # noqa: BLE001 - reported through the assertions below
            out['err'] = e

    thread = threading.Thread(target=first)
    thread.start()
    deadline = time.time() + 2.0
    while not scope.engine.active and time.time() < deadline:
        time.sleep(0.005)
    assert scope.engine.active, 'the first capture should be running'
    with pytest.raises(DscopeError) as e:
        scope.capture(st)
    assert e.value.code == 'BUSY'
    thread.join(10)
    assert 'cap' in out and out['cap'].meta['acq']['complete']


def test_an_overdriven_input_is_reported_and_recovers(bench, scope):
    link = bench.link('awg_to_a0')
    link.program(scope)
    st = link.settings(WINDOW, RATE)
    st.vdiv['A0'] = 0.2                                # 3 V into +/-0.8 V: both ends clip
    hot = scope.capture(st)
    assert any('clips' in h for h in hot.meta['hints']), hot.meta['hints']
    back = scope.capture(link.settings(WINDOW, RATE))
    assert not any('clips' in h for h in back.meta['hints']), back.meta['hints']
    link.assert_shows_the_declared_signal(back.measure(link.channel))


def test_a_repeat_run_without_a_trigger_says_the_frames_never_came(bench, scope):
    """A repeat run whose level is never reached must report the trigger, not the device, and recover.

    A *short* repeat ("collected k of N") needs a source that fires a few frames and then goes quiet for longer than
    the timeout. This bench has one continuous generator and it cannot be reprogrammed while a run is armed (the
    stop handshake owns the link), so that path stays offline — tests/stateful/test_repeat_plumbing.py drives it
    with a scripted engine."""
    link = bench.link('awg_to_a0')
    scope.awg_set('square', 1e3, 1.0, 0.0)                 # 0..1 V: the 1.5 V trigger level is never reached
    st = link.settings(WINDOW, RATE)
    st.repeat = 4
    st.trig_source, st.trig_edge, st.trig_level, st.trig_mode, st.timeout = 'A0', 'rise', 1.5, 'normal', 0.4
    with pytest.raises(DscopeError) as e:
        scope.capture(st)
    assert e.value.code == 'TIMEOUT_NO_TRIGGER'
    assert 'repeat mode' in (e.value.message or '') and 'no frame arrived' in (e.value.message or '')
    link.program(scope)
    again = scope.capture(link.settings(WINDOW, RATE))
    assert again.meta['acq']['complete'], 'the scope has to be usable again after a repeat that never started'


def test_a_stream_stop_leaves_the_device_released(bench, scope):
    link = bench.link('awg_to_a0')
    link.program(scope)
    t0 = time.time()
    cap = scope.stream(link.settings(5e-3, RATE), every=0.05, stop_flag=lambda: time.time() - t0 > 0.5)
    assert cap.meta['acq']['complete']
    assert scope.dev.t.running is False and scope.engine.active is False
    assert not any(tr.isSubmitted() for tr in scope.engine._trs)
    again = scope.capture(link.settings(WINDOW, RATE))
    assert again.meta['acq']['complete']