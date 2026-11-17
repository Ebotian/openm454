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
    hal, _, _, _ = scope.hal_config(st, repeat=scope.repeat_plan(st))
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
    hal, _, _, _ = scope.hal_config(st, repeat=3)
    hal['trigger']['enable'] = 1
    prog, geom = scope._compile(hal, scope._cached_zero())
    assert geom['hw_mode'] == 1 and geom['roll'] is False


def test_it_is_the_untriggered_form_that_becomes_the_stream_above_200ms():
    st = scope.Settings(repeat=3, duration=0.25, samples=250_000, analog=['A0'], digital=[])
    hal, _, _, _ = scope.hal_config(st, repeat=3)
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
    hal, _, _, _ = scope.hal_config(st, repeat=2)
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


# ---------------------------------------------------------------- the default program, and every frame on disk

V0_CASES = {
    'two_analog_one_digital': (dict(analog=['A0', 'A1'], digital=['D0'], duration=1e-3, samples=100_000),
                               'a021f0b1ac71ea45', 1089, 0),
    'one_analog_400msa': (dict(analog=['A0'], digital=[], duration=1e-3, samples=400_000),
                          'a484fbb7e55fb843', 1089, 0),
    'all_channels': (dict(analog=['A0', 'A1', 'A2', 'A3'], digital=['D%d' % i for i in range(8)],
                          duration=10e-3, samples=100_000), 'f0c912eef49e5f14', 1089, 2),
    'triggered': (dict(analog=['A0'], digital=[], duration=1e-3, samples=50_000,
                       trig_source='A0', trig_mode='normal', trig_level=1.4), '01c51a7a1fb7b60e', 1103, 0),
}


@pytest.mark.parametrize('name', sorted(V0_CASES))
def test_the_program_without_repeat_is_unchanged(name):
    """Hashed against v0.3.0 (origin/main) with a synthetic calibration, so the expected hash does not depend on a
    unit: repeat=0 must keep the register program byte for byte, including the write count and HW_BASE_MODE."""
    import hashlib
    from openm454 import mso
    kw, want_hash, want_writes, want_mode = V0_CASES[name]
    st = scope.Settings(**kw)
    hal, _, _, _ = scope.hal_config(st)
    hal['trigger']['enable'] = int(bool(st.trig_source) and st.trig_mode != 'force')
    prog, geom = scope._compile(hal, [mso.ProbeCal(500, 0, 0, [])] * 4)
    h = hashlib.sha256(repr([tuple(w) for w in prog]).encode()).hexdigest()[:16]
    assert (h, len(prog), geom['hw_mode']) == (want_hash, want_writes, want_mode)


def test_a_repeat_capture_returns_every_decoded_frame(monkeypatch):
    """The capture path itself, not only hal_config: every frame, its trigger index, and the counts in the meta."""
    import numpy as np
    from openm454 import scope as scopemod

    frames = [{'analog': {'A0': np.full(8, i, np.uint8)}, 'digital': {}, 't0_analog': 0.0, 't0_digital': 0.0,
               'trig_index': 100 + i} for i in range(3)]

    class Raw:
        head = object()
        aborted = False
        frames = [b'x'] * 3
        data = b''
        stop = 'normal'
        triggered = False
        handshakes = []
        transfers = 2
        transfers_wanted = 2

    s = scopemod.Scope()
    s._check_link = lambda: None
    s._run = lambda prog, geom, tt, ct, **kw: Raw()
    s._check_complete = lambda raw, geom: True
    monkeypatch.setattr(scopemod, '_decode_frames', lambda raw, geom, hal: frames)

    cap = s.capture(scope.Settings(analog=['A0'], digital=[], duration=0.01, samples=1000, repeat=3))
    assert cap.meta['acq']['repeat_frames'] == 3 and cap.meta['acq']['frames'] == 3
    assert [f['trig_index'] for f in cap.frames] == [100, 101, 102]
    assert cap.analog['A0'].shape == (8,)          # frame 0 stays the top-level array


def test_a_repeat_capture_saves_every_frame(tmp_path):
    import numpy as np
    from openm454.capture import Capture, load_capture

    frames = [{'analog': {'A0': np.full(8, i, np.uint8)},
               'digital': {'D0': np.tile(np.array([1, 0], np.uint8), 4)}, 'trig_index': 200 + i} for i in range(3)]
    meta = {'channels': {'A0': {'zero_code': 128, 'volts_per_code': 0.034, 'probe': 1}, 'D0': {}},
            'analog_rate_hz': 1e6, 'digital_rate_hz': 1e6, 't0_analog_s': 0.0, 't0_digital_s': 0.0,
            'acq': {'repeat_frames': 3, 'frames': 3}, 'frames': 3}
    cap = Capture(meta, frames[0]['analog'], frames[0]['digital'])
    cap.frames = frames
    path = cap.save(str(tmp_path / 'r.npz'))

    z = np.load(path)
    assert z['frames_analog_A0'].shape == (3, 8) and z['frames_digital_D0'].shape == (3, 1)
    back = load_capture(path)
    assert len(back.frames) == 3
    assert int(back.frames[2]['analog']['A0'][0]) == 2
    assert [f['trig_index'] for f in back.frames] == [200, 201, 202]
    assert back.meta['frames_samples'] == {'A0': 8, 'D0': 8}


# ---------------------------------------------------------------- review fixes (2026-11-17)

class _Raw:
    """A finished repeat run as the engine returns it (only the fields capture() reads)."""
    def __init__(self, frames=3, head=True, aborted=False, triggered=True):
        self.head = b'h' if head else None
        self.aborted = aborted
        self.timed_out = False
        self.frames = [b'x'] * frames
        self.data = b''
        self.stop = 'normal'
        self.triggered = triggered
        self.handshakes = []
        self.transfers = 2
        self.transfers_wanted = 2
        self.t_start = self.t_head = None


def _scope_with(raw, calls=None):
    s = scope.Scope()
    s._check_link = lambda: None

    def run(prog, geom, tt, ct, **kw):
        if calls is not None:
            calls.append(dict(kw, trigger_timeout=tt, collect_timeout=ct, geom=geom))
        return raw
    s._run = run
    return s


def _frames(n, ch='A0', digital=None):
    import numpy as np
    out = []
    for i in range(n):
        f = {'analog': {ch: np.full(16, 100 + i, np.uint8)} if ch else {}, 'digital': {}, 't0_analog': 0.0,
             't0_digital': 0.0, 'trig_index': 500 + i}
        if digital:
            f['digital'][digital] = np.tile(np.array([0, 1], np.uint8), 8)
        out.append(f)
    return out


def _st(**kw):
    base = dict(analog=['A0'], digital=[], duration=0.01, samples=10_000)
    base.update(kw)
    return scope.Settings(**base)


# 1. a digital-edge trigger in repeat mode: never re-armed, reported unchecked with the reason

def test_a_digital_edge_trigger_in_repeat_mode_is_not_rearmed(monkeypatch):
    """The misfire re-arm re-ran the repeat program without repeat_frames (byte mode with progress polls) and kept
    the rejected run's frames next to the re-run's arrays: in repeat mode nothing is re-armed."""
    from openm454 import trigcheck
    frames = _frames(3, ch=None, digital='D5')
    monkeypatch.setattr(scope, '_decode_frames', lambda raw, geom, hal: frames)
    monkeypatch.setattr(scope, '_decode', lambda raw, geom, hal: pytest.fail('repeat mode decoded a re-run'))
    monkeypatch.setattr(trigcheck, 'edge_check', lambda *a, **k: 'mismatch')      # frame 0 would be a misfire
    calls = []
    s = _scope_with(_Raw(), calls)
    st = _st(analog=[], digital=['D5'], trig_source='D5', trig_edge='fall', trig_mode='normal', repeat=3)
    cap = s.capture(st)
    acq = cap.meta['acq']
    assert len(calls) == 1 and calls[0]['repeat_frames'] == 3
    assert acq['trigger_check'] == 'unchecked' and acq['trigger_misfires'] == 0
    assert 'repeat mode' in acq['trigger_check_reason']
    assert acq['attempts'] == ['triggered']
    assert cap.frames is frames and cap.digital['D5'] is frames[0]['digital']['D5']
    assert any('repeat mode' in h and 'misfire' in h for h in cap.meta['hints'])


# 2. the engine deadline grows with N x (record + floor) and, triggered, with the trigger wait per frame

def test_the_collect_deadline_covers_every_untriggered_frame(monkeypatch):
    monkeypatch.setattr(scope, '_decode_frames', lambda raw, geom, hal: _frames(100))
    calls = []
    s = _scope_with(_Raw(frames=100, triggered=False), calls)
    s.capture(_st(duration=0.2, samples=20_000, trig_mode='force', repeat=100))
    geom = calls[0]['geom']
    ct = calls[0]['collect_timeout']
    assert ct >= 100 * (geom['record_s'] + scope.REPEAT_FRAME_FLOOR_S)        # was 10 s for >= 20 s of frames
    assert ct == pytest.approx(scope.repeat_collect_timeout(geom, 100))
    assert calls[0]['trigger_timeout'] == pytest.approx(scope.Scope._head_wait(geom, 2.0))


def test_the_collect_deadline_of_a_triggered_run_adds_a_trigger_wait_per_frame(monkeypatch):
    monkeypatch.setattr(scope, '_decode_frames', lambda raw, geom, hal: _frames(40))
    calls = []
    s = _scope_with(_Raw(frames=40), calls)
    s.capture(_st(duration=0.05, samples=50_000, trig_source='A0', trig_level=1.4, trig_mode='normal', timeout=5.0,
                  holdoff=0.01, repeat=40))
    geom, ct = calls[0]['geom'], calls[0]['collect_timeout']
    assert ct >= 39 * (5.0 + 0.01 + geom['record_s'])                          # (N - 1) x (trigger wait + record)
    assert ct == pytest.approx(scope.repeat_collect_timeout(geom, 40, 5.01))


def test_collect_timeout_values():
    geom = {'record_s': 0.2, 'pkt_bytes': 20480}
    one = 0.2 + scope.REPEAT_FRAME_FLOOR_S + 20480 / scope.REPEAT_READOUT_BPS
    assert scope.repeat_collect_timeout(geom, 0) == 10.0                       # a single capture: unchanged
    assert scope.repeat_collect_timeout(geom, 2) == 10.0                       # never below the single deadline
    assert scope.repeat_collect_timeout(geom, 100) == pytest.approx(1.5 * 100 * one + 2.0)
    assert scope.repeat_collect_timeout(geom, 100, 5.0) == pytest.approx(1.5 * 100 * one + 2.0 + 99 * 5.0)
    assert scope.repeat_collect_timeout(geom, 3000, 5.0) == scope.REPEAT_MAX_S  # the upper bound
    small = {'record_s': 5.12e-6, 'pkt_bytes': 8192}                          # 5 us at 400 MSa/s, 2000 frames
    assert scope.repeat_collect_timeout(small, 2000) == 10.0


def test_a_run_whose_frames_alone_exceed_the_bound_is_refused_offline():
    with pytest.raises(DscopeError) as e:
        _plan(duration=0.2, samples=1024, trig_mode='force', repeat=20_000)
    assert e.value.code == 'INVALID_ARG' and '3600 s' in e.value.message


@pytest.mark.parametrize('triggered', [False, True])
def test_a_frame_shortfall_says_how_the_frames_stopped(monkeypatch, triggered):
    monkeypatch.setattr(scope, '_decode_frames', lambda raw, geom, hal: _frames(2))
    s = _scope_with(_Raw(frames=2, triggered=triggered))
    kw = dict(trig_source='A0', trig_level=1.4, trig_mode='normal') if triggered else dict(trig_mode='force')
    with pytest.raises(DscopeError) as e:
        s.capture(_st(repeat=5, **kw))
    assert '2 of 5 frames' in e.value.message and 'USB' not in (e.value.hint or '')
    if triggered:
        assert e.value.code == 'TIMEOUT_NO_TRIGGER' and e.value.exit_code == 5
        assert 'later triggers' in e.value.message and '--timeout' in e.value.hint
    else:
        assert e.value.code == 'TIMEOUT_DATA' and 'stopped arriving' in e.value.message


# 3. a triggered start whose trigger never comes is TIMEOUT_NO_TRIGGER, as for a single capture

def test_a_triggered_repeat_without_a_trigger_is_timeout_no_trigger():
    calls = []
    s = _scope_with(_Raw(head=False), calls)
    with pytest.raises(DscopeError) as e:
        s.capture(_st(trig_source='A0', trig_level=1.4, trig_mode='normal', timeout=0.3, repeat=3))
    assert e.value.code == 'TIMEOUT_NO_TRIGGER' and e.value.exit_code == 5
    assert 'replug' not in (e.value.hint or '') and 'force' in e.value.hint
    assert len(calls) == 1                                  # no forced fallback run


def test_a_triggered_repeat_start_in_auto_mode_is_refused_before_the_device_is_opened():
    from openm454.cli import _budget, build_parser
    a = build_parser().parse_args(['capture', '-c', 'A0', '--repeat', '3', '--trig', 'A0:rise:1.4V',
                                   '--duration', '250ms'])
    with pytest.raises(DscopeError) as e:
        _budget(a)
    assert e.value.code == 'INVALID_ARG' and 'MODE 3' not in e.value.message
    assert '--trig-mode normal' in e.value.hint
    with pytest.raises(DscopeError):                         # the API refuses it too, before anything is sent
        _scope_with(_Raw()).capture(_st(trig_source='A0', trig_level=1.4, repeat=3))


def test_a_triggered_normal_repeat_above_200ms_passes_the_offline_checks():
    from openm454.cli import _budget, build_parser
    a = build_parser().parse_args(['capture', '-c', 'A0', '--repeat', '3', '--trig', 'A0:rise:1.4V',
                                   '--trig-mode', 'normal', '--duration', '250ms'])
    assert _budget(a)[0] >= 60.0


# 4. the watchdog: the engine's own deadlines plus a margin, a trigger wait only for a triggered start

def _budget_of(*argv):
    from openm454.cli import _budget, build_parser
    return _budget(build_parser().parse_args(['capture', '-c', 'A0'] + list(argv)))[0]


def test_an_untriggered_repeat_is_not_charged_a_trigger_wait_per_frame():
    # 0.45 s of work on the hardware; the old budget charged 2 s per frame (4056 s)
    assert _budget_of('--rate', '400M', '--duration', '5us', '--repeat', '2000', '--trig-mode', 'force') == 60.0
    assert _budget_of('--rate', '400M', '--duration', '5us', '--repeat', '2000') == 60.0


@pytest.mark.parametrize('argv, n, triggered', [
    (['--duration', '200ms', '--samples', '20k', '--repeat', '100', '--trig-mode', 'force'], 100, False),
    (['--duration', '50ms', '--samples', '50k', '--repeat', '40', '--trig', 'A0:rise:1.4V', '--trig-mode', 'normal',
      '--timeout', '5s'], 40, True),
])
def test_the_watchdog_covers_the_engine_deadlines(argv, n, triggered):
    from openm454.cli import build_parser
    st = scope.settings_from_args(build_parser().parse_args(['capture', '-c', 'A0'] + argv))
    geom = scope.Scope().plan(st)['geometry']
    wait = st.timeout if triggered else 2.0
    engine = scope.Scope._head_wait(geom, wait) + scope.repeat_collect_timeout(geom, n, wait if triggered else None)
    budget = _budget_of(*argv)
    assert budget >= engine + 30.0                       # the engine gives up first, with a margin
    assert budget <= engine + 60.0 + 1e-6 * n * geom['pkt_bytes']


def test_a_triggered_repeat_budget_is_bounded():
    b = _budget_of('--duration', '1ms', '--samples', '10k', '--repeat', '100', '--trig', 'A0:rise:1.4V',
                   '--trig-mode', 'normal', '--timeout', '30s')
    assert b <= scope.REPEAT_MAX_S + 200.0
    with pytest.raises(DscopeError) as e:                 # 9999 x 30 s of trigger waits: refused, not a 3.5-day budget
        _budget_of('--duration', '1ms', '--samples', '10k', '--repeat', '10000', '--trig', 'A0:rise:1.4V',
                   '--trig-mode', 'normal', '--timeout', '30s')
    assert e.value.code == 'INVALID_ARG'


# 5. repeat is a capture setting: stream and loop-mode logs refuse it, and never compile it

def test_hal_config_sets_the_repeat_bit_only_when_asked():
    st = scope.Settings(repeat=3)
    assert scope.hal_config(st)[0]['repeat'] == 0            # stream() and loop_hal() call it like this
    assert scope.hal_config(st, repeat=3)[0]['repeat'] == 1
    assert scope.hal_config(st, repeat=-1)[0]['repeat'] == 0


def test_stream_refuses_repeat_before_touching_the_device():
    s = scope.Scope()
    s._check_link = lambda: pytest.fail('stream reached the device')
    s._run = lambda *a, **k: pytest.fail('stream ran a program')
    with pytest.raises(DscopeError) as e:
        s.stream(scope.Settings(analog=['A0'], digital=[], duration=1.0, repeat=3))
    assert e.value.code == 'INVALID_ARG' and 'repeat' in e.value.message


def test_a_loop_log_refuses_repeat():
    from openm454 import logger
    st = scope.Settings(analog=['A0'], digital=[], repeat=3)
    with pytest.raises(DscopeError) as e:
        logger.plan(st, 1e6, 1.0, storage='none')
    assert e.value.code == 'INVALID_ARG' and 'repeat' in e.value.message
    pl = logger.plan(scope.Settings(analog=['A0'], digital=[]), 1e6, 1.0, storage='none')
    with pytest.raises(DscopeError):
        logger.run(None, st, pl)                         # refused before the scope is used
    hal = logger.loop_hal(st, 1e6)[0]
    assert hal['repeat'] == 0 and hal['loop'] == 1          # never HW_BASE_MODE 7


def test_record_refuses_repeat():
    s = scope.Scope()
    s._check_link = lambda: pytest.fail('record reached the device')
    with pytest.raises(DscopeError) as e:
        s.record(scope.Settings(analog=['A0'], digital=[], repeat=2), 1e6, 1.0)
    assert e.value.code == 'INVALID_ARG'


# 6. a negative frame count never reaches the device

def test_a_negative_count_is_refused_by_the_command_line(capsys):
    """One JSON INVALID_ARG document with exit 2, refused offline (Scope.plan) before the device is opened."""
    import json
    from openm454 import cli
    assert cli.main(['capture', '-c', 'A0', '--repeat', '-1']) == 2
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    res = json.loads(out[0])
    assert res['ok'] is False and res['error']['code'] == 'INVALID_ARG'


def test_a_triggered_run_whose_trigger_waits_do_not_fit_the_run_limit_is_refused():
    """The engine deadline is capped at REPEAT_MAX_S; a triggered run whose (N - 1) trigger waits would pass it is
    refused offline instead of being cut at the cap with every frame lost."""
    n = int(scope.REPEAT_MAX_S / 5.0) + 50                     # (N - 1) x 5 s > 3600 s
    with pytest.raises(DscopeError) as e:
        _plan(repeat=n, trig_source='A0', trig_level=1.4, trig_mode='normal', timeout=5.0, duration=1e-3)
    assert e.value.code == 'INVALID_ARG' and 'trigger wait' in e.value.message and '--timeout' in e.value.hint
    _plan(repeat=n, trig_source='A0', trig_level=1.4, trig_mode='normal', timeout=1.0, duration=1e-3)
    _plan(repeat=n, duration=1e-3)                               # untriggered: no trigger waits


def test_a_negative_count_is_refused_by_plan():
    with pytest.raises(DscopeError) as e:
        _plan(repeat=-1)
    assert e.value.code == 'INVALID_ARG'


# 7. the memory cap counts the decoded frames too

def test_the_memory_cap_counts_the_decoded_frames():
    """8 digital channels decode to 1 byte per sample per channel, ~7.5x the transfer: 10000 frames of 1 ms passed
    a cap on the transfers alone (1016 MiB) and needed ~8.6 GiB."""
    st = scope.Settings(analog=[], digital=['D%d' % i for i in range(8)], duration=1e-3, samples=100_000,
                        trig_mode='force', repeat=10_000)
    with pytest.raises(DscopeError) as e:
        scope.Scope().plan(st)
    assert e.value.code == 'INVALID_ARG' and 'decoded' in e.value.message and 'transferred' in e.value.message
    st.repeat = 1000
    geom = scope.Scope().plan(st)['geometry']
    hal = scope.hal_config(st, repeat=1000)[0]
    pkt, dec = scope.repeat_frame_bytes(geom, hal)
    assert pkt == geom['pkt_bytes'] and dec == 8 * geom['digital_depth']


# 8. the mode-3 refusal names the window that is applied

def test_the_mode_3_refusal_names_the_lengthened_window():
    with pytest.raises(DscopeError) as e:
        _plan(rate=5000, duration=0.1, trig_mode='force', repeat=3)
    assert '0.256 s window' in e.value.message and '1024-sample minimum' in e.value.message
    assert '--rate' in e.value.hint


# 9. frames of a length that is not a multiple of 8 load back at that length

def test_loaded_digital_frames_are_cut_to_their_length(tmp_path):
    import numpy as np
    from openm454.capture import Capture, load_capture
    frames = [{'analog': {}, 'digital': {'D0': np.ones(n, np.uint8)}, 'trig_index': i}
              for i, n in enumerate((1003, 1005, 1001))]
    meta = {'channels': {}, 'analog_rate_hz': None, 'digital_rate_hz': 1e6, 't0_analog_s': 0.0,
            't0_digital_s': 0.0, 'acq': {'repeat_frames': 3, 'frames': 3}}
    cap = Capture(meta, {}, frames[0]['digital'])
    cap.frames = frames
    back = load_capture(cap.save(str(tmp_path / 'f.npz')))
    assert back.meta['frames_samples'] == {'D0': 1001}
    assert [len(f['digital']['D0']) for f in back.frames] == [1001] * 3
    assert all(f['digital']['D0'].all() for f in back.frames)          # no padding zeros read as low samples


# 10. the JSON lists the first frames and summarizes all of them

def _frames_cap(n):
    import numpy as np
    from openm454.capture import Capture
    meta = {'channels': {'A0': {'zero_code': 128, 'vdiv_v': 1.0, 'volts_per_code': 1 / 29.375, 'probe': 1.0}},
            'analog_rate_hz': 1e6, 'digital_rate_hz': None, 't0_analog_s': 0.0, 't0_digital_s': 0.0,
            'acq': {'triggered': True, 'samples': 16, 'repeat_frames': n, 'frames': n},
            'applied': {'channels': ['A0'], 'trigger': {}}, 'hints': [], 'warnings': []}
    frames = []
    for i in range(n):
        a = np.full(16, 128, np.uint8)
        a[8:] = 128 + (i % 50)                                   # vpp i % 50 codes
        frames.append({'analog': {'A0': a}, 'digital': {}, 't0_analog': 0.0, 't0_digital': 0.0,
                       'trig_index': None if i in (7, 300) else 1000 + i})
    cap = Capture(meta, frames[0]['analog'], {})
    cap.frames = frames
    return cap


def test_the_capture_json_caps_the_frame_list_and_summarizes_every_frame():
    import json
    import types
    from openm454 import cli
    cap = _frames_cap(2000)
    a = cli.build_parser().parse_args(['capture', '-c', 'A0', '--repeat', '2000', '--no-save', '--no-plot',
                                       '--measure', 'none'])
    _, res = cli.cmd_capture(a, types.SimpleNamespace(capture=lambda st: cap), emit=False)
    assert len(res['frames']) == cli.FRAMES_LISTED and res['frames'][0]['index'] == 0
    fs = res['frames_summary']
    assert fs['count'] == 2000 and fs['listed'] == cli.FRAMES_LISTED
    assert fs['without_trig_index'] == 2 and fs['without_trig_index_first'] == [7, 300]
    vpp = fs['analog']['A0']['vpp_v']
    assert vpp['min'] == 0.0 and vpp['max'] == pytest.approx(49 / 29.375, abs=1e-3)
    assert set(fs['analog']['A0']) == {'vpp_v', 'vmin_v', 'vmax_v'}
    assert list(res)[-2:] == ['frames_summary', 'frames']        # after files / hints / warnings
    assert len(json.dumps(res)) < 40_000
    assert cli._n_values(cap) == 2000 * 16                       # the idle watchdog extension covers every frame


def test_analyze_of_a_repeat_file_reports_its_frames(tmp_path, capsys):
    import json
    from openm454 import cli
    cap = _frames_cap(5)
    path = cap.save(str(tmp_path / 'r.npz'))
    assert cli.main(['analyze', path]) == 0
    res = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert res['acq']['repeat_frames'] == 5 and res['frames_summary']['count'] == 5 and len(res['frames']) == 5


# 11. MCP summaries keep the frame counts and the summary, not the list

def test_mcp_summaries_keep_the_repeat_frames():
    from openm454 import mcp_server as M
    doc = {'ok': True, 'cmd': 'capture', 'acq': {'triggered': True, 'repeat_frames': 3, 'frames': 3,
                                                  'trigger_check': 'unchecked',
                                                  'trigger_check_reason': 'repeat mode: frames are neither checked '
                                                                          'nor re-armed'},
           'frames_summary': {'count': 3, 'listed': 3}, 'frames': [{'index': i} for i in range(3)]}
    out = M.summarize(doc)
    assert out['acq']['repeat_frames'] == 3 and out['acq']['frames'] == 3
    assert out['acq']['trigger_check_reason'].startswith('repeat mode')
    assert out['frames_summary'] == {'count': 3, 'listed': 3} and 'frames' not in out
