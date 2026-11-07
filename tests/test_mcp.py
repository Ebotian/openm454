"""MCP server (openm454/mcp_server.py) without hardware: the protocol, summaries, errors, timeouts and recordings
against a fake CLI, and the offline commands through the real CLI."""
import io
import json
import os
import sys
import textwrap
import time

import numpy as np
import pytest

from openm454 import mcp_server as M
from openm454.capture import Capture

FAKE = textwrap.dedent(r'''
    import json, os, sys, time
    args = sys.argv[1:]
    with open(os.environ['FAKE_LOG'], 'a') as f:
        f.write(json.dumps(args) + '\n')
    def out(d, code=0):
        print(json.dumps(d), flush=True)
        sys.exit(code)
    cmd = args[0]
    if cmd == 'info':
        if os.environ.get('FAKE_NODEV'):
            out({'ok': False, 'cmd': 'info', 'error': {'code': 'NOT_FOUND', 'message': 'DSCope M454 (2A0E:0080) not '
                 'found', 'hint': 'check the USB cable'}}, 3)
        out({'ok': True, 'cmd': 'info', 'device': {'model': 'DSCope M454'}, 'limits': {'x': 1}, 'awg_last': None})
    if cmd == 'capture':
        if '--trig' in args and os.environ.get('FAKE_NOTRIG'):
            out({'ok': False, 'cmd': 'capture', 'error': {'code': 'TIMEOUT_NO_TRIGGER', 'message': 'no trigger '
                 'within 5 s', 'hint': 'check the level'}}, 5)
        frames = [{'t_s': i * 1e-4, 'value': i % 256} for i in range(120)]
        out({'ok': True, 'cmd': 'capture',
             'applied': {'channels': ['A1', 'D0'], 'vdiv_v': {'A1': 1.0}, 'offset_v': {'A1': 0.0},
                         'probe': {'A1': 1.0}, 'coupling': {'A1': 'dc'}, 'bw20': [], 'invert': [],
                         'threshold_v': 1.4, 'trigger': {'source': 'A1', 'edge': 'rise', 'mode': 'normal',
                                                         'pos_pct': 50.0, 'level_v': 1.4},
                         'duration_req_s': 0.002},
             'acq': {'analog_rate_hz': 1e8, 'digital_rate_hz': 1e8, 'samples': 200000, 'duration_s': 0.002,
                     'triggered': True, 'attempts': ['triggered'], 'stop': 'normal', 'handshakes': 1,
                     'complete': True, 'elapsed_ms': 400},
             'analog': {'A1': {'samples': 200000, 'vmin_v': 0.0, 'vmax_v': 2.9, 'vpp_v': 2.9, 'vmean_v': 1.4,
                               'vrms_v': 2.0, 'freq_hz': 1000.0, 'duty_pct': 50.0, 'rise_s': 8e-6, 'fall_s': 3e-7,
                               'period_s': 1e-3, 'clip_lo': False, 'clip_hi': True, 'overshoot_pct': 1.0}},
             'digital': {'D0': {'samples': 200000, 'state': 'toggling', 'high_frac': 0.5, 'edges': 4,
                                'freq_hz': 1000.0, 'period_s': 1e-3, 'duty_pct': 50.0}},
             'dump': {'A1': list(range(5000))},
             'decode': [{'protocol': 'uart', 'count': 120, 'errors': 0, 'text': 'x' * 1000, 'frames': frames}],
             'files': {'npz': 'captures/x.npz', 'png': 'captures/x.png'}, 'hints': [], 'warnings': []})
    if cmd == 'sleep':
        time.sleep(float(args[1]))
        out({'ok': True, 'cmd': 'sleep'})
    if cmd == 'analyze' and os.environ.get('FAKE_SLOW'):
        time.sleep(30)
    if cmd == 'awg':
        out({'ok': True, 'cmd': 'awg', 'awg': {'wave': 'sine'}, 'persists_after_exit': True})
    if cmd in ('log', 'stream'):
        if '--rate' in args and args[args.index('--rate') + 1] == '100M':
            out({'ok': False, 'cmd': 'log', 'error': {'code': 'NEEDS_FORCE', 'message': 'gap-free for 2.9 s only',
                 'hint': 'add --force', 'plan': {'gapfree_max_s': 2.9, 'recommendation': 'log --force'}}}, 2)
        if '--rate' in args and args[args.index('--rate') + 1] == 'crash':
            sys.stderr.write('Traceback ...\nRuntimeError: simulated crash\n')
            sys.exit(1)
        print(json.dumps({'ok': True, 'cmd': cmd, 'started': True, 'plan': {'verdict': 'sustained', '_x': 1},
                          'out_stem': 'data/x'}), flush=True)
        i = 0
        while not os.path.exists(os.environ['FAKE_STOP']):
            print(json.dumps({'i': i, 't_signal_s': i * 0.05, 'backlog_mb': 0.5}), flush=True)
            i += 1
            time.sleep(0.05)
        out({'ok': True, 'cmd': cmd, 'acq': {'mode': 'loop', 'samples': 1000, 'duration_s': i * 0.05,
                                              'stop_reason': 'stop_request'},
             'analog': {'A1': {'vmin_v': 0.0, 'vmax_v': 2.9, 'vmean_v': 1.4}}, 'files': {'npz': 'data/x.npz'}})
    out({'ok': False, 'cmd': cmd, 'error': {'code': 'INVALID_ARG', 'message': 'unknown'}}, 2)
''')


@pytest.fixture
def fake(tmp_path, monkeypatch):
    script = tmp_path / 'fake_cli.py'
    script.write_text(FAKE)
    monkeypatch.setenv('FAKE_LOG', str(tmp_path / 'argv.log'))
    stop = tmp_path / 'stop.request'
    monkeypatch.setenv('FAKE_STOP', str(stop))
    monkeypatch.setattr(M, 'STOP_REQUEST', str(stop))
    monkeypatch.setattr(M, 'JOBS_DIR', str(tmp_path / 'jobs'))
    srv = M.Server(cli=M.Cli([sys.executable, str(script)]), out=io.BytesIO())
    srv.argv = lambda: [json.loads(l) for l in open(tmp_path / 'argv.log')]
    return srv


def replies(srv):
    return [json.loads(l) for l in srv.out.getvalue().decode().splitlines()]


def call(srv, name, args=None, mid=1):
    srv.call(mid, name, args or {})
    r = [m for m in replies(srv) if m.get('id') == mid][-1]
    return r['result']


# ------------------------------------------------------------------------------------------------ protocol
def test_initialize_list_and_versions(fake):
    fake.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                   'params': {'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 't'}}})
    fake.dispatch({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    fake.dispatch({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
    fake.dispatch({'jsonrpc': '2.0', 'id': 3, 'method': 'ping'})
    fake.dispatch({'jsonrpc': '2.0', 'id': 4, 'method': 'resources/list'})
    r = {m['id']: m for m in replies(fake)}
    assert r[1]['result']['protocolVersion'] == '2025-03-26' and r[1]['result']['serverInfo']['name'] == 'openm454'
    names = [t['name'] for t in r[2]['result']['tools']]
    assert names[:2] == ['status', 'capture'] and 'record_start' in names
    assert all(t['inputSchema']['type'] == 'object' for t in r[2]['result']['tools'])
    assert r[3]['result'] == {} and r[4]['error']['code'] == -32601
    assert 'structuredContent' not in call(fake, 'awg', {'action': 'off'}, mid=5)     # 2025-03-26: text only
    fake.version = '2025-06-18'
    assert call(fake, 'awg', {'action': 'off'}, mid=6)['structuredContent']['ok']


def test_serve_reads_lines_and_answers_calls_in_threads(fake):
    msgs = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2099-01-01'}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'status', 'arguments': {}}}]
    fake.serve(io.BytesIO(b'not json\n' + b''.join(json.dumps(m).encode() + b'\n' for m in msgs)))
    t_end = time.time() + 20
    while time.time() < t_end and not any(m.get('id') == 2 for m in replies(fake)):
        time.sleep(0.05)
    r = {m.get('id'): m for m in replies(fake)}
    assert r[None]['error']['code'] == -32700                     # the bad line, the server keeps reading
    assert r[1]['result']['protocolVersion'] == M.PROTOCOL_VERSIONS[0]
    assert r[2]['result']['structuredContent'] == {'ok': True, 'cmd': 'info', 'connected': True,
                                                   'device': {'model': 'DSCope M454'}, 'awg_last': None}


# ------------------------------------------------------------------------------------------------ tools
def test_capture_returns_a_summary_not_samples(fake):
    fake.version = '2025-06-18'
    res = call(fake, 'capture', {'channels': 'A1,D0', 'trigger': 'A1:rise:1.4V', 'timebase': '200us'})
    doc = res['structuredContent']
    assert not res['isError'] and 'dump' not in doc and 'dump' not in res['content'][0]['text']
    assert doc['analog']['A1'] == {'freq_hz': 1000.0, 'vpp_v': 2.9, 'vmin_v': 0.0, 'vmax_v': 2.9, 'vmean_v': 1.4,
                                   'duty_pct': 50.0, 'rise_s': 8e-6, 'fall_s': 3e-7, 'clip_hi': True}
    assert doc['digital']['D0']['state'] == 'toggling' and doc['acq']['triggered'] is True
    assert doc['trigger']['level_v'] == 1.4 and doc['files']['npz'] == 'captures/x.npz'
    d = doc['decode'][0]
    assert d['count'] == 120 and len(d['frames']) == M.DECODE_FRAMES and len(d['text']) < 500
    argv = fake.argv()[-1]
    assert argv[argv.index('--trig-mode') + 1] == 'normal' and argv[argv.index('--timeout') + 1] == '5s'
    call(fake, 'capture', {'channels': 'A1', 'save': False, 'plot': False}, mid=2)
    argv = fake.argv()[-1]
    assert argv[argv.index('--trig-mode') + 1] == 'force' and '--no-save' in argv and '--no-plot' in argv
    full = call(fake, 'capture', {'channels': 'A1', 'detail': 'full'}, mid=3)
    assert 'dump' in full['structuredContent']


def test_errors_are_explicit(fake, monkeypatch):
    fake.version = '2025-06-18'
    monkeypatch.setenv('FAKE_NOTRIG', '1')
    res = call(fake, 'capture', {'trigger': 'A1:rise:1.4V', 'timeout_s': 2})
    assert res['isError'] and res['content'][0]['text'].startswith('TIMEOUT_NO_TRIGGER: no trigger')
    assert 'hint: check the level' in res['content'][0]['text']
    argv = fake.argv()[-1]
    assert argv[argv.index('--timeout') + 1] == '2s'
    monkeypatch.setenv('FAKE_NODEV', '1')
    res = call(fake, 'status', mid=2)
    assert res['isError'] and res['structuredContent']['error']['code'] == 'NOT_FOUND'
    res = call(fake, 'capture', {'trig_mode': 'auto'}, mid=3)            # auto without a trigger
    assert res['isError'] and res['structuredContent']['error']['code'] == 'INVALID_ARG'
    fake.call(4, 'no_such_tool', {})
    assert [m for m in replies(fake) if m.get('id') == 4][0]['error']['code'] == -32602


def test_a_hung_command_never_hangs_the_call(fake, monkeypatch):
    monkeypatch.setattr(M, 'STOP_GRACE_S', 0.3)
    t0 = time.time()
    with pytest.raises(M.ToolError) as ei:                        # a scope command: left to the CLI's watchdog
        fake.cli.run(['sleep', '3'], 0.5)
    assert time.time() - t0 < 2.5 and ei.value.doc['error']['code'] == 'TIMEOUT_WATCHDOG'
    assert 'watchdog' in ei.value.doc['error']['hint'] and not os.path.exists(M.STOP_REQUEST)
    monkeypatch.setenv('FAKE_SLOW', '1')
    t0 = time.time()
    with pytest.raises(M.ToolError) as ei:                        # offline: killed, a recording is not disturbed
        fake.cli.run(['analyze', 'x.npz'], 0.5)
    assert time.time() - t0 < 2.5 and 'stopped' in ei.value.doc['error']['message']
    assert not os.path.exists(M.STOP_REQUEST)
    doc = fake.cli.run(['stream', '-c', 'D0', '--duration', 'forever'], 0.5)   # stoppable: ends cleanly
    assert doc['ok'] and doc['acq']['stop_reason'] == 'stop_request'


def test_background_recording(fake):
    fake.version = '2025-06-18'
    start = call(fake, 'record_start', {'mode': 'log', 'channels': 'A1', 'rate': '1M', 'duration': 'forever'})
    s = start['structuredContent']
    assert not start['isError'] and s['running'] and s['plan'] == {'verdict': 'sustained'}
    assert fake.argv()[-1][:3] == ['log', '-c', 'A1'] and '--plan' not in fake.argv()[-1]
    busy = call(fake, 'capture', {}, mid=2)                       # the scope is busy meanwhile
    assert busy['isError'] and busy['structuredContent']['error']['code'] == 'BUSY'
    time.sleep(0.4)
    st = call(fake, 'record_status', {'lines': 2}, mid=3)['structuredContent']
    assert st['running'] and len(st['last']) == 2 and 'i' in st['last'][-1]
    stop = call(fake, 'record_stop', {}, mid=4)['structuredContent']
    assert stop['ok'] and not stop['running'] and stop['acq']['stop_reason'] == 'stop_request'
    assert stop['files']['npz'] == 'data/x.npz'
    assert call(fake, 'record_status', {}, mid=5)['structuredContent']['result']['ok']
    refused = call(fake, 'record_start', {'mode': 'log', 'rate': '100M', 'duration': '1h'}, mid=6)
    e = refused['structuredContent']
    assert refused['isError'] and e['error']['code'] == 'NEEDS_FORCE' and e['plan']['gapfree_max_s'] == 2.9


def test_bad_messages_never_end_the_server(fake):
    lines = [b'1', b'[]', b'{"jsonrpc":"2.0","id":7,"method":"initialize","params":[1]}',
             b'{"jsonrpc":"2.0","id":8,"method":"tools/call","params":{"name":"status","arguments":[1]}}',
             b'{"jsonrpc":"2.0","id":9,"method":7}', b'{"jsonrpc":"2.0","id":10,"method":"ping"}']
    fake.serve(io.BytesIO(b'\n'.join(lines) + b'\n'))
    r = replies(fake)
    assert [m['error']['code'] for m in r if m.get('id') is None] == [-32600, -32600]
    by_id = {m['id']: m for m in r if m.get('id') is not None}
    assert by_id[7]['error']['code'] == -32602 and by_id[8]['error']['code'] == -32602
    assert by_id[9]['error']['code'] == -32600 and by_id[10]['result'] == {}


def test_recordings_survive_a_server_restart(fake, tmp_path):
    fake.version = '2025-06-18'
    job = call(fake, 'record_start', {'mode': 'stream', 'channels': 'D0', 'duration': 'forever'})['structuredContent']
    other = M.Server(cli=fake.cli, out=io.BytesIO())              # e.g. the client restarted the server
    other.version = '2025-06-18'
    assert job['job'] in other.jobs and other.jobs[job['job']].running()
    busy = call(other, 'status', mid=2)
    assert busy['isError'] and busy['structuredContent']['error']['code'] == 'BUSY'
    stop = call(other, 'record_stop', {}, mid=3)['structuredContent']
    assert stop['ok'] and stop['job'] == job['job'] and not other.jobs[job['job']].running()


def test_a_crashed_recording_reports_its_stderr(fake):
    fake.version = '2025-06-18'
    res = call(fake, 'record_start', {'mode': 'log', 'rate': 'crash'})
    e = res['structuredContent']['error']
    assert res['isError'] and e['code'] == 'INTERNAL' and 'simulated crash' in e['hint']


def test_stop_without_a_known_recording_still_sends_a_stop_request(fake):
    fake.version = '2025-06-18'
    res = call(fake, 'record_stop', {})['structuredContent']
    assert res['ok'] and 'stop request' in res['note'] and os.path.exists(M.STOP_REQUEST)


def test_arguments_that_would_be_ignored_are_refused(fake):
    fake.version = '2025-06-18'
    for i, (tool, a) in enumerate((('record_start', {'mode': 'log', 'rate': '1M', 'decode': ['uart:rx=D0']}),
                                   ('record_start', {'mode': 'stream', 'force': True}),
                                   ('record_start', {'mode': 'stream', 'storage': 'disk'}),
                                   ('awg', {'action': 'set', 'level': '1V'}),
                                   ('awg', {'action': 'off', 'freq': '1k'}))):
        res = call(fake, tool, a, mid=i + 1)
        assert res['isError'] and res['structuredContent']['error']['code'] == 'INVALID_ARG', (tool, a)


def test_capture_time_limit_covers_the_cli_budget():
    small = M.capture_wall(M.capture_args({'channels': 'A1', 'timebase': '1ms'}))
    deep = M.capture_wall(M.capture_args({'channels': 'A1', 'rate': '400M', 'duration': '1s', 'samples': '400M'}))
    assert 150 < small < deep and deep > 120 + 400 + 30               # 400M values need the post-processing time


def test_summary_trims_long_decoder_output():
    r = M._trim_decode({'mosi_hex': 'ab' * 4096, 'error_frames': list(range(50)), 'frames': list(range(5))})
    assert len(r['mosi_hex']) < 500 and len(r['error_frames']) == M.DECODE_FRAMES and r['frames'] == list(range(5))
    doc = M.summarize({'ok': False, 'cmd': 'log', 'error': {'code': 'HOST_TOO_SLOW', 'message': 'x'},
                       'warnings': ['w'], 'analog': {'A1': {'vmin_v': 0.0}}})
    assert doc['warnings'] == ['w'] and doc['analog'] == {'A1': {'vmin_v': 0.0}} and not doc['ok']


def test_log_plan_and_awg_arguments(fake):
    call(fake, 'log_plan', {'rate': '1M', 'duration': '10min', 'channels': 'A1,D0', 'vdiv': 'A1=2V'})
    assert fake.argv()[-1] == ['log', '-c', 'A1,D0', '--vdiv', 'A1=2V', '--rate', '1M', '--duration', '10min',
                               '--plan']
    call(fake, 'awg', {'action': 'set', 'wave': 'sine', 'freq': '10k', 'low': '0V', 'high': '2V'}, mid=2)
    assert fake.argv()[-1] == ['awg', 'set', '--wave', 'sine', '--freq', '10k', '--high', '2V', '--low', '0V']
    res = call(fake, 'awg', {'action': 'dc'}, mid=3)
    assert res['isError']


# ------------------------------------------------------------------------------------------------ the real CLI
@pytest.fixture
def real(tmp_path, monkeypatch):
    monkeypatch.setattr(M, 'JOBS_DIR', str(tmp_path / 'jobs'))
    srv = M.Server(out=io.BytesIO())
    srv.version = '2025-06-18'
    return srv


def test_real_cli_offline_commands(real, tmp_path, monkeypatch):
    monkeypatch.setenv('OPENM454_NO_USB', '1')                    # whatever happens, the scope is never opened
    plan = call(real, 'log_plan', {'channels': 'A2,D0-D7', 'rate': '1M', 'duration': '10min', 'storage': 'none'})
    assert not plan['isError'] and plan['structuredContent']['plan']['verdict'] == 'sustained'
    rate = 1e6
    t = np.arange(20000) / rate
    cap = Capture({'analog_rate_hz': rate, 't0_analog_s': 0.0, 'digital_rate_hz': None, 't0_digital_s': 0.0,
                   'channels': {'A1': {'zero_code': 60.0, 'volts_per_code': 3.0 / 140, 'probe': 1.0}}},
                  analog={'A1': np.where((t * 1000) % 1 < 0.3, 200, 60).astype(np.uint8)})   # raw codes: 0 / 3 V
    path = str(tmp_path / 'sq.npz')
    cap.save(path)
    res = call(real, 'analyze', {'file': path}, mid=2)
    a1 = res['structuredContent']['analog']['A1']
    assert not res['isError'] and a1['freq_hz'] == pytest.approx(1000, rel=1e-3)
    assert a1['duty_pct'] == pytest.approx(30, abs=0.5) and a1['vpp_v'] == pytest.approx(3.0)
    # refused before the scope is opened: a level outside the screen is an error, not a silent clamp
    res = call(real, 'capture', {'channels': 'A1', 'vdiv': '1V', 'trigger': 'A1:rise:9V'}, mid=3)
    err = res['structuredContent']['error']
    assert res['isError'] and err['code'] == 'TRIGGER_OUT_OF_RANGE' and 'visible range of A1' in err['message']
    res = call(real, 'status', mid=4)                              # the guard itself: refused, not opened
    err = res['structuredContent']['error']                         # (BUSY first when DSWave runs on this PC)
    assert res['isError'] and (err['code'] == 'BUSY' or 'OPENM454_NO_USB' in err['message'])
