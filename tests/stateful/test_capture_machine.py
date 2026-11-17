"""One stateful capture/repeat machine for the audit invariants (no hardware).

The rules drive the real Scope against a scripted engine, so each rule checks a decision the audit was about:
a misfire is re-armed and the re-armed capture is returned; repeat mode is one start and is neither checked nor
re-armed; a triggered run without a head is TIMEOUT_NO_TRIGGER; an untriggered repeat start fails instead of
falling back; stream/log refuse repeat. The hold times of the engine loop are measured by the engine tests
(tests/stateful/test_acquire_sim.py), not by this machine.
"""
import numpy as np
import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test

from openm454 import scope
from openm454 import scope as scopemod
from openm454.errors import DscopeError


class Raw:
    def __init__(self, n=1, tag=0):
        self.head = object()
        self.aborted = False
        self.frames = [self._frame(tag)] * n
        self.frame = self.frames[0]
        self.data = b''
        self.stop = 'normal'
        self.triggered = True
        self.handshakes = []
        self.transfers = 2
        self.transfers_wanted = 2
        self.t_start = 1.0
        self.t_head = 1.0
        self.tag = tag

    @staticmethod
    def _frame(tag):
        return {'analog': {'A0': np.zeros(8, np.uint8)},
                'digital': {'D0': np.full(8, tag, np.uint8)},
                't0_analog': 0.0, 't0_digital': 0.0}


class NoHead(Raw):
    def __init__(self):
        super().__init__(1)
        self.head = None


class Machine(RuleBasedStateMachine):
    """script entries are (kind, tag, frames); the kind 'none' is a run that never saw a head."""

    VERDICTS = []

    def __init__(self):
        super().__init__()
        self.scope = scope.Scope()
        self.scope._check_link = lambda: None
        self.scope._cal_check = lambda st: None
        self.scope._run = self._run
        self.scope._check_complete = lambda raw, geom: True
        self.scope._decode_frames = lambda raw, geom, hal: raw.frames
        self.scope._decode = lambda raw, geom, hal: raw.frames[0]
        self.repeat = 0
        self.script = []
        self.runs = 0
        self.last_misfires = 0
        self.last_error = None

    def _run(self, prog, geom, tt, ct, **kw):
        kind, tag, n = self.script.pop(0) if self.script else ('ok', self.runs, kw.get('repeat_frames') or 1)
        self.runs += 1
        if kind == 'none':
            return NoHead()
        return Raw(n, tag)

    def settings(self, **kw):
        stg = scope.Settings(analog=['A0'], digital=['D0'], duration=0.01, samples=1000)
        for k, v in kw.items():
            setattr(stg, k, v)
        return stg

    @rule(repeat=st.integers(0, 3))
    def set_repeat(self, repeat):
        self.repeat = repeat

    @rule()
    def capture(self):
        self.settings_ = self.settings(repeat=self.repeat)
        cap = self.scope.capture(self.settings_)
        if self.repeat:
            assert len(cap.frames) == self.repeat
            assert cap.meta['acq']['repeat_frames'] == self.repeat
        else:
            assert 'repeat_frames' not in cap.meta['acq']

    @rule(tries=st.integers(1, 2))
    def misfire_is_rearmed(self, tries):
        """A digital edge capture that misfires is re-armed; the retry run is what the caller gets."""
        self.script = [('misfire', 0, 1)] + [('ok', 1, 1)] * tries
        Machine.VERDICTS[:] = ['mismatch'] * tries + ['ok']
        self.runs, self.last_misfires, self.last_error = 0, 0, None
        stg = self.settings(trig_source='D0', trig_edge='rise', trig_mode='normal', timeout=0.5, trig_retries=tries)
        cap = self.scope.capture(stg)
        acq = cap.meta['acq']
        self.last_misfires = acq['trigger_misfires']
        assert self.runs == tries + 1
        assert acq['trigger_check'] == 'misfire_retried' and acq['trigger_misfires'] == tries
        assert set(np.unique(cap.digital['D0'])) == {1}      # the re-armed run is returned, not the misfire

    @rule(repeat=st.integers(2, 3))
    def repeat_is_not_rearmed(self, repeat):
        """Repeat mode is one start: no misfire check, no re-arm, and the recorded frames are returned as they are."""
        self.script = [('misfire', 0, repeat)]
        Machine.VERDICTS[:] = ['mismatch']
        self.runs = 0
        stg = self.settings(repeat=repeat, trig_source='D0', trig_edge='rise', trig_mode='normal', timeout=0.5)
        cap = self.scope.capture(stg)
        acq = cap.meta['acq']
        assert self.runs == 1 and acq['trigger_misfires'] == 0 and acq['trigger_check'] == 'unchecked'
        assert 'neither checked nor re-armed' in acq['trigger_check_reason']
        assert any('repeat mode' in h and 'not checked' in h for h in cap.meta['hints'])
        assert len(cap.frames) == repeat

    @rule()
    def no_trigger_is_reported(self):
        self.script = [('none', 0, 0)]
        self.runs, self.last_error = 0, None
        stg = self.settings(trig_source='D0', trig_edge='rise', trig_mode='normal', timeout=0.05)
        with pytest.raises(DscopeError) as e:
            self.scope.capture(stg)
        self.last_error = e.value
        assert e.value.code == 'TIMEOUT_NO_TRIGGER'
        assert 'did not start' not in (e.value.message or '').lower()
        assert 'replug' not in (e.value.hint or '').lower()
        assert self.runs == 1

    @rule()
    def untriggered_repeat_start_failure(self):
        self.script = [('none', 0, 0)]
        self.runs = 0
        stg = self.settings(repeat=2, trig_mode='force')
        with pytest.raises(DscopeError) as e:
            self.scope.capture(stg)
        assert e.value.code == 'PROTOCOL_ERROR'
        assert self.runs == 1                                # no forced fallback capture

    @rule()
    def stream_refuses_repeat(self):
        with pytest.raises(DscopeError) as e:
            self.scope.stream(scope.Settings(analog=['A0'], digital=[], duration=0.01, samples=1000, repeat=2))
        assert e.value.code == 'INVALID_ARG'

    @rule()
    def record_refuses_repeat(self):
        with pytest.raises(DscopeError) as e:
            self.scope.record(scope.Settings(analog=['A0'], digital=[], duration=0.01, samples=1000, repeat=2), 1000)
        assert e.value.code == 'INVALID_ARG'

    @invariant()
    def a_reported_no_trigger_means_no_trigger(self):
        if self.last_error is not None:
            assert self.last_error.code != 'DEVICE_DID_NOT_START'
            assert 'did not start' not in (self.last_error.message or '').lower()


def test_a_triggered_repeat_without_a_head_is_no_trigger():
    """A deterministic anchor for the rule the machine explores randomly: the repeat start that never saw a head."""
    m = Machine()
    m.script = [('none', 0, 0)]
    stg = m.settings(repeat=2, trig_source='D0', trig_edge='rise', trig_mode='normal', timeout=0.05)
    with pytest.raises(DscopeError) as e:
        m.scope.capture(stg)
    assert e.value.code == 'TIMEOUT_NO_TRIGGER' and m.runs == 1
    assert 'replug' not in (e.value.hint or '').lower()


def test_capture_machine(monkeypatch):
    monkeypatch.setattr(scopemod, '_decode', lambda raw, geom, hal: raw.frames[0])
    monkeypatch.setattr(scopemod, '_decode_frames', lambda raw, geom, hal: raw.frames)
    monkeypatch.setattr(scopemod.trigcheck, 'edge_check',
                        lambda *a, **k: Machine.VERDICTS.pop(0) if Machine.VERDICTS else 'ok')
    run_state_machine_as_test(Machine, settings=settings(max_examples=12, stateful_step_count=6, deadline=None))
