"""OPENM454_TIMING: the capture-spacing profile. The constants are the traced spacing; a profile only shortens.

The values are only worth shortening if the signal still matches: a profile is refused before anything reaches the
device, so a typo cannot turn into a silent zero (or into a NaN sleep) halfway through a capture. The floors and the
hardware evidence behind them: docs/PROTOCOL.md section 7.1.
"""
import io
import json
import os
import types

import pytest

from openm454 import acquire, cli, host, logger, scope, selftest, timing, transport
from openm454 import mcp_server as M
from openm454.capture import load_capture
from openm454.errors import DscopeError
from openm454.fixture import UNKNOWN

FLOOR = 'gap=0.005,submit=0.005,setup=0.002,start=0.002,read=0.002,quiet=0.005'
FLOOR_PROFILE = {'gap': 0.005, 'submit': 0.005, 'setup': 0.002, 'start': 0.002, 'read': 0.002, 'quiet': 0.005}


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.delenv(timing.ENV, raising=False)
    monkeypatch.setenv('OPENM454_NO_USB', '1')                   # whatever happens, no scope is opened
    monkeypatch.setattr(host, 'HOST_FILE', str(tmp_path / 'host.json'))
    monkeypatch.setattr(cli, 'STOP_REQUEST', str(tmp_path / 'stop.request'))
    timing.reset()
    yield
    timing.reset()


def applied_matches_reported():
    """What active() names is exactly what the constants are: the profile over the traced baseline."""
    return timing.current() == {**timing.baseline(), **timing.active()}


def test_without_a_profile_nothing_changes():
    assert timing.profile() == {}
    assert timing.activate() == {}
    assert timing.active() == {}
    assert (acquire.GAP_BETWEEN_RUNS, acquire.QUIET_AFTER_STOP) == (0.100, 0.100)
    assert transport.READ_DELAY == 0.015
    assert timing.baseline() == {'gap': 0.100, 'quiet': 0.100, 'submit': 0.050, 'setup': 0.015, 'start': 0.015,
                                 'final': 0.001, 'read': 0.015}


def test_profile_parses_keys_not_module_names():
    assert timing.profile('gap=0.005,read=0.003') == {'gap': 0.005, 'read': 0.003}
    assert timing.profile(' gap = 0.005 ,, submit=0.02 ') == {'gap': 0.005, 'submit': 0.02}
    assert timing.profile('') == {}


def test_activate_shortens_the_constants_and_reports_them(monkeypatch):
    monkeypatch.setenv(timing.ENV, 'gap=0.005,quiet=0.006,read=0.003')
    assert timing.activate() == {'gap': 0.005, 'quiet': 0.006, 'read': 0.003}
    assert acquire.GAP_BETWEEN_RUNS == 0.005
    assert acquire.QUIET_AFTER_STOP == 0.006
    assert transport.READ_DELAY == 0.003
    assert timing.active() == {'gap': 0.005, 'quiet': 0.006, 'read': 0.003}
    assert applied_matches_reported()


def test_the_floor_profile_is_accepted_and_applied():
    assert timing.activate(FLOOR) == FLOOR_PROFILE
    assert applied_matches_reported() and timing.current()['final'] == 0.001


def test_the_quiet_time_is_its_own_key(monkeypatch):
    """Shortening a run-spacing key must not shorten the quiet time after a stop."""
    monkeypatch.setenv(timing.ENV, 'submit=0.01')
    timing.activate()
    assert acquire.SUBMIT_TO_START == 0.01
    assert acquire.QUIET_AFTER_STOP == 0.100
    assert timing.baseline()['quiet'] == 0.100


def test_the_hold_after_an_untriggered_stop_is_not_a_profile_key():
    """Writes within ~10 ms of a stop without a trigger were dropped on hardware: that hold stays at 100 ms."""
    timing.activate(FLOOR)
    assert acquire.QUIET_AFTER_UNTRIGGERED_STOP == 0.100
    assert 'QUIET_AFTER_UNTRIGGERED_STOP' not in [name for _, name in timing.KEYS.values()]


@pytest.mark.parametrize('key', sorted(timing.KEYS))
def test_every_key_has_a_floor(key):
    """Every key has a floor, the floor profile (docs/PROTOCOL.md 7.1); `final` is not shortened at all."""
    with pytest.raises(DscopeError) as e:
        timing.activate('%s=0' % key)
    assert e.value.code == 'INVALID_ARG' and 'floor' in e.value.message
    timing.activate('%s=%g' % (key, timing.MIN[key]))          # the floor itself is accepted
    assert timing.MIN['final'] == timing.baseline()['final']


@pytest.mark.parametrize('key, name', [('wake', 'ANALOG_WAKE_SETTLE'), ('overdrive', 'OVERDRIVE_SETTLE')])
def test_front_end_settling_is_not_adjustable(key, name):
    """The analog front end needs ~80 ms (wake) and ~215 ms (after overdrive) to settle: device physics, so a profile
    cannot shorten these waits; the key is refused with the reason."""
    before = getattr(scope, name)
    for spec in ('%s=0' % key, '%s=0.1' % key, 'gap=0.005,%s=0' % key):
        with pytest.raises(DscopeError) as e:
            timing.activate(spec)
        assert e.value.code == 'INVALID_ARG' and 'not adjustable' in e.value.message
        assert 'physics' in e.value.hint and 'shifts the measured levels' in e.value.hint
    assert key not in timing.KEYS and getattr(scope, name) == before and timing.active() == {}


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
    'gap=0.05,gap=0.005',  # a key given twice
    'gap=0.005,read=0.003,gap=0.005',
    'wake=0',              # front-end settling: not part of a profile
    'overdrive=0',
])
def test_a_bad_profile_is_invalid_arg_and_changes_nothing(spec):
    with pytest.raises(DscopeError) as e:
        timing.activate(spec)
    assert e.value.code == 'INVALID_ARG'
    assert timing.active() == {}
    assert (acquire.GAP_BETWEEN_RUNS, acquire.QUIET_AFTER_STOP) == (0.100, 0.100)
    assert transport.READ_DELAY == 0.015


def test_a_repeated_key_is_refused_with_a_reason():
    with pytest.raises(DscopeError) as e:
        timing.profile('gap=0.05,gap=0.005')
    assert 'twice' in e.value.message and 'ignored silently' in e.value.hint


def test_a_refused_profile_mid_list_leaves_no_half_applied_state():
    """Validation happens before the first constant is touched."""
    with pytest.raises(DscopeError):
        timing.activate('gap=0.005,quiet=0')
    assert acquire.GAP_BETWEEN_RUNS == 0.100
    assert acquire.QUIET_AFTER_STOP == 0.100


def test_a_new_profile_restores_every_key_the_old_one_shortened():
    timing.activate('gap=0.005,read=0.003')
    assert timing.activate('submit=0.01') == {'submit': 0.01}
    assert (acquire.GAP_BETWEEN_RUNS, transport.READ_DELAY, acquire.SUBMIT_TO_START) == (0.100, 0.015, 0.01)
    assert applied_matches_reported()
    assert timing.activate('') == {}
    assert timing.current() == timing.baseline() and applied_matches_reported()


def test_a_refused_second_profile_keeps_the_first_one_whole():
    timing.activate('gap=0.005,read=0.003')
    with pytest.raises(DscopeError):
        timing.activate('submit=0.01,quiet=0')
    assert timing.active() == {'gap': 0.005, 'read': 0.003} and applied_matches_reported()


def test_every_scope_reads_the_variable_again(monkeypatch):
    """A later different value is applied, an invalid one refused: never silently ignored."""
    monkeypatch.setenv(timing.ENV, 'gap=0.005')
    scope.Scope()
    assert timing.active() == {'gap': 0.005}
    monkeypatch.setenv(timing.ENV, 'read=0.003')
    scope.Scope()
    assert timing.active() == {'read': 0.003} and acquire.GAP_BETWEEN_RUNS == 0.100 and applied_matches_reported()
    monkeypatch.setenv(timing.ENV, 'gap=oops')
    with pytest.raises(DscopeError) as e:
        scope.Scope()
    assert e.value.code == 'INVALID_ARG'
    assert timing.active() == {'read': 0.003} and applied_matches_reported()    # the refused one changed nothing
    monkeypatch.delenv(timing.ENV)
    scope.Scope()
    assert timing.active() == {} and timing.current() == timing.baseline()


def test_a_profile_given_through_the_api_stays_until_the_variable_is_set(monkeypatch):
    timing.activate('gap=0.005')
    scope.Scope()                                                 # the variable is not set: the API profile stays
    assert timing.active() == {'gap': 0.005} and applied_matches_reported()
    monkeypatch.setenv(timing.ENV, 'read=0.003')
    scope.Scope()
    assert timing.active() == {'read': 0.003} and applied_matches_reported()


def test_scope_refuses_a_bad_profile_before_opening_anything(monkeypatch):
    monkeypatch.setenv(timing.ENV, 'gap=oops')
    with pytest.raises(DscopeError) as e:
        scope.Scope()
    assert e.value.code == 'INVALID_ARG'


def test_the_stream_seam_estimate_follows_the_profile():
    assert logger.restart_s() == pytest.approx(logger.RESTART_S)          # the traced spacing: the measured seam
    timing.activate(FLOOR)
    # 100 -> 5 ms quiet/gap, 15 -> 2 ms setup, 50 -> 5 ms submit, 15 -> 2 ms start
    assert logger.restart_s() == pytest.approx(logger.RESTART_S - 0.095 - 0.013 - 0.045 - 0.013)


def test_the_log_plan_compares_with_stream_at_the_active_spacing():
    import test_logger as L
    st = L.settings(['A2', 'A3'], ['D%d' % i for i in range(8)])
    alt = lambda: logger.stream_alternative(st, 50e6, 150e6, 1e12, 3600.0)     # no drain: the seam alone
    traced = alt()
    assert traced['gap_s'] == pytest.approx(logger.RESTART_S, abs=1e-3)
    timing.activate(FLOOR)
    floor = alt()
    assert floor['gap_s'] == pytest.approx(logger.restart_s(), abs=1e-3) and floor['gap_s'] < traced['gap_s'] - 0.1


# ------------------------------------------------------------------------------------------- the CLI refuses it
@pytest.mark.parametrize('argv', [
    ['info'], ['awg', 'off'], ['release'], ['recover'], ['selftest', '--quick'], ['debug', 'regs-read', '0x10'],
    ['debug', 'plan', '-c', 'A1'], ['log', '-c', 'A0', '--rate', '1M', '--duration', '1s', '--no-save'],
    ['log', '-c', 'A0', '--rate', '1M', '--duration', '1s', '--no-save', '--plan'], ['bench', '--no-device'],
    ['capture', '-c', 'A0'], ['watch', '-c', 'A0', '--count', '1'], ['stream', '-c', 'A0', '--duration', '1ms'],
])
@pytest.mark.parametrize('bad', ['gap=oops', 'gap=0', 'wake=0', 'gap=0.05,gap=0.005'])
def test_a_bad_profile_is_one_invalid_arg_document_for_every_command(monkeypatch, capsys, argv, bad):
    monkeypatch.setenv(timing.ENV, bad)
    code = cli.main(argv)
    out = capsys.readouterr().out.splitlines()
    assert code == 2 and len(out) == 1, out
    doc = json.loads(out[0])
    assert doc['ok'] is False and doc['cmd'] == argv[0] and doc['error']['code'] == 'INVALID_ARG'
    assert timing.ENV in doc['error']['message']


def test_mcp_tools_report_a_bad_profile_as_invalid_arg(monkeypatch, tmp_path):
    monkeypatch.setattr(M, 'JOBS_DIR', str(tmp_path / 'jobs'))
    monkeypatch.setenv(timing.ENV, 'gap=oops')
    srv = M.Server(out=io.BytesIO())
    for name, args in (('status', {}), ('log_plan', {'channels': 'A0', 'rate': '1M', 'duration': '1s',
                                                     'storage': 'none'})):
        doc = srv.run_tool(name, args)
        assert doc['ok'] is False and doc['error']['code'] == 'INVALID_ARG', (name, doc)


# ------------------------------------------------------------------------------------------- where it is reported
def test_info_names_the_active_profile(monkeypatch):
    monkeypatch.setenv(timing.ENV, 'gap=0.005')
    s = scope.Scope()
    s.dev = types.SimpleNamespace(info={})
    assert s.info()['timing'] == {'gap': 0.005}
    monkeypatch.delenv(timing.ENV)
    s2 = scope.Scope()
    s2.dev = types.SimpleNamespace(info={})
    assert s2.info()['timing'] == {}          # no profile: the baseline is the default


def _sim_capture():
    """A forced A1 capture through Scope.capture and the real engine on the simulated device."""
    from test_acquire_sim import FakeDevice, Sim
    st = scope.settings_from_args(cli.build_parser().parse_args(
        ['capture', '-c', 'A1', '--duration', '1ms', '--trig-mode', 'force']))
    s = scope.Scope()
    s.dev = types.SimpleNamespace(zero_info=scope._cached_zero(), info={'usb_speed': 'super'},
                                  t=types.SimpleNamespace(running=False, stale=False))
    s.engine = acquire.Engine(FakeDevice(Sim()))
    return s.capture(st)


def test_every_capture_names_the_profile_it_was_taken_with(monkeypatch):
    assert 'timing' not in _sim_capture().meta['acq']             # the baseline adds nothing
    monkeypatch.setenv(timing.ENV, FLOOR)
    cap = _sim_capture()
    assert cap.meta['acq']['timing'] == FLOOR_PROFILE and cap.meta['acq']['complete']


def _log(tmp_path, **kw):
    import test_logger as L
    pl = L.make_plan(['A2'], ['D0', 'D1'], 100e3, 1.0, tmp_path, **kw)
    layout, an, dg, stream = L.fake_signal(pl, 1.5)
    return L, pl, stream


def test_the_log_result_and_its_files_name_the_profile(tmp_path, monkeypatch):
    monkeypatch.setenv(timing.ENV, FLOOR)
    timing.activate()
    L, pl, stream = _log(tmp_path)
    res = logger.run(L.FakeScope(stream, pl['packet_bytes']), L.settings(['A2'], ['D0', 'D1']), pl,
                     str(tmp_path / 'm'))
    assert res['ok'] and res['acq']['timing'] == FLOOR_PROFILE           # the result: the last line `log` prints
    cap = load_capture(res['files']['npz'])
    assert cap.meta['acq']['timing'] == FLOOR_PROFILE                    # the saved file, where a capture has it
    timing.reset()
    res = logger.run(L.FakeScope(stream, pl['packet_bytes']), L.settings(['A2'], ['D0', 'D1']), pl,
                     str(tmp_path / 'n'))
    assert 'timing' not in res['acq'] and 'timing' not in load_capture(res['files']['npz']).meta['acq']


def test_the_disk_log_chunks_name_the_profile(tmp_path):
    timing.activate(FLOOR)
    L, pl, stream = _log(tmp_path, storage='disk', chunk_s=0.5)
    res = logger.run(L.FakeScope(stream, pl['packet_bytes']), L.settings(['A2'], ['D0', 'D1']), pl,
                     str(tmp_path / 'd'))
    idx = json.load(open(res['files']['index']))
    assert idx['meta']['acq']['timing'] == FLOOR_PROFILE and idx['chunks']
    first = load_capture(os.path.join(res['files']['dir'], idx['chunks'][0]['file']))
    assert first.meta['acq']['timing'] == FLOOR_PROFILE


def test_the_last_line_of_cli_log_names_the_profile(tmp_path, monkeypatch, capsys):
    """At the CLI defaults no final statistics line is printed (the duration is a whole number of lines): the
    result document itself must carry the profile, and so must the error document of a failed log."""
    timing.activate(FLOOR)
    L, pl, stream = _log(tmp_path)
    args = cli.build_parser().parse_args(['log', '-c', 'A2,D0,D1', '--rate', '100k', '--duration', '1s',
                                          '--no-save'])
    assert cli.cmd_log(args, L.ProbeScope(stream, pl['packet_bytes'])) == 0
    last = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert last['ok'] and last['cmd'] == 'log' and last['acq']['timing'] == FLOOR_PROFILE

    class Failing(L.ProbeScope):
        def _run(self, *a, **kw):
            raise DscopeError('DEVICE_ERROR', 'simulated')
    assert cli.cmd_log(args, Failing(stream, pl['packet_bytes'])) == 6
    last = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert last['error']['code'] == 'DEVICE_ERROR' and last['acq']['timing'] == FLOOR_PROFILE


def test_watch_names_the_profile_in_its_first_line_and_summary(monkeypatch, capsys):
    import test_cli_watch_measure as W
    clock = W.Clock()
    monkeypatch.setattr(cli, 'time', clock)
    timing.activate(FLOOR)
    cli.cmd_watch(W.args_of('watch', '-c', 'A1,D0', '--every', '0.5s', '--count', '3'), W.FakeScope(clock, [0.1]))
    out = W.lines_of(capsys)
    assert out[0]['timing'] == FLOOR_PROFILE and 'timing' not in out[1]
    assert out[-1]['summary'] and out[-1]['timing'] == FLOOR_PROFILE
    timing.reset()
    cli.cmd_watch(W.args_of('watch', '-c', 'A1,D0', '--every', '0.5s', '--count', '2'), W.FakeScope(clock, [0.1]))
    assert not any('timing' in l for l in W.lines_of(capsys))


def test_the_selftest_result_names_the_profile():
    import test_selftest as T
    assert 'timing' not in selftest.run(T.FakeScope(), quick=True, awg=True, fx=UNKNOWN)
    timing.activate(FLOOR)
    res = selftest.run(T.FakeScope(), quick=True, awg=True, fx=UNKNOWN)
    assert res['timing'] == FLOOR_PROFILE
    assert M.summarize_selftest(res)['timing'] == FLOOR_PROFILE           # ... and so does its MCP summary


class StubCli:
    def __init__(self, doc):
        self.doc = doc

    def run(self, args, max_wall):
        return self.doc


def test_mcp_summaries_keep_the_profile(monkeypatch, tmp_path):
    monkeypatch.setattr(M, 'JOBS_DIR', str(tmp_path / 'jobs'))
    capture = {'ok': True, 'cmd': 'capture', 'applied': {'channels': ['A0']},
               'acq': {'mode': 'single', 'samples': 1000, 'complete': True, 'timing': FLOOR_PROFILE}}
    assert M.summarize(capture)['acq']['timing'] == FLOOR_PROFILE
    log = {'ok': True, 'cmd': 'log', 'acq': {'mode': 'loop', 'samples': 64, 'stop_reason': 'duration',
                                             'timing': FLOOR_PROFILE}}
    assert M.summarize(log)['acq']['timing'] == FLOOR_PROFILE           # record_stop / record_status results
    info = {'ok': True, 'cmd': 'info', 'device': {'model': 'DSCope M454'}, 'limits': {}, 'awg_last': None,
            'timing': FLOOR_PROFILE}
    srv = M.Server(cli=StubCli(info), out=io.BytesIO())
    assert srv.run_tool('status', {})['timing'] == FLOOR_PROFILE
    srv = M.Server(cli=StubCli(dict(info, timing={})), out=io.BytesIO())
    assert 'timing' not in srv.run_tool('status', {})                  # the baseline: nothing to report
