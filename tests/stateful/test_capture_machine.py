"""One stateful capture/repeat machine for the audit invariants (no hardware)."""
import numpy as np
import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, rule, run_state_machine_as_test

from openm454 import scope
from openm454 import scope as scopemod
from openm454.errors import DscopeError

FRAME = {'analog': {'A0': np.zeros(8, np.uint8)},
         'digital': {'D0': np.zeros(8, np.uint8)},
         't0_analog': 0.0, 't0_digital': 0.0}


class Raw:
    def __init__(self, n):
        self.head = object()
        self.aborted = False
        self.frames = [dict(FRAME) for _ in range(n)]
        self.frame = self.frames[0]
        self.data = b''
        self.stop = 'normal'
        self.triggered = True
        self.handshakes = []
        self.transfers = 2
        self.transfers_wanted = 2
        self.t_start = 1.0
        self.t_head = 1.0


class NoHead(Raw):
    def __init__(self):
        super().__init__(1)
        self.head = None


class Machine(RuleBasedStateMachine):
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
        self.settings = scope.Settings(analog=['A0'], digital=['D0'], duration=0.01, samples=1000)

    def _run(self, prog, geom, tt, ct, **kw):
        n = kw.get('repeat_frames', 0) or 1
        return Raw(n)

    @rule(repeat=st.integers(0, 3))
    def set_repeat(self, repeat):
        self.repeat = repeat

    @rule()
    def capture(self):
        self.settings.repeat = self.repeat
        cap = self.scope.capture(self.settings)
        if self.repeat:
            assert len(cap.frames) == self.repeat
            assert cap.meta['acq']['repeat_frames'] == self.repeat
        else:
            assert 'repeat_frames' not in cap.meta['acq']

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


def test_capture_machine(monkeypatch):
    monkeypatch.setattr(scopemod, '_decode', lambda raw, geom, hal: raw.frames[0])
    monkeypatch.setattr(scopemod, '_decode_frames', lambda raw, geom, hal: raw.frames)
    run_state_machine_as_test(Machine, settings=settings(max_examples=10, stateful_step_count=5, deadline=None))


def test_a_triggered_repeat_without_a_head_is_no_trigger():
    s = scope.Scope()
    s._check_link = lambda: None
    s._cal_check = lambda st: None
    s._run = lambda prog, geom, tt, ct, **kw: NoHead()
    stg = scope.Settings(analog=['A0'], digital=['D0'], duration=0.01, samples=1000, repeat=2,
                         trig_source='D0', trig_edge='rise', trig_mode='normal', timeout=0.1)
    with pytest.raises(DscopeError) as e:
        s.capture(stg)
    assert e.value.code == 'TIMEOUT_NO_TRIGGER'
    assert 'replug' not in (e.value.hint or '').lower()
