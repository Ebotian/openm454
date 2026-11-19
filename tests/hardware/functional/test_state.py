"""What a run leaves behind: no new state files, an honest AWG record, an idle engine (`hw`)."""
import os

import pytest

from openm454 import scope as scopemod

pytestmark = pytest.mark.hw

RATE = 10e6
WINDOW = 2e-3


def _tree(root):
    out = set()
    for dirpath, _, files in os.walk(root):
        for name in files:
            out.add(os.path.relpath(os.path.join(dirpath, name), root))
    return out


def test_a_capture_adds_no_state_file(bench, scope):
    link = bench.link('awg_to_a0')
    link.program(scope)
    before = _tree(scopemod.STATE)
    scope.capture(link.settings(WINDOW, RATE))
    after = _tree(scopemod.STATE)
    assert after <= before | {'openm454.lock'}, 'new files under state/: %s' % sorted(after - before)


def test_the_awg_record_is_what_was_programmed(bench, scope):
    """The generator is write-only, so the record in state/awg.json is the only account of what is running."""
    scope.awg_set('square', 2e3, 2.0, 0.5)
    st = scope.awg_status()
    assert st['enabled'] is True and st['freq_hz'] == pytest.approx(2000.0)
    assert st['high_v'] == pytest.approx(2.0, abs=0.01) and st['low_v'] == pytest.approx(0.5, abs=0.01)


def test_the_engine_is_idle_after_a_capture(bench, scope):
    link = bench.link('awg_to_a0')
    link.program(scope)
    scope.capture(link.settings(WINDOW, RATE))
    assert scope.engine.active is False
    assert scope.dev.t.running is False and not scope.dev.t.failed
    assert not any(tr.isSubmitted() for tr in scope.engine._trs)