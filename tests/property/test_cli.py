"""Bad CLI values and bad OPENM454_TIMING must be JSON errors, not tracebacks."""
from openm454 import cli
from tests.support.cli import last_json


def test_bad_duration_is_json(capsys):
    assert cli.main(['capture', '--duration', 'abc']) == 2
    err = last_json(capsys)
    assert err['ok'] is False and err['error']['code'] == 'INVALID_ARG'


def test_bad_timing_env_is_json(capsys, monkeypatch):
    monkeypatch.setenv('OPENM454_TIMING', 'gap=oops')
    assert cli.main(['info']) == 2
    err = last_json(capsys)
    assert err['ok'] is False and err['error']['code'] == 'INVALID_ARG'
