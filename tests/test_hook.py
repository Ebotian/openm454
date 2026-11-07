"""log --hook (openm454/hook.py): user code on every piece of a gap-free recording, without hardware."""
import json
import textwrap
import time

import numpy as np
import pytest

from openm454 import cli, host, logger
from openm454 import mcp_server as M
from openm454.capture import load_capture
from openm454.errors import DscopeError
from openm454.hook import Block, Hook

from test_logger import A2D8, FakeScope, fake_signal, make_plan, settings


@pytest.fixture(autouse=True)
def no_host_file(tmp_path, monkeypatch):
    monkeypatch.setattr(host, 'HOST_FILE', str(tmp_path / 'host.json'))
    monkeypatch.setattr(cli, 'STOP_REQUEST', str(tmp_path / 'stop.request'))


def hook_file(tmp_path, body, name='h.py'):
    p = tmp_path / name
    p.write_text(textwrap.dedent(body))
    return str(p)


COUNTER = '''
    import numpy as np
    S = {'t1': 0.0, 'n': 0, 'calls': 0, 'gaps': 0, 'setup': None}
    def setup(info):
        S['setup'] = info
    def process(block):
        S['calls'] += 1
        if abs(block.t0_s - S['t1']) > 1e-12:
            S['gaps'] += 1
        S['t1'] = block.t1_s
        S['n'] += block.n
        v = block.volts('A2')
        b = block.bits('D3')
        assert v.dtype == np.float32 and len(v) == block.n and len(b) == block.n and set(np.unique(b)) <= {0, 1}
        if S['calls'] % 10 == 0:
            return {'calls': S['calls'], 'vmax': float(v.max()), 'stop': S['calls'] >= STOP_AT}
    def finish(summary):
        return {'n': S['n'], 'gaps': S['gaps'], 'calls': S['calls'], 'samples': summary['samples']}
    STOP_AT = 30
'''


class Paced:
    """FakeScope clock: one transfer per ms, so the data does not outrun the writer (a real scope sends in real time)."""

    def tick(self):
        time.sleep(0.001)


def test_hook_sees_every_sample_in_order_and_can_stop(tmp_path):
    pl = make_plan(*A2D8, 1e6, None, every=0.05)
    layout, an, dg, stream = fake_signal(pl, 0.5)
    h = Hook(hook_file(tmp_path, COUNTER))
    lines = []
    res = logger.run(FakeScope(stream, pl['packet_bytes'], clock=Paced()), settings(*A2D8), pl, None,
                     emit=lines.append, hook=h)
    assert res['ok'] and res['acq']['stop_reason'] == 'hook'
    events = [l for l in lines if 'event' in l]
    assert [e['event']['calls'] for e in events] == [10, 20, 30] and [e['k'] for e in events] == [0, 1, 2]
    assert events[-1]['event']['stop'] is True and all('i' not in e for e in events)
    r = res['hook']['result']
    assert r['gaps'] == 0 and r['calls'] == res['hook']['calls'] >= 30
    assert r['samples'] == res['acq']['samples'] and 0 < r['n'] <= r['samples']   # in order, no gap, until the stop
    assert res['hook']['events'] == 3 and res['hook']['stopped_run'] and res['hook']['load'] is not None
    assert any('hook_ms' in l for l in lines if 'i' in l)
    S = h.process.__globals__['S']
    assert S['setup']['rate_hz'] == 1e6 and S['setup']['analog'] == ['A2', 'A3'] and 'A2' in S['setup']['scale']
    assert S['setup']['piece_s'] <= 0.0101                             # ~10 ms pieces


def test_hook_volts_and_bits_match_the_recording(tmp_path):
    pl = make_plan(*A2D8, 100e3, 0.3, tmp_path)
    layout, an, dg, stream = fake_signal(pl, 0.5)
    got = {'v': [], 'b': []}
    h = Hook(hook_file(tmp_path, 'def process(block):\n    pass\n'))

    def process(block):
        got['v'].append(block.volts('A2').copy())
        got['b'].append(block.bits('D5').copy())
        assert block.codes('A2').dtype == np.uint8 and block.times()[0] == block.t0_s
    h.process = process
    res = logger.run(FakeScope(stream, pl['packet_bytes']), settings(*A2D8), pl, str(tmp_path / 'r'), hook=h)
    cap = load_capture(res['files']['npz'])
    np.testing.assert_allclose(np.concatenate(got['v']), cap.volts('A2'), atol=1e-4)
    np.testing.assert_array_equal(np.concatenate(got['b']), cap.digital['D5'])


def test_a_failing_hook_stops_the_run_and_keeps_the_data(tmp_path):
    pl = make_plan(*A2D8, 100e3, 1.0, tmp_path)
    layout, an, dg, stream = fake_signal(pl, 1.5)
    h = Hook(hook_file(tmp_path, '''
        N = [0]
        def process(block):
            N[0] += 1
            if N[0] == 5:
                raise ValueError('boom')
    '''))
    res = logger.run(FakeScope(stream, pl['packet_bytes']), settings(*A2D8), pl, str(tmp_path / 'e'), hook=h)
    assert not res['ok'] and res['error']['code'] == 'HOOK_ERROR' and 'boom' in res['error']['message']
    assert res['acq']['stop_reason'] == 'hook_error' and any('boom' in l for l in res['error']['trace'])
    assert len(load_capture(res['files']['npz']).analog['A2']) > 0           # the data so far is kept


def test_bad_hook_files_are_refused_before_the_run(tmp_path):
    for body, why in (('def processs(block):\n    pass\n', 'no process'), ('def process(block)\n    pass\n', 'cannot be loaded'),
                      ('import no_such_module_xyz\ndef process(b):\n    pass\n', 'cannot be loaded')):
        with pytest.raises(DscopeError) as ei:
            Hook(hook_file(tmp_path, body, 'bad.py'))
        assert ei.value.code == 'INVALID_ARG' and why in ei.value.message
    with pytest.raises(DscopeError) as ei:
        Hook(str(tmp_path / 'missing.py'))
    assert ei.value.code == 'INVALID_ARG'
    rc = cli.main(['log', '-c', 'A1', '--rate', '1M', '--duration', '1s', '--storage', 'none', '--plan',
                   '--hook', str(tmp_path / 'bad.py')])
    assert rc == 2


def test_setup_errors_and_wrong_return_types(tmp_path):
    pl = make_plan(*A2D8, 1e6, 0.1)
    layout, an, dg, stream = fake_signal(pl, 0.2)
    h = Hook(hook_file(tmp_path, 'def setup(info):\n    1 / 0\ndef process(block):\n    pass\n'))
    with pytest.raises(DscopeError) as ei:
        logger.run(FakeScope(stream, pl['packet_bytes']), settings(*A2D8), pl, None, hook=h)
    assert ei.value.code == 'HOOK_ERROR'
    h = Hook(hook_file(tmp_path, 'def process(block):\n    return 3\n', 'r.py'))
    res = logger.run(FakeScope(stream, pl['packet_bytes']), settings(*A2D8), pl, None, hook=h)
    assert res['error']['code'] == 'HOOK_ERROR' and 'None or a dict' in res['error']['message']


def test_block_helpers():
    scale = {'A1': {'zero_code': 100.0, 'volts_per_code': 0.02, 'probe': 10.0}}
    bits = np.array([1, 0, 1, 1, 0, 0, 0, 1, 1, 1], np.uint8)
    b = Block({'A1': np.array([100, 150, 50], np.uint8), 'D0': np.packbits(bits, bitorder='little')}, 1.0, 3, 1e3,
              scale, ['A1'], ['D0'])
    np.testing.assert_allclose(b.volts('A1'), [0.0, 10.0, -10.0])
    assert list(b.bits('D0')) == [1, 0, 1] and b.t1_s == pytest.approx(1.003)
    np.testing.assert_allclose(b.times(), [1.0, 1.001, 1.002])


def test_mcp_record_start_passes_the_hook(tmp_path):
    hp = str(tmp_path / 'h.py')
    args = M.record_args({'mode': 'log', 'rate': '1M', 'hook': hp}, plan=False)
    assert args[args.index('--hook') + 1] == hp
    for bad in ({'mode': 'stream', 'hook': hp}, {'mode': 'log', 'hook': 'h.py'}):     # stream / relative path
        with pytest.raises(M.ToolError):
            M.record_args(bad)
    ns = cli.build_parser().parse_args(M.record_args({'mode': 'log', 'rate': '1M', 'hook': hp}))
    assert ns.hook == hp


def test_print_in_a_hook_goes_to_stderr(tmp_path, capsys):
    h = Hook(hook_file(tmp_path, '''
        print("loading")
        def process(block):
            print("piece", block.n)
    '''))
    h.call(Block({}, 0.0, 64, 1e6, {}, [], []))
    out, err = capsys.readouterr()
    assert out == '' and 'loading' in err and 'piece 64' in err


def test_numpy_values_in_events_are_printed_as_json(tmp_path):
    pl = make_plan(*A2D8, 1e6, 0.05)
    layout, an, dg, stream = fake_signal(pl, 0.1)
    h = Hook(hook_file(tmp_path, '''
        import numpy as np
        def process(block):
            c = block.codes('A2')
            return {'min': c.min(), 'hist': np.bincount(c, minlength=2)[:2], 'nan': float('nan'), 'hi': c.max() > 100}
    '''))
    lines = []
    res = logger.run(FakeScope(stream, pl['packet_bytes']), settings(*A2D8), pl, None, emit=lines.append, hook=h)
    ev = [l for l in lines if 'event' in l]
    assert res['ok'] and ev and isinstance(ev[0]['event']['min'], int) and ev[0]['event']['nan'] is None
    assert ev[0]['event']['hi'] is True and isinstance(ev[0]['event']['hist'], list)
    json.dumps(ev)                                                      # what the CLI prints
    h = Hook(hook_file(tmp_path, 'def process(block):\n    return {"x": {1, 2}}\n', 'set.py'))
    res = logger.run(FakeScope(stream, pl['packet_bytes']), settings(*A2D8), pl, None, hook=h)
    assert res['error']['code'] == 'HOOK_ERROR' and 'JSON' in res['error']['message']


def test_the_hook_cannot_change_the_recording(tmp_path):
    pl = make_plan(*A2D8, 100e3, 0.3, tmp_path)
    layout, an, dg, stream = fake_signal(pl, 0.5)
    h = Hook(hook_file(tmp_path, """
        def process(block):
            c = block.codes('A2')
            c -= c
    """))
    res = logger.run(FakeScope(stream, pl['packet_bytes']), settings(*A2D8), pl, str(tmp_path / 'ro'), hook=h)
    assert res['error']['code'] == 'HOOK_ERROR' and 'read-only' in res['error']['message']
    cap = load_capture(res['files']['npz'])
    assert cap.analog['A2'].max() == 200                                # the stored data is untouched


def test_sys_exit_in_a_hook_is_a_hook_error(tmp_path):
    pl = make_plan(*A2D8, 1e6, 0.1)
    layout, an, dg, stream = fake_signal(pl, 0.2)
    h = Hook(hook_file(tmp_path, 'import sys\ndef process(block):\n    sys.exit(3)\n'))
    res = logger.run(FakeScope(stream, pl['packet_bytes']), settings(*A2D8), pl, None, hook=h)
    assert res['error']['code'] == 'HOOK_ERROR' and 'SystemExit' in res['error']['message']
    with pytest.raises(DscopeError) as ei:                              # at load time too: refused, not an exit
        Hook(hook_file(tmp_path, 'import sys\nsys.exit(1)\ndef process(b):\n    pass\n', 'x.py'))
    assert ei.value.code == 'INVALID_ARG'


def test_a_hook_stop_ends_the_recording_at_that_piece(tmp_path):
    pl = make_plan(*A2D8, 1e6, None)
    layout, an, dg, stream = fake_signal(pl, 0.5)
    h = Hook(hook_file(tmp_path, """
        T = {}
        def process(block):
            if block.t1_s >= 0.1 and 't' not in T:
                T['t'] = block.t1_s
                return {'stop': True}
    """))
    res = logger.run(FakeScope(stream, pl['packet_bytes']), settings(*A2D8), pl, None, hook=h)   # unpaced: floods
    t_stop = h.process.__globals__['T']['t']
    assert res['ok'] and res['acq']['stop_reason'] == 'hook'
    assert res['acq']['duration_s'] == pytest.approx(t_stop, abs=1e-9)  # nothing after the stopping piece


def test_hook_files_can_use_dataclasses_and_their_own_helpers(tmp_path):
    (tmp_path / 'myhelper.py').write_text('LIMIT = 7\n')
    h = Hook(hook_file(tmp_path, """
        from dataclasses import dataclass
        import myhelper
        @dataclass
        class State:
            n: int = 0
        S = State()
        def process(block):
            S.n += 1
            return {'limit': myhelper.LIMIT, 'n': S.n}
    """))
    assert h.call(Block({}, 0.0, 64, 1e6, {}, [], [])) == {'limit': 7, 'n': 1}


class Broken(FakeScope):
    """A device error after a few transfers: the hook still gets finish() and its report is kept."""

    def _run(self, prog, geom, trigger_timeout, collect_timeout, **kw):
        on = kw['on_chunk']
        n = [0]

        def chunk(b):
            n[0] += 1
            if n[0] > 6:
                raise DscopeError('USB_ERROR', 'gone')
            on(b)
        kw['on_chunk'] = chunk
        return super()._run(prog, geom, trigger_timeout, collect_timeout, **kw)


def test_device_error_keeps_the_hook_report(tmp_path):
    pl = make_plan(*A2D8, 1e6, None)
    layout, an, dg, stream = fake_signal(pl, 0.5)
    h = Hook(hook_file(tmp_path, COUNTER))
    with pytest.raises(DscopeError) as ei:
        logger.run(Broken(stream, pl['packet_bytes']), settings(*A2D8), pl, None, hook=h)
    rep = ei.value.hook_report
    assert rep['calls'] >= 1 and rep['result']['calls'] == rep['calls']
