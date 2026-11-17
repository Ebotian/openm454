"""OPENM454_TIMING: the capture-spacing profile. The constants are the traced spacing; a profile only shortens.

The values are only worth shortening if the signal still matches: a profile is refused before anything reaches the
device, so a typo cannot turn into a silent zero (or into a NaN sleep) halfway through a capture.
"""
import json
import types

import pytest

from openm454 import acquire, scope, timing, transport
from openm454.errors import DscopeError


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(timing.ENV, raising=False)
    timing.reset()
    yield
    timing.reset()


def test_without_a_profile_nothing_changes():
    assert timing.profile() == {}
    assert timing.activate() == {}
    assert timing.active() == {}
    assert (acquire.GAP_BETWEEN_RUNS, acquire.QUIET_AFTER_STOP) == (0.100, 0.100)
    assert transport.READ_DELAY == 0.015


def test_profile_parses_keys_not_module_names():
    assert timing.profile('gap=0.005,read=0.003') == {'gap': 0.005, 'read': 0.003}
    assert timing.profile(' gap = 0.005 ,, submit=0.02 ') == {'gap': 0.005, 'submit': 0.02}
    assert timing.profile('') == {}


def test_activate_shortens_the_constants_and_reports_them(monkeypatch):
    monkeypatch.setenv(timing.ENV, 'gap=0.005,quiet=0.1,read=0.003')
    assert timing.activate() == {'gap': 0.005, 'quiet': 0.1, 'read': 0.003}
    assert acquire.GAP_BETWEEN_RUNS == 0.005
    assert acquire.QUIET_AFTER_STOP == 0.1
    assert transport.READ_DELAY == 0.003
    assert timing.active() == {'gap': 0.005, 'quiet': 0.1, 'read': 0.003}


def test_the_quiet_time_is_its_own_key(monkeypatch):
    """Shortening a run-spacing key must not shorten the quiet time after a stop."""
    monkeypatch.setenv(timing.ENV, 'submit=0.01')
    timing.activate()
    assert acquire.SUBMIT_TO_START == 0.01
    assert acquire.QUIET_AFTER_STOP == 0.100
    assert timing.baseline()['quiet'] == 0.100


@pytest.mark.parametrize('key', ['gap', 'submit', 'setup', 'start', 'final', 'read', 'quiet'])
def test_every_key_with_evidence_has_a_floor(key):
    """Zeroing the spacing wedges the scope on Windows after 8-15 captures; the floors are the conservative
    profile (300/300 on the maintainer's bench, 100+ per run here)."""
    with pytest.raises(DscopeError) as e:
        timing.activate('%s=0' % key)
    assert e.value.code == 'INVALID_ARG' and 'floor' in e.value.message
    timing.activate('%s=%g' % (key, timing.MIN[key]))          # the floor itself is accepted


def test_fidelity_only_keys_may_be_zeroed_but_are_flagged(monkeypatch):
    """wake/overdrive are not wedge-safety floors; zeroing them is allowed but can corrupt analog readings."""
    assert set(timing.FIDELITY_ONLY) == {'wake', 'overdrive'}
    assert set(timing.MIN).isdisjoint(timing.FIDELITY_ONLY)
    assert set(timing.MIN) | set(timing.FIDELITY_ONLY) == set(timing.KEYS)
    monkeypatch.setenv(timing.ENV, 'wake=0,overdrive=0')
    assert timing.activate() == {'wake': 0.0, 'overdrive': 0.0}
    w = timing.warnings()
    assert len(w) == 2 and all('fidelity-only' in x for x in w)
    p = scope.Scope().plan(scope.Settings(analog=['A0'], duration=0.01, samples=1000))
    assert any('fidelity-only' in x for x in p['warnings'])


def test_the_floors_are_one_conservative_profile_not_per_key_evidence():
    """The 300/300 evidence is for the all-MIN profile; mixed profiles rest on the monotonicity assumption."""
    assert timing.MIN == {'gap': 0.005, 'submit': 0.005, 'setup': 0.002, 'start': 0.002,
                          'read': 0.002, 'quiet': 0.1, 'final': 0.001}
    timing.activate('quiet=0.1,gap=0.1')       # mixed, at/above every floor; not separately measured
    assert acquire.QUIET_AFTER_STOP == 0.1 and acquire.GAP_BETWEEN_RUNS == 0.1


@pytest.mark.parametrize('spec', [
    'gap',                 # not key=value
    'nonsense=1',          # unknown key
    'gap=abc',             # not a number
    'gap=',                # empty value
    'gap=-0.001',          # negative
    'gap=nan',             # not finite
    'gap=inf',
    'gap=0.2',             # above the traced value: a profile only shortens
    'read=0.02',
    'quiet=0.2',
    'gap=0',               # below the floor it needs evidence for
    'quiet=0.001',
    'final=0',
])
def test_a_bad_profile_is_invalid_arg_and_changes_nothing(spec):
    with pytest.raises(DscopeError) as e:
        timing.activate(spec)
    assert e.value.code == 'INVALID_ARG'
    assert timing.active() == {}
    assert (acquire.GAP_BETWEEN_RUNS, acquire.QUIET_AFTER_STOP) == (0.100, 0.100)
    assert transport.READ_DELAY == 0.015


def test_a_refused_profile_mid_list_leaves_no_half_applied_state():
    """Validation happens before the first constant is touched."""
    with pytest.raises(DscopeError):
        timing.activate('gap=0.005,quiet=0')
    assert acquire.GAP_BETWEEN_RUNS == 0.100
    assert acquire.QUIET_AFTER_STOP == 0.100


def test_scope_refuses_a_bad_profile_before_opening_anything(monkeypatch):
    monkeypatch.setenv(timing.ENV, 'gap=oops')
    with pytest.raises(DscopeError) as e:
        scope.Scope()
    assert e.value.code == 'INVALID_ARG'


def test_cli_reports_a_bad_profile_as_json(monkeypatch, capsys):
    monkeypatch.setenv(timing.ENV, 'gap=oops')
    from openm454 import cli
    assert cli.main(['info']) == 2
    err = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert err['ok'] is False and err['error']['code'] == 'INVALID_ARG'


def test_info_names_the_active_profile(monkeypatch):
    monkeypatch.setenv(timing.ENV, 'gap=0.005')
    s = scope.Scope()
    s.dev = types.SimpleNamespace(info={})
    assert s.info()['timing'] == {'gap': 0.005}
    monkeypatch.delenv(timing.ENV)
    timing.reset()
    s2 = scope.Scope()
    s2.dev = types.SimpleNamespace(info={})
    assert s2.info()['timing'] == {}          # no profile: the baseline is the default
