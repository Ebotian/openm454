"""CLI: the watch schedule (fake scope and clock), --measure basic/full/none, and help texts (no hardware)."""
import argparse
import json
import types

import numpy as np
import pytest

from openm454 import cli
from openm454.capture import Capture
from openm454.errors import DscopeError

RATE = 1e6
VPC = 1 / 29.375                       # volts per code at 1 V/div


def square(n=10_000, period=1000, high=250, jitter=None):
    """0/1 square: rising edges every `period` samples (or at the given list of periods), high for `high` samples."""
    b = np.zeros(n, np.uint8)
    t = 100
    for p in (jitter or []) + [period] * (n // period + 1):
        if t >= n:
            break
        b[t:t + high] = 1
        t += p
    return b


def make_cap(bits=None):
    bits = square() if bits is None else bits
    meta = {'channels': {'A1': {'zero_code': 128, 'vdiv_v': 1.0, 'volts_per_code': VPC, 'probe': 1.0}},
            'analog_rate_hz': RATE, 'digital_rate_hz': RATE, 't0_analog_s': -0.005, 't0_digital_s': -0.005,
            'acq': {'triggered': True, 'samples': bits.size}, 'applied': {'channels': ['A1', 'D0'], 'trigger': {}},
            'hints': [], 'warnings': []}
    codes = (128 + np.round(bits * 2.8 / VPC)).astype(np.uint8)          # 0 V / 2.8 V square
    return Capture(meta, {'A1': codes}, {'D0': bits.copy()})


def args_of(*argv):
    return cli.build_parser().parse_args(list(argv))


class Clock:
    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def sleep(self, dt):
        assert dt > 0
        self.t += dt


class FakeScope:
    """capture() takes durations[k] seconds of fake time (the first is slow, like a real session's first run)."""
    def __init__(self, clock, durations, on_capture=None):
        self.clock, self.durations, self.starts, self.on_capture = clock, list(durations), [], on_capture

    def capture(self, st):
        self.starts.append(round(self.clock.t - 1000.0, 6))
        k = len(self.starts) - 1
        self.clock.t += self.durations[min(k, len(self.durations) - 1)]
        if self.on_capture:
            self.on_capture(k)
        return make_cap()


@pytest.fixture
def clock(monkeypatch, tmp_path):
    c = Clock()
    monkeypatch.setattr(cli, 'time', c)
    monkeypatch.setattr(cli, 'STOP_REQUEST', str(tmp_path / 'stop.request'))
    return c


def lines_of(capsys):
    return [json.loads(x) for x in capsys.readouterr().out.splitlines()]


# ------------------------------------------------------------------------------------------- watch schedule
def test_watch_captures_start_every_apart_despite_a_slow_first_capture(clock, capsys):
    """Seen on hardware: '--every 0.5s' printed t_s 0.19 s apart for the first two lines: t_s was taken after each
    capture, and the first capture of a session is slower (setup tables, analog wake-up). Captures start 'every'
    apart and t_s is the start."""
    sc = FakeScope(clock, [0.31, 0.09])
    cli.cmd_watch(args_of('watch', '-c', 'A1,D0', '--every', '0.5s', '--count', '5'), sc)
    out = lines_of(capsys)
    assert sc.starts == pytest.approx([0.0, 0.5, 1.0, 1.5, 2.0])
    assert [x['t_s'] for x in out[:-1]] == pytest.approx([0.0, 0.5, 1.0, 1.5, 2.0])
    assert out[-1]['summary'] and out[-1]['n'] == 5 and out[-1]['stopped_by'] == 'count'


def test_watch_overrun_starts_the_next_capture_at_once_without_catch_up(clock, capsys):
    sc = FakeScope(clock, [0.1, 0.8, 0.1])                 # the 2nd capture takes longer than --every
    cli.cmd_watch(args_of('watch', '-c', 'A1', '--every', '0.5s', '--count', '5'), sc)
    assert sc.starts == pytest.approx([0.0, 0.5, 1.3, 1.8, 2.3])   # no burst to get back onto 1.5 / 2.0
    capsys.readouterr()


def test_watch_for_starts_no_capture_after_the_total_time(clock, capsys):
    sc = FakeScope(clock, [0.1])
    cli.cmd_watch(args_of('watch', '-c', 'A1', '--every', '0.5s', '--for', '2s'), sc)
    out = lines_of(capsys)
    assert sc.starts == pytest.approx([0.0, 0.5, 1.0, 1.5])
    assert out[-1]['stopped_by'] == 'duration' and out[-1]['n'] == 4


def test_watch_stop_request_ends_a_long_wait(clock, capsys, monkeypatch):
    sleep = clock.sleep

    def sleep_then_stop(dt):                                 # 'openm454 stop' runs 5 s into a 60 s wait
        sleep(dt)
        if clock.t - 1000.0 > 5.0:
            open(cli.STOP_REQUEST, 'w').close()
    monkeypatch.setattr(clock, 'sleep', sleep_then_stop)
    sc = FakeScope(clock, [0.1])
    cli.cmd_watch(args_of('watch', '-c', 'A1', '--every', '60s', '--count', '5'), sc)
    out = lines_of(capsys)
    assert out[-1]['stopped_by'] == 'stop_request' and out[-1]['n'] == 1
    assert clock.t - 1000.0 < 6.0                            # not after the rest of the minute
    assert not cli.stop_requested()                          # the request was consumed


def test_watch_measure_none_and_full(clock, capsys):
    sc = FakeScope(clock, [0.1])
    cli.cmd_watch(args_of('watch', '-c', 'A1,D0', '--measure', 'none', '--count', '2', '--every', '0s'), sc)
    out = lines_of(capsys)
    assert set(out[0]) == {'i', 't_s', 'triggered'}
    with pytest.raises(DscopeError) as ei:                  # nothing to print or test: refused before capturing
        cli.cmd_watch(args_of('watch', '-c', 'A1', '--measure', 'none', '--fields', 'A1.vpp_v'), sc)
    assert ei.value.code == 'INVALID_ARG' and ei.value.exit_code == 2
    with pytest.raises(DscopeError):                        # the offline budget check refuses it before opening
        cli._budget(args_of('watch', '-c', 'A1', '--measure', 'none', '--stop-when', "A1['vpp_v'] < 1"))
    cli.cmd_watch(args_of('watch', '-c', 'A1,D0', '--measure', 'full', '--count', '2', '--every', '0s',
                          '--fields', 'A1.period_max_s,D0.pwidth_s,D0.duty_max_pct'), sc)
    out = lines_of(capsys)
    assert out[0]['A1.period_max_s'] == pytest.approx(1e-3)
    assert out[0]['D0.pwidth_s'] == pytest.approx(250e-6) and out[0]['D0.duty_max_pct'] == pytest.approx(25.0)
    assert out[-1]['fields']['A1.period_max_s']['mean'] == pytest.approx(1e-3)


# ------------------------------------------------------------------------------------------- --measure
def test_basic_is_the_previous_default_output():
    """Scripts read the default keys: basic (the default) stays exactly Capture.summary()."""
    cap = make_cap()
    assert cli._measurements(make_cap(), args_of('capture')) == cap.summary()
    assert cli._measurements(make_cap(), args_of('capture', '--edges', '3')) == cap.summary(edges_n=3)
    win = (-2e-3, 2e-3)
    assert cli._measurements(make_cap(), args_of('capture'), win) == cap.summary(window=win)


def test_default_output_keeps_the_keys_the_mcp_server_summarizes():
    from openm454.mcp_server import A_KEYS, D_KEYS
    s = cli._measurements(make_cap(), args_of('capture'))
    assert set(A_KEYS) <= set(s['analog']['A1']) and set(D_KEYS) <= set(s['digital']['D0'])


def test_full_adds_cycle_statistics_and_the_visible_range():
    basic = cli._measurements(make_cap(), args_of('capture'))
    full = cli._measurements(make_cap(), args_of('capture', '--measure', 'full'))
    for grp, ch in (('analog', 'A1'), ('digital', 'D0')):
        b, f = basic[grp][ch], full[grp][ch]
        assert {k: f[k] for k in b} == b                     # every basic value unchanged
        assert 'edges_rise_s' not in f and 'edges_fall_s' not in f      # only with --edges
        assert f['cycles'] == 9
        assert f['period_min_s'] == pytest.approx(1e-3) and f['period_max_s'] == pytest.approx(1e-3)
        assert f['period_std_s'] < 1e-9
        assert f['freq_min_hz'] == pytest.approx(1e3) and f['freq_max_hz'] == pytest.approx(1e3)
        assert f['duty_min_pct'] == pytest.approx(25.0) and f['duty_max_pct'] == pytest.approx(25.0)
        assert f['pwidth_min_s'] == pytest.approx(250e-6) and f['nwidth_max_s'] == pytest.approx(750e-6)
    assert full['digital']['D0']['pwidth_s'] == pytest.approx(250e-6)        # digital gains the mean widths
    a = full['analog']['A1']
    assert a['range_lo_v'] == pytest.approx((10 - 128) * VPC, abs=1e-3)
    assert a['range_hi_v'] == pytest.approx((245 - 128) * VPC, abs=1e-3)
    assert 'range_lo_v' not in basic['analog']['A1']
    f3 = cli._measurements(make_cap(), args_of('capture', '--measure', 'full', '--edges', '3'))
    assert f3['digital']['D0']['edges_rise_s'] == basic_edges('D0', 3)


def basic_edges(ch, n):
    return cli._measurements(make_cap(), args_of('capture', '--edges', str(n)))['digital'][ch]['edges_rise_s']


def test_full_shows_period_jitter_that_the_mean_hides():
    bits = square(n=12_000, jitter=[1000, 1000, 1200, 1000])
    s = cli._measurements(make_cap(bits), args_of('capture', '--measure', 'full'))
    d = s['digital']['D0']
    assert d['period_min_s'] == pytest.approx(1e-3) and d['period_max_s'] == pytest.approx(1.2e-3)
    assert d['period_std_s'] > 0 and d['freq_min_hz'] == pytest.approx(1e3 / 1.2)
    assert d['duty_min_pct'] == pytest.approx(25.0 / 1.2, rel=1e-3)


def test_full_skips_cycle_statistics_without_cycles():
    flat = np.zeros(5000, np.uint8)
    s = cli._measurements(make_cap(flat), args_of('capture', '--measure', 'full'))
    assert 'cycles' not in s['digital']['D0'] and s['digital']['D0']['state'] == 'low'
    assert 'range_hi_v' in s['analog']['A1']


def test_capture_measure_none_reports_no_measurements(capsys):
    scope = types.SimpleNamespace(capture=lambda st: make_cap())
    cli.cmd_capture(args_of('capture', '-c', 'A1,D0', '--measure', 'none', '--no-save', '--no-plot'), scope)
    res = lines_of(capsys)[-1]
    assert res['ok'] and 'analog' not in res and 'digital' not in res and res['files'] == {}
    cli.cmd_capture(args_of('capture', '-c', 'A1,D0', '--no-save', '--no-plot'), scope)
    res = lines_of(capsys)[-1]
    assert 'cycles' not in res['digital']['D0'] and res['analog']['A1']['freq_hz'] == pytest.approx(1e3)


def test_analyze_measure_full_and_channels(tmp_path, capsys):
    path = str(tmp_path / 'x.npz')
    make_cap().save(path)
    assert cli.main(['analyze', path, '--measure', 'full', '--channels', 'D0']) == 0
    res = lines_of(capsys)[-1]
    assert res['analog'] == {} and res['digital']['D0']['cycles'] == 9
    assert cli.main(['analyze', path]) == 0
    res = lines_of(capsys)[-1]
    assert 'cycles' not in res['analog']['A1'] and 'D0' in res['digital']


# ------------------------------------------------------------------------------------------- help texts
def _walk(p, path=()):
    for a in p._actions:
        if isinstance(a, argparse._SubParsersAction):
            helped = {c.dest for c in a._choices_actions}
            for name, sp in a.choices.items():
                yield path + (name,), name in helped
                yield from _walk(sp, path + (name,))
        elif not isinstance(a, argparse._HelpAction):
            yield path + (a.option_strings[-1] if a.option_strings else a.dest,), bool(a.help)


def test_every_option_and_subcommand_has_help():
    missing = [' '.join(p) for p, ok in _walk(cli.build_parser()) if not ok]
    assert missing == []
    for argv in (['capture', '--help'], ['watch', '--help'], ['stream', '--help'], ['analyze', '--help'],
                 ['awg', 'set', '--help'], ['debug', 'plan', '--help']):
        with pytest.raises(SystemExit) as ei:                # help texts must format (a lone % breaks argparse)
            cli.build_parser().parse_args(argv)
        assert ei.value.code == 0


def test_defaults_are_unchanged():
    a = args_of('capture')
    assert (a.measure, a.edges, a.dump, a.trig_mode, a.trig_pos, a.auto_after, a.timeout, a.holdoff, a.sensitivity,
            a.tag, a.decode_limit, a.vdiv, a.offset, a.threshold) == \
        ('basic', 0, 0, 'auto', '50%', '0.3s', '5s', '0s', 5, 'cap', 50, '1V', '0V', '1.4V')
    w = args_of('watch')
    assert (w.every, w.count, w.duration_total, w.measure) == ('0.5s', None, None, 'basic')
    s = args_of('stream')
    assert (s.every, s.tag, s.measure) == ('1s', 'cap', 'basic')
    assert args_of('analyze', 'x.npz').measure == 'basic'
    lg = args_of('log')
    assert (lg.rate, lg.duration, lg.every, lg.tag) == ('1M', '10s', '1s', 'log')
    aw = args_of('awg', 'set')
    assert (aw.wave, aw.freq, aw.duty, aw.phase, aw.high, aw.low) == ('sine', '1k', '50%', '0', None, None)


def test_prune_only_removes_openm454_captures(tmp_path):
    import os
    import time as _t
    from openm454.capture import prune
    own = []
    for i in range(5):
        stem = tmp_path / ('20261107-0000%02d-000_cap' % i)
        for ext in ('.npz', '.png', '_decode.json'):
            p = str(stem) + ext
            open(p, 'w').close()
            os.utime(p, (1000 + i, 1000 + i))
        own.append(stem)
    user = ['notes.json', 'scope.png', 'mydata.npz', '20261107-000000_other.json', 'readme.txt']
    for n in user:
        p = tmp_path / n
        p.write_text('x')
        os.utime(p, (1, 1))                                            # older than every capture
    prune(str(tmp_path), keep=2)
    left = sorted(os.listdir(tmp_path))
    assert all(n in left for n in user)                                # never touches other files
    assert [n for n in left if n.endswith('.npz') and n[0].isdigit()] == ['20261107-000003-000_cap.npz',
                                                                          '20261107-000004-000_cap.npz']
    assert '20261107-000004-000_cap_decode.json' in left and '20261107-000000-000_cap.png' not in left


def test_frequency_from_falling_edges_when_only_one_rise_is_complete():
    # 1 kHz square, 50 % duty, 2 ms window cut just after a rise: rises at 1 us (incomplete) and 1.0 ms, falls at 0.5
    # and 1.5 ms (the window the 12-channel capture on 2026-11-07 had: freq was missing)
    import numpy as np
    from openm454 import measure
    rate = 50e6
    t = np.arange(int(2e-3 * rate)) / rate
    v = np.where(((t - 0.00000002) % 1e-3) < 0.5e-3, 2.8, 0.0)
    m = measure.analog(v, rate) if hasattr(measure, 'analog') else None
    if m is None:
        from openm454.capture import Capture
        cap = Capture({'analog_rate_hz': rate, 't0_analog_s': 0.0, 'digital_rate_hz': None, 't0_digital_s': 0.0,
                       'channels': {'A1': {'zero_code': 30.0, 'volts_per_code': 3.0 / 150, 'probe': 1.0}}},
                      analog={'A1': (30 + v / (3.0 / 150)).round().astype(np.uint8)})
        m = cap.measure('A1')
    assert m['freq_hz'] == pytest.approx(1000, rel=1e-3) and m['duty_pct'] == pytest.approx(50, abs=1)


def test_prune_by_tag_spares_log_recordings(tmp_path):
    import os
    from openm454.capture import prune
    for i, tag in enumerate(['log'] + ['cap'] * 4):
        p = tmp_path / ('20261107-0000%02d-000_%s.npz' % (i, tag))
        p.write_text('x')
        os.utime(p, (1000 + i, 1000 + i))
    prune(str(tmp_path), keep=2, tag='cap')
    left = sorted(os.listdir(tmp_path))
    assert '20261107-000000-000_log.npz' in left                       # oldest, but another tag: kept
    assert [n for n in left if n.endswith('_cap.npz')] == ['20261107-000003-000_cap.npz', '20261107-000004-000_cap.npz']


def test_measure_full_with_edges_on_an_empty_window(tmp_path):
    import numpy as np
    from openm454 import cli
    from openm454.capture import Capture
    rate = 1e6
    t = np.arange(20000) / rate
    cap = Capture({'analog_rate_hz': rate, 't0_analog_s': 0.0, 'digital_rate_hz': None, 't0_digital_s': 0.0,
                   'channels': {'A1': {'zero_code': 60.0, 'volts_per_code': 3.0 / 140, 'probe': 1.0}}},
                  analog={'A1': np.where((t * 1000) % 1 < 0.5, 200, 60).astype(np.uint8)})
    f = str(tmp_path / 'c.npz')
    cap.save(f)
    rc = cli.main(['analyze', f, '--measure', 'full', '--edges', '3', '--window', '1s:2s'])
    assert rc == 0
