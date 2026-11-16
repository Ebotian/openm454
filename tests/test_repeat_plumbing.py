"""Repeat mode exposure: settings -> hal_config -> capture, without a device.

The packet-level frame layout (tests/test_packet_golden.py::test_repeat_mode_frames) and the engine path
(tests/test_acquire_sim.py) are covered elsewhere; what these pin is the wiring that made the mode unreachable:
`hal['repeat']` used to be a hard 0 in hal_config, so nothing could turn it on.
"""
import pytest

from openm454 import scope
from openm454.errors import DscopeError


def test_repeat_is_off_by_default_and_that_is_what_hal_config_says():
    st = scope.Settings()
    assert st.repeat == 0
    hal, _, _, _ = scope.hal_config(st)
    assert hal['repeat'] == 0


def test_a_repeat_setting_reaches_the_hal_and_the_geometry():
    st = scope.Settings(repeat=8)
    hal, _, _, _ = scope.hal_config(st)
    assert hal['repeat'] == 1
    # the geometry has to switch too: repeat transfers carry a 4 KiB tail per frame
    prog, geom = scope._compile(hal, scope._cached_zero())
    assert geom['repeat'] is True
    assert geom['pkt_bytes'] > 0 and geom['pkt_bytes'] % 1024 == 0


@pytest.mark.parametrize('trigger, mode', [(None, 'force'), ('A0', 'force'), (None, 'auto')])
def test_repeat_accepts_a_forced_or_untriggered_start(trigger, mode):
    st = scope.Settings(repeat=3, trig_source=trigger, trig_mode=mode)
    assert scope.repeat_plan(st) == 3


def test_a_triggered_start_is_allowed_and_stays_mode_1_above_200ms():
    """Measured 2026-11-16: a 2 Hz source with a rise trigger gives three 250 ms frames in 1.75 s, each a separate
    acquisition - the device re-arms for every frame. A triggered start keeps HW_BASE_MODE 1 at any window; only the
    untriggered form switches to the stream (mode 3) above 200 ms."""
    st = scope.Settings(repeat=3, trig_source='A0', trig_mode='normal', trig_level=1.4,
                        duration=0.25, samples=250_000, analog=['A0'], digital=[])
    assert scope.repeat_plan(st) == 3
    hal, _, _, _ = scope.hal_config(st)
    hal['trigger']['enable'] = 1
    prog, geom = scope._compile(hal, scope._cached_zero())
    assert geom['hw_mode'] == 1 and geom['roll'] is False


def test_it_is_the_untriggered_form_that_becomes_the_stream_above_200ms():
    st = scope.Settings(repeat=3, duration=0.25, samples=250_000, analog=['A0'], digital=[])
    hal, _, _, _ = scope.hal_config(st)
    hal['trigger']['enable'] = 0
    prog, geom = scope._compile(hal, scope._cached_zero())
    assert geom['hw_mode'] == 3 and geom['roll'] is True


def test_a_negative_frame_count_is_refused():
    with pytest.raises(DscopeError) as e:
        scope.repeat_plan(scope.Settings(repeat=-1))
    assert e.value.code == 'INVALID_ARG'


def test_off_still_plans_a_single_capture():
    assert scope.repeat_plan(scope.Settings()) == 0


def test_the_cli_flag_reaches_the_settings():
    from openm454.cli import build_parser
    a = build_parser().parse_args(['capture', '--repeat', '5'])
    st = scope.settings_from_args(a)
    assert st.repeat == 5


def test_the_default_command_line_still_plans_a_single_capture():
    from openm454.cli import build_parser
    a = build_parser().parse_args(['capture'])
    assert scope.repeat_plan(scope.settings_from_args(a)) == 0


# ---------------------------------------------------------------- the guards (what repeat mode must refuse)

def _plan(**kw):
    st = scope.Settings(analog=['A0'], digital=[], **kw)
    return scope.Scope().plan(st)


def test_a_window_above_200ms_is_refused_because_the_device_switches_to_mode_3():
    """DSWave switches to HW_BASE_MODE 3 there too, and in that mode the per-frame tail stops describing the
    record (measured 2026-11-16, docs/PROTOCOL.md 8.2): repeat must not silently produce mis-framed frames."""
    with pytest.raises(DscopeError) as e:
        _plan(repeat=3, duration=0.25, samples=250_000)
    assert e.value.code == 'INVALID_ARG'
    assert 'MODE 3' in e.value.message and '0.2 s' in (e.value.hint or '')


def test_a_window_at_the_200ms_threshold_is_accepted():
    geom = _plan(repeat=3, duration=0.2, samples=200_000)['geometry']
    assert geom['roll'] is False and geom['hw_mode'] == 1


def test_the_same_window_without_repeat_is_accepted():
    geom = _plan(duration=0.25, samples=250_000)['geometry']
    assert geom['repeat'] is False


def test_a_frame_count_that_cannot_be_held_in_memory_is_refused():
    # 200 ms at 400 MSa/s (1 channel) is ~160 kB per frame: 100000 frames is 16 GB
    with pytest.raises(DscopeError) as e:
        _plan(repeat=100_000, duration=0.2, samples=80_000_000)
    assert e.value.code == 'INVALID_ARG'
    assert 'MiB' in e.value.message


def test_the_watchdog_budget_grows_with_the_frame_count():
    """The run's wall-clock budget has to know about the frame count: one record per frame is the floor."""
    from openm454.cli import _budget, build_parser

    def per(n):
        a = build_parser().parse_args(['capture', '-c', 'A0', '--duration', '200ms', '--samples', '100k',
                                       '--repeat', str(n)])
        return _budget(a)[0]
    one, many = per(1), per(2000)
    assert one == 60.0                                  # the floor for a single capture
    assert many > 2000 * 0.2                            # at least one 200 ms record per frame


def test_repeat_without_a_head_raises_instead_of_running_the_forced_fallback():
    """The fallback re-runs the program without repeat_frames, then polls progress in a mode that does not poll."""
    import types
    calls = []

    class Raw:
        head = None
        aborted = False
        frames = []
        data = b''
        stop = None

    s = scope.Scope()
    s._check_link = lambda: None
    s._run = lambda prog, geom, tt, ct, **kw: (calls.append(kw), Raw())[1]
    st = scope.Settings(analog=['A0'], digital=[], duration=0.01, samples=1000, repeat=3)
    with pytest.raises(DscopeError) as e:
        s.capture(st)
    assert e.value.code == 'PROTOCOL_ERROR'
    assert len(calls) == 1 and calls[0].get('repeat_frames') == 3


def test_the_repeat_program_is_mode_1_with_the_trigger_off():
    st = scope.Settings(analog=['A0'], digital=[], duration=0.1, samples=100_000, repeat=2)
    hal, _, _, _ = scope.hal_config(st)
    hal['trigger']['enable'] = 0
    prog, geom = scope._compile(hal, scope._cached_zero())
    assert geom['hw_mode'] == 1 and geom['roll'] is False
    mode = [v for a, v in prog if a == 0x00]
    assert mode == [1]                        # repeat bit only: no stream, no loop
    assert [v for a, v in prog if a == 0x30] == [0x0002]   # auto trigger, trigger disabled


def test_each_frame_is_decoded_once_and_only_frame_0_inherits_stale_levels(monkeypatch):
    """Frames after the first follow their own predecessor, so fill_stale (the stale level of the previous
    acquisition) belongs to frame 0 only; and decoding every frame twice was pure waste."""
    import numpy as np
    from openm454 import mso_adapter, packet

    calls = []

    def fake_decode(data, layout, nsamples=None, fill_stale=False):
        calls.append(fill_stale)
        return {'analog': {0: np.zeros(8, np.uint8)}, 'digital': {}, 'tail': None, 'nblocks': 1,
                'analog_samples': 8, 'digital_samples': 0}

    monkeypatch.setattr(packet, 'decode', fake_decode)
    monkeypatch.setattr(mso_adapter, '_layout', lambda geom: object())
    monkeypatch.setattr(mso_adapter, '_decode_bytes',
                        lambda data, geom, hal, layout, trig, dec=None: {'analog': {}, 'digital': {}})

    import types
    raw = types.SimpleNamespace(frames=[b'\x00' * 64, b'\x01' * 64])
    out = mso_adapter.decode_frames(raw, {}, {})
    assert len(out) == 2
    assert calls == [True, False]
