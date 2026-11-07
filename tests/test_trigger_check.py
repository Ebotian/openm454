"""Digital edge-trigger misfire check (openm454.trigcheck) and the re-arm loop in Scope.capture (fake engine, no
hardware). Hardware facts behind it (2026-11-07, 1 kHz calibration square): a D5 falling-edge trigger fired inside
the slow (~8 us) rising edge in 13/40 captures, t = 0 exactly on the first fall of ~50 ns of comparator chatter."""
import types

import numpy as np
import pytest

from openm454 import scope as scopemod
from openm454 import trigcheck
from openm454.acquire import RawCapture
from openm454.errors import DscopeError


# ------------------------------------------------------------------------------------------------ signals
def clean(n, idx, edge, at=0):
    """Settled step at sample idx + at: 'fall' = 1 -> 0, 'rise' = 0 -> 1."""
    b = np.zeros(n, np.uint8)
    if edge == 'fall':
        b[:idx + at] = 1
    else:
        b[idx + at:] = 1
    return b


# measured at 400 MSa/s (2.5 ns): rises at -2.5, 7.5, 17.5, 27.5, 37.5 ns, falls at 0, 10, 22.5, 32.5, 42.5 ns,
# then high; t = 0 (idx) is the first chatter fall. Offsets in samples from idx: (start, level) of each run.
CHATTER_RISE = [(-1, 1), (0, 0), (3, 1), (4, 0), (7, 1), (9, 0), (11, 1), (13, 0), (15, 1), (17, 0), (19, 1)]


def chatter(n, idx, rising=True, pattern=CHATTER_RISE):
    """Slow edge with chatter, settled before and after; rising=False is the mirror image (a slow falling edge)."""
    b = np.zeros(n, np.uint8)
    for k, (off, lvl) in enumerate(pattern):
        end = idx + pattern[k + 1][0] if k + 1 < len(pattern) else n
        b[idx + off:end] = lvl
    return b if rising else 1 - b


# ------------------------------------------------------------------------------------------------ pure check
@pytest.mark.parametrize('rate', [400e6, 100e6, 10e6, 1e6])
@pytest.mark.parametrize('edge', ['fall', 'rise'])
def test_clean_edges_are_ok_and_the_other_edge_mismatches(rate, edge):
    n, idx = 4000, 2000
    b = clean(n, idx, edge)
    other = 'rise' if edge == 'fall' else 'fall'
    assert trigcheck.edge_check(b, idx, rate, edge) == 'ok'
    assert trigcheck.edge_check(b, idx, rate, {'fall': 'falling', 'rise': 'rising'}[edge]) == 'ok'
    assert trigcheck.edge_check(b, idx, rate, other) == 'mismatch'
    assert trigcheck.edge_check(b.astype(bool), idx, rate, edge) == 'ok'


def test_measured_misfire_fall_trigger_inside_chattering_rising_edge():
    n, idx = 8000, 4000                       # 20 us at 400 MSa/s, trigger in the middle
    b = chatter(n, idx, rising=True)
    assert b[idx - 1] == 1 and b[idx] == 0 and b[idx - 400:idx - 1].max() == 0 and b[idx + 19:].min() == 1
    assert trigcheck.settled_levels(b, idx, 400e6) == (0, 1)
    assert trigcheck.edge_check(b, idx, 400e6, 'fall') == 'mismatch'
    assert trigcheck.edge_check(b, idx, 400e6, 'rise') == 'ok'       # the same edge seen by a rise trigger


def test_chatter_on_the_requested_edge_is_ok():
    n, idx = 8000, 4000
    b = chatter(n, idx, rising=False)          # slow falling edge, rise trigger on the first chatter rise
    assert trigcheck.edge_check(b, idx, 400e6, 'rise') == 'mismatch'
    assert trigcheck.edge_check(b, idx, 400e6, 'fall') == 'ok'
    # trigger on a later chatter fall of a slow falling edge: still the falling edge
    fall = 1 - chatter(n, idx, rising=True)
    assert trigcheck.edge_check(fall, idx + 3, 400e6, 'rise') == 'mismatch'
    assert trigcheck.edge_check(fall, idx + 4, 400e6, 'fall') == 'ok'


@pytest.mark.parametrize('rate,tol', [(400e6, 8), (100e6, 2), (1e6, 2)])
def test_trigger_point_tolerance_never_turns_into_a_mismatch(rate, tol):
    n, idx = 4000, 2000
    for at in range(-tol, tol + 1):
        assert trigcheck.edge_check(clean(n, idx, 'fall', at), idx, rate, 'fall') == 'ok', at
    for at in (-tol - 1, tol + 1, -60, 60):
        assert trigcheck.edge_check(clean(n, idx, 'fall', at), idx, rate, 'fall') == 'inconclusive', at
        assert trigcheck.edge_check(clean(n, idx, 'fall', at), idx, rate, 'rise') == 'inconclusive', at


def test_too_few_samples_are_inconclusive():
    b = clean(4000, 2000, 'fall')
    assert trigcheck.edge_check(b, 0, 100e6, 'fall') == 'inconclusive'          # no pre-trigger samples
    assert trigcheck.edge_check(b[:2001], 2000, 100e6, 'fall') == 'inconclusive'  # one sample after t = 0
    assert trigcheck.edge_check(b[1990:2010], 10, 100e6, 'fall') == 'inconclusive'  # 10 < 20 samples (200 ns)
    assert trigcheck.edge_check(np.array([1, 0], np.uint8), 1, 100e6, 'fall') == 'inconclusive'
    assert trigcheck.edge_check(np.zeros(0, np.uint8), 0, 100e6, 'fall') == 'inconclusive'
    assert trigcheck.edge_check(b, 5000, 100e6, 'fall') == 'inconclusive'       # index past the end
    assert trigcheck.edge_check(None, 0, 100e6, 'fall') == 'inconclusive'
    assert trigcheck.edge_check(b, 2000, 0, 'fall') == 'inconclusive'


def test_short_settled_runs_at_slow_rates():
    # 1 MSa/s: 3 samples (3 us) settle a level; the 50 ns chatter is invisible and a misfire is a plain step
    b = clean(40, 20, 'rise')
    assert trigcheck.edge_check(b, 20, 1e6, 'fall') == 'mismatch'
    assert trigcheck.edge_check(b, 20, 1e6, 'rise') == 'ok'
    assert trigcheck.edge_check(clean(6, 3, 'rise'), 3, 1e6, 'rise') == 'ok'       # 3 + 3 samples
    assert trigcheck.edge_check(clean(5, 3, 'rise'), 3, 1e6, 'rise') == 'inconclusive'  # 2 after
    # 1 kSa/s on a 1 kHz square: aliased bits, no settled runs
    alias = np.tile(np.array([0, 1], np.uint8), 50)
    assert trigcheck.edge_check(alias, 50, 1e3, 'fall') == 'inconclusive'


def test_glitches_and_fast_clocks_are_inconclusive_not_misfires():
    b = np.ones(4000, np.uint8)
    b[2000:2005] = 0                                    # 50 ns low glitch at 100 MSa/s: a real 1->0 step
    assert trigcheck.edge_check(b, 2000, 100e6, 'fall') == 'inconclusive'
    clk = np.tile(np.array([1] * 5 + [0] * 5, np.uint8), 400)    # 10 MHz at 100 MSa/s: runs of 50 ns
    assert trigcheck.edge_check(clk, 2000, 100e6, 'fall') == 'inconclusive'
    slow = np.tile(np.array([1] * 50 + [0] * 50, np.uint8), 40)   # 1 MHz: 500 ns runs settle; falls at 100k + 50
    assert trigcheck.edge_check(slow, 2050, 100e6, 'fall') == 'ok'
    assert trigcheck.edge_check(slow, 2000, 100e6, 'fall') == 'mismatch'


def test_long_chatter_beyond_the_window_is_inconclusive():
    n, idx = 20000, 10000
    b = np.tile(np.array([0, 1], np.uint8), n // 2)     # chatter over the whole 10 us window at 400 MSa/s
    b[:idx - 4500] = 0
    b[idx + 4500:] = 1
    assert trigcheck.edge_check(b, idx, 400e6, 'fall') == 'inconclusive'


def test_any_edge_and_pulse_triggers_are_unchecked():
    b = clean(4000, 2000, 'fall')
    for e in ('any', 'both', 'pulse>', 'pulse<', 'npulse>', 'npulse<', None):
        assert trigcheck.edge_check(b, 2000, 100e6, e) == 'unchecked'
    assert trigcheck.checked_edge('Falling') == 'fall' and trigcheck.checked_edge('rise') == 'rise'


# ------------------------------------------------------------------------------------------------ re-arm loop
HEAD = bytes(b'\x55\x55\x55\x55' + bytes(4092))       # triggered head


class FakeDev:
    def __init__(self):
        self.zero_info = scopemod._cached_zero()
        self.info = {'usb_speed': 'super'}
        self.t = types.SimpleNamespace(running=False, stale=False)


class ScriptEngine:
    """Each run takes the next (frame, trigger wait s) of the script; frame None = no trigger ('abort': aborted)."""
    def __init__(self, script, record_s):
        self.script, self.record_s, self.calls = list(script), record_s, []

    def run(self, prog, **kw):
        self.calls.append(kw)
        fr, waited = self.script.pop(0)
        raw = RawCapture()
        raw.t_start = 1000.0
        if fr is None or fr == 'abort':
            raw.aborted = fr == 'abort'
            return raw
        raw.head, raw.data = HEAD, bytearray(kw['need_bytes'])
        raw.t_head = raw.t_start + self.record_s + waited
        raw.frame = fr
        return raw


@pytest.fixture
def fake_decode(monkeypatch):
    monkeypatch.setattr(scopemod, '_decode', lambda raw, geom, hal: raw.frame)


def st_d5(**kw):
    st = scopemod.Settings()
    st.analog, st.digital = [], ['D5']
    st.trig_source, st.trig_edge, st.trig_mode = 'D5', 'fall', 'normal'
    st.duration, st.samples, st.timeout = 20e-6, 8000, 3.0
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def run(st, kinds, waits=None):
    """kinds: 'ok' (clean requested edge), 'misfire' (chatter on the other edge), None (no trigger), 'abort'."""
    geom = scopemod.Scope().plan(st)['geometry']
    rate, n = geom['digital_rate'], geom['digital_depth']
    idx = n // 2

    def frame(kind):
        if kind in (None, 'abort'):
            return kind
        rising = trigcheck.checked_edge(st.trig_edge) == 'rise'
        if kind == 'ok':
            b = clean(n, idx, 'rise' if rising else 'fall')
        else:
            b = chatter(n, idx, rising=not rising)
        b = b.copy()
        b.flags.writeable = False
        return {'analog': {}, 'digital': {st.trig_source: b}, 't0_analog': 0.0, 't0_digital': -idx / rate}
    frames = [frame(k) for k in kinds]
    s = scopemod.Scope()
    s.dev = FakeDev()
    s.engine = ScriptEngine(list(zip(frames, waits or [0.001] * len(kinds))), geom['record_s'])
    return s, frames, geom


def test_misfires_are_discarded_and_rearmed(fake_decode):
    s, frames, _ = run(st_d5(), ['misfire', 'misfire', 'ok'])
    cap = s.capture(st_d5())
    acq = cap.meta['acq']
    assert len(s.engine.calls) == 3 and s.engine.script == []
    assert acq['trigger_check'] == 'misfire_retried' and acq['trigger_misfires'] == 2
    assert acq['attempts'] == ['misfire', 'misfire', 'triggered'] and acq['triggered']
    assert cap.digital['D5'] is frames[2]['digital']['D5']
    assert not any('misfire' in w for w in cap.meta['warnings'])
    assert any('2 misfire(s)' in h and 'rising edge' in h for h in cap.meta['hints'])


def test_clean_trigger_is_ok_with_one_run(fake_decode):
    s, _, _ = run(st_d5(), ['ok'])
    cap = s.capture(st_d5())
    assert cap.meta['acq']['trigger_check'] == 'ok' and cap.meta['acq']['trigger_misfires'] == 0
    assert cap.meta['acq']['attempts'] == ['triggered'] and len(s.engine.calls) == 1


def test_rise_trigger_misfire_on_a_slow_falling_edge(fake_decode):
    st = st_d5(trig_edge='rise')
    s, _, _ = run(st, ['misfire', 'ok'])
    cap = s.capture(st)
    assert cap.meta['acq']['trigger_check'] == 'misfire_retried' and cap.meta['acq']['trigger_misfires'] == 1


def test_every_retry_misfires_returns_the_last_with_a_warning(fake_decode):
    s, frames, _ = run(st_d5(), ['misfire'] * 5)
    cap = s.capture(st_d5())
    acq = cap.meta['acq']
    assert len(s.engine.calls) == 5                     # first run + trig_retries (4)
    assert acq['trigger_check'] == 'mismatch' and acq['trigger_misfires'] == 4
    assert acq['attempts'] == ['misfire'] * 5 and acq['triggered']      # the hardware did trigger
    assert cap.digital['D5'] is frames[-1]['digital']['D5']
    w = [x for x in cap.meta['warnings'] if 'D5 falling-edge trigger' in x]
    assert w and 'all 5 captures misfired' in w[0] and 're-arms' in w[0]
    assert any('no hysteresis' in h and 'analog channel' in h for h in cap.meta['hints'])


def test_retries_zero_only_checks(fake_decode):
    st = st_d5(trig_retries=0)
    s, _, _ = run(st, ['misfire'])
    cap = s.capture(st)
    assert len(s.engine.calls) == 1
    assert cap.meta['acq']['trigger_check'] == 'mismatch' and cap.meta['acq']['trigger_misfires'] == 0
    assert any('trig_retries=0' in w and 'returned anyway' in w for w in cap.meta['warnings'])


def test_retries_share_the_original_trigger_wait(fake_decode):
    # auto mode waits auto_after = 0.3 s for a trigger; each run here waits 0.2 s for its (mis)fire
    st = st_d5(trig_mode='auto', auto_after=0.3)
    s, frames, geom = run(st, ['misfire', 'misfire', 'ok'], waits=[0.2, 0.2, 0.2])
    cap = s.capture(st)
    acq = cap.meta['acq']
    assert len(s.engine.calls) == 2                     # 0.1 s left after the first run, none after the second
    assert s.engine.calls[1]['trigger_timeout'] == pytest.approx(s._head_wait(geom, 0.1))
    assert acq['trigger_check'] == 'mismatch' and acq['trigger_misfires'] == 1
    assert acq['attempts'] == ['misfire', 'misfire']
    assert cap.digital['D5'] is frames[1]['digital']['D5']
    assert any('trigger wait is used up' in w for w in cap.meta['warnings'])


def test_normal_mode_timeout_bounds_the_retries(fake_decode):
    st = st_d5(timeout=1.0)
    s, _, geom = run(st, ['misfire'] * 5, waits=[0.4, 0.4, 0.4, 0.4, 0.4])
    cap = s.capture(st)
    assert len(s.engine.calls) == 3                     # 1.0 - 0.4 - 0.4 = 0.2 s left before the third run
    assert s.engine.calls[2]['trigger_timeout'] == pytest.approx(s._head_wait(geom, 0.2))
    assert cap.meta['acq']['trigger_check'] == 'mismatch'


def test_holdoff_left_in_the_wait_is_needed_for_a_rearm(fake_decode):
    st = st_d5(timeout=0.5, holdoff=0.3)                # wait 0.8 s; the misfire came after 0.6 s
    s, _, _ = run(st, ['misfire', 'ok'], waits=[0.6, 0.3])
    cap = s.capture(st)
    assert len(s.engine.calls) == 1 and cap.meta['acq']['trigger_check'] == 'mismatch'
    st = st_d5(timeout=1.0, holdoff=0.3)
    s, _, _ = run(st, ['misfire', 'ok'], waits=[0.6, 0.3])
    assert s.capture(st).meta['acq']['trigger_check'] == 'misfire_retried'


def test_rearm_without_trigger_keeps_the_misfire(fake_decode):
    s, frames, _ = run(st_d5(), ['misfire', None])
    cap = s.capture(st_d5())
    acq = cap.meta['acq']
    assert acq['attempts'] == ['misfire', 'no-trigger'] and acq['trigger_check'] == 'mismatch'
    assert acq['trigger_misfires'] == 0 and cap.digital['D5'] is frames[0]['digital']['D5']
    assert any('did not fire' in w for w in cap.meta['warnings'])


def test_rearm_aborted_by_the_watchdog_raises(fake_decode):
    s, _, _ = run(st_d5(), ['misfire', 'abort'])
    with pytest.raises(DscopeError) as ei:
        s.capture(st_d5())
    assert ei.value.code == 'TIMEOUT_WATCHDOG'


def test_unchecked_triggers_never_rearm(fake_decode):
    for st in (st_d5(trig_edge='any'), st_d5(trig_edge='pulse>', trig_pulse=1e-6), st_d5(trig_mode='force')):
        s, _, _ = run(st, ['misfire', 'ok'])
        cap = s.capture(st)
        assert cap.meta['acq']['trigger_check'] == 'unchecked' and len(s.engine.calls) == 1
    # auto mode without a trigger: the forced fallback is not checked
    st = st_d5(trig_mode='auto')
    s, _, _ = run(st, [None, 'misfire'])
    cap = s.capture(st)
    assert cap.meta['acq']['attempts'] == ['no-trigger', 'forced']
    assert cap.meta['acq']['trigger_check'] == 'unchecked' and len(s.engine.calls) == 2


def test_analog_trigger_is_unchecked(fake_decode):
    st = st_d5(analog=['A1'], digital=['D5'], trig_source='A1', trig_level=1.0)
    s, frames, geom = run(st_d5(analog=['A1'], digital=['D5']), ['misfire', 'ok'])
    for fr in frames:
        fr['analog'] = {'A1': np.full(geom['analog_depth'], 128, np.uint8)}
    cap = s.capture(st)
    assert cap.meta['acq']['trigger_check'] == 'unchecked' and len(s.engine.calls) == 1


def test_trig_retries_from_cli_args():
    a = types.SimpleNamespace(duration=None, timebase=None, rate=None, samples=None, trig='D5:fall', trig_mode='normal',
                              trig_pos='50', auto_after='0.3s', timeout='5s', holdoff='0s', sensitivity=5)
    assert scopemod._timing_from_args(scopemod.Settings(), a).trig_retries == 4
    a.trig_retries = '2'
    assert scopemod._timing_from_args(scopemod.Settings(), a).trig_retries == 2
    a.trig_retries = -1
    with pytest.raises(DscopeError) as ei:
        scopemod._timing_from_args(scopemod.Settings(), a)
    assert ei.value.code == 'INVALID_ARG'


def test_force_mode_never_refuses_an_off_screen_trigger_level():
    from openm454.errors import DscopeError
    from openm454.scope import Scope, Settings
    st = Settings()
    st.analog, st.digital = ['A1'], []
    st.vdiv, st.offset = {c: 1.0 for c in ('A0', 'A1', 'A2', 'A3')}, {c: 0.0 for c in ('A0', 'A1', 'A2', 'A3')}
    st.trig_source, st.trig_level, st.trig_mode = 'A1', 9.0, 'force'
    Scope().plan(st)                                                   # the trigger is not used: no error
    st.trig_mode = 'normal'
    with pytest.raises(DscopeError) as ei:
        Scope().plan(st)
    assert ei.value.code == 'TRIGGER_OUT_OF_RANGE'


@pytest.mark.parametrize('rate', [100e6, 400e6])
def test_fast_data_bits_are_not_taken_for_chatter(rate):
    # review finding: SPI MOSI at 8 MHz (125 ns bits) sending 0x2F on an idle-low line, D0:fall on the clean 1->0
    # between bit 2 and bit 3: the 0 and 1 around it are shorter than the settle time, which once read as chatter
    spb = int(round(125e-9 * rate))
    idle = np.zeros(int(2e-6 * rate), dtype=np.uint8)
    bits = [int(c) for c in format(0x2F, '08b')]
    data = np.repeat(np.array(bits, dtype=np.uint8), spb)
    x = np.concatenate((idle, data, np.ones(int(2e-6 * rate), dtype=np.uint8)))
    idx = len(idle) + 3 * spb                      # the fall between bit 2 (1) and bit 3 (0)
    assert x[idx - 1] == 1 and x[idx] == 0
    assert trigcheck.edge_check(x, idx, rate, 'fall') == 'inconclusive'


def test_aliased_chatter_at_80_msa_is_still_a_misfire():
    # measured 2026-11-07, D5 alone at 80 MSa/s: settled low, then 5 samples high, 1 low, settled high; the falling
    # trigger fired on the 1-sample low (aliasing turned ~100 MHz chatter into a 62.5 ns run)
    x = np.array([0] * 995 + [1] * 5 + [0] * 1 + [1] * 999, dtype=np.uint8)
    assert trigcheck.edge_check(x, 1000, 80e6, 'fall') == 'mismatch'
