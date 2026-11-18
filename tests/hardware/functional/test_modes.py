"""The capture modes on the bench: single, triggered, repeat, stream, log (`hw`)."""
import time

import pytest

pytestmark = pytest.mark.hw

RATE = 10e6
WINDOW = 2e-3


def _triggered(st, level=1.5, timeout=2.0):
    st.trig_source, st.trig_edge, st.trig_level, st.trig_mode, st.timeout = 'A0', 'rise', level, 'normal', timeout
    return st


def test_a_forced_single_capture_is_complete(bench, scope):
    link = bench.link('awg_to_a0')
    link.program(scope)
    cap = scope.capture(link.settings(WINDOW, RATE))
    acq = cap.meta['acq']
    assert acq['complete'] and acq['triggered'] is False
    assert acq['samples'] >= 1024 and acq['duration_s'] == pytest.approx(acq['samples'] / acq['analog_rate_hz'])


@pytest.mark.parametrize('mode', ['normal', 'auto'])
def test_a_triggered_capture_triggers(bench, scope, mode):
    link = bench.link('awg_to_a0')
    link.program(scope)
    st = _triggered(link.settings(WINDOW, RATE))
    st.trig_mode, st.auto_after = mode, 0.05
    cap = scope.capture(st)
    assert cap.meta['acq']['triggered'] is True and cap.meta['acq']['complete']


def test_repeat_forced_collects_every_frame(bench, scope):
    link = bench.link('awg_to_a0')
    link.program(scope)
    st = link.settings(WINDOW, RATE)
    st.repeat = 16
    cap = scope.capture(st)
    assert len(cap.frames) == 16 == cap.meta['acq']['frames']
    assert all(f.get('host_s') is not None for f in cap.frames)


def test_repeat_triggered_collects_every_frame(bench, scope):
    """Every frame is its own acquisition with its own trigger: the indices must differ."""
    link = bench.link('awg_to_a0')
    link.program(scope)
    st = _triggered(link.settings(WINDOW, RATE))
    st.repeat = 8
    cap = scope.capture(st)
    assert len(cap.frames) == 8 and all(f['trig_index'] is not None for f in cap.frames)
    assert len({f['trig_index'] for f in cap.frames}) > 1


def test_back_to_back_captures_leave_the_device_idle(bench, scope):
    """The loop watch runs, without the CLI: every capture hands the device back before the next one starts."""
    link = bench.link('awg_to_a0')
    link.program(scope)
    for _ in range(3):
        cap = scope.capture(link.settings(WINDOW, RATE))
        assert cap.meta['acq']['complete']
        assert scope.dev.t.running is False and scope.engine.active is False
        assert not any(tr.isSubmitted() for tr in scope.engine._trs)
        time.sleep(0.05)


def test_stream_stops_on_request(bench, scope):
    link = bench.link('awg_to_a0')
    link.program(scope)
    t0 = time.time()
    cap = scope.stream(link.settings(5e-3, RATE), every=0.05, stop_flag=lambda: time.time() - t0 > 0.5)
    assert cap.meta['acq']['complete']
    assert scope.dev.t.running is False


def test_log_records_to_ram_and_to_disk(bench, scope, tmp_path):
    link = bench.link('awg_to_a0')
    link.program(scope)
    st = link.settings(1e-3, RATE)
    ram = scope.record(st, RATE, duration=0.3, storage='ram')
    assert ram['ok'] and ram['acq']['mode'] == 'loop'
    disk = scope.record(st, RATE, duration=0.3, storage='disk', out_stem=str(tmp_path / 'log'))
    assert disk['ok'] and disk['files']
    assert any(tmp_path.iterdir())