"""Bad CLI values and bad OPENM454_TIMING must be JSON errors, not tracebacks."""
import json

from openm454 import cli


def _last_json(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_bad_duration_is_json(capsys):
    assert cli.main(['capture', '--duration', 'abc']) == 2
    err = _last_json(capsys)
    assert err['ok'] is False and err['error']['code'] == 'INVALID_ARG'


def test_bad_timing_env_is_json(capsys, monkeypatch):
    monkeypatch.setenv('OPENM454_TIMING', 'gap=oops')
    assert cli.main(['info']) == 2
    err = _last_json(capsys)
    assert err['ok'] is False and err['error']['code'] == 'INVALID_ARG'
