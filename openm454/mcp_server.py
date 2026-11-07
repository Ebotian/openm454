"""MCP server for the DSCope M454: Model Context Protocol over stdio, no extra dependencies.

A thin shell over the openm454 CLI. Every tool runs one CLI command in a child process (python -m openm454 ...), so
the scope is opened and released per call (it accepts one USB client), a hang or crash never takes the server down,
and the CLI's watchdog, safety rules and error codes apply unchanged.

Results are compact summaries (frequency, Vpp, duty, rise/fall times, trigger state, decoded frames); the samples
stay in the saved .npz file, whose path is returned. Errors carry a stable code, a message and a hint:
NOT_FOUND (scope not connected), BUSY (DSWave or another openm454 process holds it), TIMEOUT_NO_TRIGGER (nothing
triggered within timeout_s), TRIGGER_OUT_OF_RANGE, INVALID_ARG, TIMEOUT_WATCHDOG, USB_ERROR ... (README).

Run:          python -m openm454.mcp_server      (or the openm454-mcp script)
Claude Code:  claude mcp add openm454 -- python -m openm454.mcp_server
"""
import itertools
import json
import os
import subprocess
import sys
import threading
import time

from . import __version__
from .cli import STOP_REQUEST
from .paths import ROOT

PROTOCOL_VERSIONS = ('2025-06-18', '2025-03-26', '2024-11-05')
STRUCTURED_SINCE = '2025-06-18'      # structuredContent in tool results
STOP_GRACE_S = 15.0                  # after a timeout: time for a stopped stream/log to finish cleanly
JOB_START_WAIT_S = 20.0              # record_start waits this long for the first line (a log may probe ~2 s first)
DECODE_FRAMES = 20                   # frames per decoder in a summary (all of them are in files.decode)
JOBS_DIR = os.path.join(ROOT, 'state', 'mcp_jobs')

INSTRUCTIONS = """Measure and generate real signals with the DSCope M454 oscilloscope on this PC (4 analog channels
A0-A3 up to 400 MSa/s, 8 digital D0-D7, signal generator 0-3 V). Start with `status`. `capture` takes one triggered
(or untriggered) record and returns measurements per channel (frequency, Vpp, duty, rise/fall times...) plus the
saved file; `analyze` / `plot` work on saved files. Long continuous recordings: `log_plan`, then `record_start`,
`record_status`, `record_stop`. Choose the lowest rate and fewest channels that answer the question. Ask the user
before connecting probes or the generator output to their circuit. Errors have a code: NOT_FOUND = not connected,
BUSY = another program holds the scope, TIMEOUT_NO_TRIGGER = no trigger within timeout_s."""

VERTICAL = {
    'vdiv': {'type': 'string', 'description': "volts/div at the probe tip: '1V' or per channel 'A0=500mV,A2=2V'"},
    'offset': {'type': 'string', 'description': "voltage at the vertical centre: '1.5V' or 'A1=1.5V'"},
    'probe': {'type': 'string', 'description': "probe factor: '10x' or 'A2=10x' (default 1x)"},
    'coupling': {'type': 'string', 'description': "'dc' / 'ac' or per channel 'A1=ac'"},
    'bw20': {'type': 'string', 'description': 'channels with the 20 MHz bandwidth limit, e.g. A1,A2'},
    'threshold': {'type': 'string', 'description': "digital input threshold 0..5 V (default '1.4V')"},
}
DECODE = {'type': 'array', 'items': {'type': 'string'},
          'description': "protocol decoders: 'uart:rx=D4,baud=115200', 'spi:clk=D1,mosi=D3,miso=D2,cs=D0,mode=0', "
                         "'i2c:scl=D6,sda=D7', 'can:rx=D6,bitrate=500k'"}
DETAIL = {'type': 'string', 'enum': ['summary', 'full'], 'default': 'summary',
          'description': "summary (default): key measurements; full: the CLI's whole JSON document"}
MAX_WALL = {'type': 'number', 'description': 'hard limit for the whole call in seconds (default 180); the command '
                                             'is stopped and an error returned when it is exceeded'}


def _schema(props, required=()):
    return {'type': 'object', 'properties': props, 'required': list(required), 'additionalProperties': False}


TOOLS = [
    {'name': 'status', 'title': 'Scope status',
     'description': 'Is the DSCope M454 connected and usable? Model, USB speed, firmware/FPGA state and the last '
                    'signal-generator setting. NOT_FOUND = not connected, BUSY = another program holds it.',
     'inputSchema': _schema({}), 'annotations': {'readOnlyHint': True}},
    {'name': 'capture', 'title': 'Capture a record',
     'description': 'One record from the scope with measurements per channel (analog: frequency, Vpp, min/max/mean, '
                    'duty, rise/fall; digital: state, frequency, duty, edges), the trigger result and the saved '
                    'files (.npz samples, .png plot). With `trigger` it waits up to `timeout_s` (default 5 s) and '
                    'returns TIMEOUT_NO_TRIGGER if nothing triggers; without one it records at once.',
     'inputSchema': _schema(dict({
         'channels': {'type': 'string', 'default': 'A0-A3,D0-D7', 'description': "e.g. 'A1', 'A0-A3,D0-D7'"},
         'timebase': {'type': 'string', 'description': "time/div, window = 10 divisions (e.g. '200us')"},
         'duration': {'type': 'string', 'description': "window length instead of timebase (e.g. '10ms')"},
         'rate': {'type': 'string', 'description': "sample rate (e.g. '10M'); else chosen from the window"},
         'samples': {'type': 'string', 'description': "samples per channel (e.g. '1M')"},
         'trigger': {'type': 'string', 'description': "SRC:EDGE[:LEVEL], e.g. 'A1:rise:1.4V', 'D0:fall', "
                                                      "pulse: 'A1:pulse>:1.4V:200us'"},
         'trig_mode': {'type': 'string', 'enum': ['normal', 'auto', 'force'],
                       'description': 'normal (default with a trigger): wait up to timeout_s; auto: free-run after '
                                      '0.3 s without a trigger; force (default without one): no trigger'},
         'timeout_s': {'type': 'number', 'default': 5, 'description': 'how long to wait for the trigger'},
         'trig_pos': {'type': 'string', 'description': "trigger position in the window, e.g. '10%' (default 50%)"},
         'holdoff': {'type': 'string', 'description': "trigger holdoff, e.g. '1ms'"},
         'decode': DECODE,
         'edges': {'type': 'integer', 'description': 'also list the first N edge times per channel'},
         'window': {'type': 'string', 'description': "measure only t0:t1, e.g. '-1ms:1ms'"},
         'save': {'type': 'boolean', 'default': True, 'description': 'save the samples (.npz)'},
         'plot': {'type': 'boolean', 'default': True, 'description': 'save a .png plot'},
         'detail': DETAIL, 'max_wall_s': MAX_WALL}, **VERTICAL))},
    {'name': 'analyze', 'title': 'Analyze a saved capture',
     'description': 'Measurements (and optional protocol decoding) of a saved .npz capture or log file.',
     'inputSchema': _schema({'file': {'type': 'string', 'description': 'path of a saved .npz'},
                             'channels': {'type': 'string'}, 'window': {'type': 'string'},
                             'edges': {'type': 'integer'}, 'decode': DECODE, 'detail': DETAIL}, ['file']),
     'annotations': {'readOnlyHint': True}},
    {'name': 'plot', 'title': 'Plot a saved capture',
     'description': 'Render a saved .npz to a .png (optionally a time window or some channels); returns its path.',
     'inputSchema': _schema({'file': {'type': 'string'}, 'channels': {'type': 'string'}, 'window': {'type': 'string'},
                             'out': {'type': 'string', 'description': 'png path (default next to the file)'}},
                            ['file'])},
    {'name': 'awg', 'title': 'Signal generator',
     'description': 'Drive the generator output (0..3 V, up to 1 MHz). action set: wave/freq/high/low (or vpp+dc), '
                    'duty, phase; dc: a steady level; off: park at 0 V; status: the last setting. The output keeps '
                    'running after the call. Ask the user before connecting it to their circuit.',
     'inputSchema': _schema({'action': {'type': 'string', 'enum': ['set', 'dc', 'off', 'status']},
                             'wave': {'type': 'string', 'enum': ['sine', 'square', 'triangle', 'noise']},
                             'freq': {'type': 'string', 'description': "e.g. '10k'"},
                             'high': {'type': 'string'}, 'low': {'type': 'string'}, 'vpp': {'type': 'string'},
                             'dc': {'type': 'string'}, 'duty': {'type': 'string'}, 'phase': {'type': 'string'},
                             'level': {'type': 'string', 'description': "dc level, e.g. '1.2V'"}}, ['action'])},
    {'name': 'log_plan', 'title': 'Plan a gap-free log',
     'description': 'Plan a gap-free loop-mode log without touching the scope: data rate, verdict, gap-free limit, '
                    'storage, and above the readout limit the comparison with stream (STREAM_BETTER / NEEDS_FORCE '
                    'errors carry the numbers).',
     'inputSchema': _schema(dict({'channels': {'type': 'string'}, 'rate': {'type': 'string'},
                                  'duration': {'type': 'string', 'description': "e.g. '60s', '2h', 'forever'"},
                                  'every': {'type': 'string'}, 'storage': {'type': 'string',
                                                                           'enum': ['ram', 'disk', 'none']},
                                  'keep_last': {'type': 'boolean'}, 'force': {'type': 'boolean'}}, **VERTICAL),
                            ['rate']),
     'annotations': {'readOnlyHint': True}},
    {'name': 'record_start', 'title': 'Start a continuous recording',
     'description': 'Start a background recording: mode log (gap-free, one rate for all channels, kept in RAM and '
                    'saved at the end) or stream (records with short gaps, live statistics). Returns a job id; '
                    'follow it with record_status and end it with record_stop. One recording at a time; the scope '
                    'is busy meanwhile.',
     'inputSchema': _schema(dict({'mode': {'type': 'string', 'enum': ['log', 'stream']},
                                  'channels': {'type': 'string'}, 'rate': {'type': 'string'},
                                  'duration': {'type': 'string', 'description': "e.g. '60s', 'forever'"},
                                  'every': {'type': 'string', 'description': "signal time per status line, '1s'"},
                                  'storage': {'type': 'string', 'enum': ['ram', 'disk', 'none']},
                                  'keep_last': {'type': 'boolean'}, 'force': {'type': 'boolean'},
                                  'decode': DECODE}, **VERTICAL), ['mode'])},
    {'name': 'record_status', 'title': 'Recording status',
     'description': 'The latest status lines of a recording (backlog, live statistics) and its result once done.',
     'inputSchema': _schema({'job': {'type': 'string', 'description': 'job id (default: the latest)'},
                             'lines': {'type': 'integer', 'default': 3}}),
     'annotations': {'readOnlyHint': True}},
    {'name': 'record_stop', 'title': 'Stop a recording',
     'description': 'Stop a recording cleanly and return its result (files, measurements).',
     'inputSchema': _schema({'job': {'type': 'string'},
                             'wait_s': {'type': 'number', 'default': 120,
                                        'description': 'how long to wait for the final file to be written'}})},
    {'name': 'release', 'title': 'Release the scope',
     'description': 'Stop and release a scope left running or in a bad state by a crashed program.',
     'inputSchema': _schema({})},
]
DEVICE_TOOLS = {'status', 'capture', 'awg', 'record_start', 'release'}


class ToolError(Exception):
    def __init__(self, code, message, hint=None, extra=None):
        super().__init__(message)
        self.doc = {'ok': False, 'error': dict({'code': code, 'message': message}, **({'hint': hint} if hint else {}))}
        self.doc.update(extra or {})


# ------------------------------------------------------------------------------------------------ CLI runner
def _last_json(text):
    for line in reversed((text or '').splitlines()):
        line = line.strip()
        if line.startswith('{'):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def request_stop():
    os.makedirs(os.path.dirname(STOP_REQUEST), exist_ok=True)
    with open(STOP_REQUEST, 'w') as f:
        f.write('mcp\n')


class Cli:
    """Runs openm454 CLI commands in child processes."""

    def __init__(self, argv0=None):
        self.argv0 = list(argv0 or [sys.executable, '-X', 'utf8', '-m', 'openm454'])

    def _popen(self, args, stdout):
        kw = {}
        if os.name == 'nt':
            kw['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        return subprocess.Popen(self.argv0 + list(args), stdout=stdout, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, **kw)

    def run(self, args, max_wall):
        """-> the command's (last) JSON document. Never waits much longer than max_wall: past it the command is asked
        to stop (stream/log/watch end cleanly) and, if it is still running, an error says so."""
        p = self._popen(args, subprocess.PIPE)
        try:
            out, err = p.communicate(timeout=max_wall)
        except subprocess.TimeoutExpired:
            request_stop()
            try:
                out, err = p.communicate(timeout=STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                raise ToolError('TIMEOUT_WATCHDOG', 'openm454 %s did not finish within %.0f s' % (args[0], max_wall),
                                'process %d keeps the scope until its own watchdog stops it; retry later, or raise '
                                'max_wall_s for long captures' % p.pid)
        doc = _last_json(out.decode('utf-8', 'replace'))
        if doc is None:
            tail = err.decode('utf-8', 'replace').strip().splitlines()[-3:]
            raise ToolError('INTERNAL', 'openm454 %s ended (exit %s) without a result' % (args[0], p.returncode),
                            ' / '.join(tail) or None)
        return doc

    def start(self, args, out_path):
        f = open(out_path, 'wb')
        try:
            return self._popen(args, f)
        finally:
            f.close()


# ------------------------------------------------------------------------------------------------ summaries
A_KEYS = ('freq_hz', 'vpp_v', 'vmin_v', 'vmax_v', 'vmean_v', 'duty_pct', 'rise_s', 'fall_s')
D_KEYS = ('state', 'freq_hz', 'duty_pct', 'edges', 'high_frac')
ACQ_KEYS = ('triggered', 'mode', 'analog_rate_hz', 'digital_rate_hz', 'rate_hz', 'samples', 'duration_s',
            'complete', 'stop_reason', 'host_mb_s', 'backlog_max_mb', 'unread_at_stop_mb')


def _trim_decode(r):
    r = dict(r)
    frames = r.get('frames') or []
    if len(frames) > DECODE_FRAMES:
        r['frames'] = frames[:DECODE_FRAMES]
        r['frames_listed'] = DECODE_FRAMES
    for k in ('text', 'data_hex'):
        if isinstance(r.get(k), str) and len(r[k]) > 400:
            r[k] = r[k][:400] + '...'
    return r


def summarize(doc):
    """The CLI document reduced to what an agent reads: settings, trigger, key measurements, files, warnings."""
    if not doc.get('ok', False):
        return {k: doc[k] for k in ('ok', 'cmd', 'error', 'plan', 'files', 'acq') if k in doc}
    out = {'ok': True, 'cmd': doc.get('cmd')}
    applied = doc.get('applied') or {}
    if applied:
        out['applied'] = {k: v for k, v in applied.items()
                          if k in ('channels', 'vdiv_v', 'offset_v', 'probe', 'threshold_v', 'rate_hz', 'mode')}
        if applied.get('trigger'):
            out['trigger'] = {k: v for k, v in applied['trigger'].items() if v is not None}
    if doc.get('acq'):
        out['acq'] = {k: doc['acq'][k] for k in ACQ_KEYS if doc['acq'].get(k) is not None}
    for group, keys in (('analog', A_KEYS), ('digital', D_KEYS)):
        if doc.get(group):
            out[group] = {}
            for ch, m in doc[group].items():
                d = {k: m[k] for k in keys if m.get(k) is not None}
                d.update({k: True for k in ('clip_lo', 'clip_hi') if m.get(k)})
                d.update({k: m[k] for k in ('edges_rise_s', 'edges_fall_s') if m.get(k)})   # only with edges=N
                out[group][ch] = d
    if doc.get('decode'):
        out['decode'] = [_trim_decode(r) for r in doc['decode']]
    for k in ('files', 'plan', 'device', 'awg', 'awg_last', 'persists_after_exit', 'note', 'records', 'signal_s',
              'seams_s', 'stopped_by'):
        if doc.get(k) not in (None, {}, []):
            out[k] = doc[k]
    for k in ('warnings', 'hints'):
        if doc.get(k):
            out[k] = doc[k]
    if isinstance(out.get('plan'), dict):
        out['plan'] = {k: v for k, v in out['plan'].items() if not k.startswith('_')}
    return out


# ------------------------------------------------------------------------------------------------ argument mapping
def _vertical(a):
    out = []
    for k in ('vdiv', 'offset', 'probe', 'coupling', 'bw20', 'threshold'):
        if a.get(k) not in (None, ''):
            out += ['--' + k, str(a[k])]
    return out


def _opt(a, key, flag=None):
    v = a.get(key)
    return [] if v in (None, '') else [flag or '--' + key.replace('_', '-'), str(v)]


def capture_args(a):
    args = ['capture', '-c', a.get('channels') or 'A0-A3,D0-D7'] + _vertical(a)
    for k in ('timebase', 'duration', 'rate', 'samples', 'trig_pos', 'holdoff', 'window'):
        args += _opt(a, k)
    mode = a.get('trig_mode') or ('normal' if a.get('trigger') else 'force')
    if a.get('trigger'):
        args += ['--trig', a['trigger']]
    elif mode != 'force':
        raise ToolError('INVALID_ARG', 'trig_mode %s needs a trigger' % mode, "e.g. trigger 'A1:rise:1.4V'")
    timeout = float(a.get('timeout_s', 5.0))
    args += ['--trig-mode', mode, '--timeout', '%gs' % timeout]
    if a.get('edges'):
        args += ['--edges', str(int(a['edges']))]
    for spec in a.get('decode') or ():
        args += ['--decode', spec]
    if a.get('save') is False:
        args.append('--no-save')
    if a.get('plot') is False:
        args.append('--no-plot')
    return args


def analyze_args(a):
    args = ['analyze', a['file']] + _opt(a, 'window') + _opt(a, 'channels')
    if a.get('edges'):
        args += ['--edges', str(int(a['edges']))]
    for spec in a.get('decode') or ():
        args += ['--decode', spec]
    return args


def awg_args(a):
    act = a['action']
    if act in ('off', 'status'):
        return ['awg', act]
    if act == 'dc':
        if not a.get('level'):
            raise ToolError('INVALID_ARG', "awg dc needs level, e.g. '1.2V'")
        return ['awg', 'dc', '--level', str(a['level'])]
    args = ['awg', 'set']
    for k in ('wave', 'freq', 'high', 'low', 'vpp', 'dc', 'duty', 'phase'):
        args += _opt(a, k)
    return args


def record_args(a, plan=False):
    mode = a.get('mode', 'log')
    args = [mode, '-c', a.get('channels') or 'A0-A3,D0-D7'] + _vertical(a)
    for k in ('rate', 'duration', 'every'):
        args += _opt(a, k)
    if mode == 'log':
        args += _opt(a, 'storage')
        if a.get('keep_last'):
            args.append('--keep-last')
        if a.get('force'):
            args.append('--force')
        if plan:
            args.append('--plan')
    else:
        for spec in a.get('decode') or ():
            args += ['--decode', spec]
        if a.get('storage') == 'none':
            args.append('--no-save')
    return args


# ------------------------------------------------------------------------------------------------ server
class Server:
    def __init__(self, cli=None, out=None):
        self.cli = cli or Cli()
        self.out = out or sys.stdout.buffer
        self.wlock = threading.Lock()
        self.device = threading.Lock()          # one scope command at a time
        self.version = PROTOCOL_VERSIONS[0]
        self.jobs = {}                           # id -> {'proc', 'path', 'mode', 'args', 't0'}
        self.job_ids = itertools.count(1)
        self.cancelled = set()

    # ------------------------------------------------------------ JSON-RPC plumbing
    def send(self, msg):
        data = (json.dumps(msg, separators=(',', ':')) + '\n').encode('utf-8')
        with self.wlock:
            self.out.write(data)
            self.out.flush()

    def serve(self, inp=None):
        inp = inp or sys.stdin.buffer
        for raw in iter(inp.readline, b''):
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                self.send({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}})
                continue
            for m in (msg if isinstance(msg, list) else [msg]):
                self.dispatch(m)
        self.shutdown()

    def dispatch(self, m):
        method, mid = m.get('method'), m.get('id')
        if method is None:                       # a response to us (we send no requests): ignore
            return
        if mid is None:                          # notification
            if method == 'notifications/cancelled':  # the call still finishes (bounded); its reply is dropped
                self.cancelled.add((m.get('params') or {}).get('requestId'))
            return
        if method == 'initialize':
            want = (m.get('params') or {}).get('protocolVersion')
            self.version = want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return self.reply(mid, {'protocolVersion': self.version,
                                    'capabilities': {'tools': {'listChanged': False}},
                                    'serverInfo': {'name': 'openm454', 'title': 'DSCope M454 oscilloscope',
                                                   'version': __version__},
                                    'instructions': INSTRUCTIONS})
        if method == 'ping':
            return self.reply(mid, {})
        if method == 'tools/list':
            return self.reply(mid, {'tools': TOOLS})
        if method == 'tools/call':
            p = m.get('params') or {}
            threading.Thread(target=self.call, args=(mid, p.get('name'), p.get('arguments') or {}),
                             daemon=True).start()
            return
        self.send({'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': 'unknown method %s' % method}})

    def reply(self, mid, result):
        self.send({'jsonrpc': '2.0', 'id': mid, 'result': result})

    def call(self, mid, name, args):
        try:
            if name not in {t['name'] for t in TOOLS}:
                self.send({'jsonrpc': '2.0', 'id': mid,
                           'error': {'code': -32602, 'message': 'unknown tool %s' % name}})
                return
            doc = self.run_tool(name, args)
            err = not doc.get('ok', False)
        except ToolError as e:
            doc, err = e.doc, True
        except Exception as e:                   # noqa: BLE001 - reported to the client, the server keeps running
            doc, err = {'ok': False, 'error': {'code': 'INTERNAL', 'message': repr(e)}}, True
        if mid in self.cancelled:
            return
        result = {'content': [{'type': 'text', 'text': self.text(doc)}], 'isError': err}
        if self.version >= STRUCTURED_SINCE:
            result['structuredContent'] = doc
        self.reply(mid, result)

    @staticmethod
    def text(doc):
        body = json.dumps(doc, indent=1)
        if doc.get('ok', False):
            return body
        e = doc.get('error') or {}
        head = '%s: %s' % (e.get('code'), e.get('message'))
        if e.get('hint'):
            head += '\nhint: %s' % e['hint']
        return head + '\n' + body

    # ------------------------------------------------------------ tools
    def jobs_running(self):
        return [j for j, v in self.jobs.items() if v['proc'].poll() is None]

    def run_tool(self, name, a):
        if name in DEVICE_TOOLS:
            busy = self.jobs_running()
            if busy:
                raise ToolError('BUSY', 'recording %s holds the scope' % busy[0],
                                'record_stop it first (or wait until it ends)')
        wall = float(a.get('max_wall_s') or 180.0)
        if name == 'status':
            with self.device:
                doc = self.cli.run(['info'], 60.0)
            if doc.get('ok'):
                doc = {'ok': True, 'cmd': 'info', 'connected': True, 'device': doc.get('device'),
                       'awg_last': doc.get('awg_last')}
            return doc
        if name == 'capture':
            args = capture_args(a)
            with self.device:
                doc = self.cli.run(args, wall + float(a.get('timeout_s', 5.0)))
            return doc if a.get('detail') == 'full' else summarize(doc)
        if name == 'analyze':
            doc = self.cli.run(analyze_args(a), wall)
            return doc if a.get('detail') == 'full' else summarize(doc)
        if name == 'plot':
            return self.cli.run(['plot', a['file']] + _opt(a, 'window') + _opt(a, 'channels') + _opt(a, 'out'), wall)
        if name == 'awg':
            with self.device:
                return summarize(self.cli.run(awg_args(a), 60.0))
        if name == 'log_plan':
            return summarize(self.cli.run(record_args(dict(a, mode='log'), plan=True), 60.0))
        if name == 'release':
            with self.device:
                return self.cli.run(['release'], 60.0)
        if name == 'record_start':
            return self.record_start(a)
        if name == 'record_status':
            return self.record_status(a)
        if name == 'record_stop':
            return self.record_stop(a)
        raise ToolError('INVALID_ARG', 'unknown tool %s' % name)

    def record_start(self, a):
        if a.get('mode') not in ('log', 'stream'):
            raise ToolError('INVALID_ARG', "mode must be 'log' or 'stream'")
        os.makedirs(JOBS_DIR, exist_ok=True)
        job = '%s-%d' % (time.strftime('%Y%m%d-%H%M%S'), next(self.job_ids))
        path = os.path.join(JOBS_DIR, job + '.jsonl')
        args = record_args(a)
        with self.device:
            proc = self.cli.start(args, path)
        self.jobs[job] = {'proc': proc, 'path': path, 'mode': a['mode'], 'args': args, 't0': time.time()}
        t_end = time.time() + JOB_START_WAIT_S
        while time.time() < t_end:              # until the run started (or was refused)
            lines = _read_lines(path)
            if proc.poll() is not None or (lines and (lines[-1].get('started') or 'i' in lines[-1])):
                break
            time.sleep(0.1)
        lines = _read_lines(path)
        if proc.poll() is not None:
            final = lines[-1] if lines else None
            if final is None or not final.get('ok', False):
                plan = ((final or {}).get('error') or {}).get('plan')
                raise ToolError(*_err_parts(final, 'the recording ended at once'), extra={'plan': plan} if plan else None)
            return dict(summarize(final), job=job, running=False)
        out = {'ok': True, 'job': job, 'running': True, 'pid': proc.pid, 'output': path.replace('\\', '/'),
               'args': args}
        for l in lines:
            if 'probe' in l:
                out['probe'] = l['probe']
            if l.get('started'):
                out['plan'] = {k: v for k, v in (l.get('plan') or {}).items() if not k.startswith('_')}
                out['out_stem'] = l.get('out_stem')
        return out

    def _job(self, a):
        if not self.jobs:
            raise ToolError('INVALID_ARG', 'no recording was started by this server')
        job = a.get('job') or max(self.jobs, key=lambda j: self.jobs[j]['t0'])
        if job not in self.jobs:
            raise ToolError('INVALID_ARG', 'unknown job %s' % job, 'jobs: %s' % ', '.join(sorted(self.jobs)))
        return job, self.jobs[job]

    def record_status(self, a):
        job, j = self._job(a)
        lines = _read_lines(j['path'])
        running = j['proc'].poll() is None
        out = {'ok': True, 'job': job, 'running': running, 'mode': j['mode'],
               'elapsed_s': round(time.time() - j['t0'], 1)}
        stats = [l for l in lines if 'i' in l and 'ok' not in l]    # the live statistics lines
        out['last'] = stats[-max(1, int(a.get('lines', 3))):]
        if not running and lines:
            out['result'] = summarize(lines[-1])
        return out

    def record_stop(self, a):
        job, j = self._job(a)
        if j['proc'].poll() is None:
            request_stop()
            try:
                j['proc'].wait(timeout=float(a.get('wait_s', 120)))
            except subprocess.TimeoutExpired:
                raise ToolError('TIMEOUT_WATCHDOG', 'recording %s has not finished %.0f s after the stop request'
                                % (job, float(a.get('wait_s', 120))),
                                'a large RAM log takes a while to write: call record_stop again with a longer wait_s')
        lines = _read_lines(j['path'])
        final = lines[-1] if lines else None
        if final is None:
            raise ToolError('INTERNAL', 'recording %s left no result' % job)
        return dict(summarize(final), job=job, running=False)

    def shutdown(self):
        """stdin closed: a recording must not outlive the client unattended."""
        for job in self.jobs_running():
            request_stop()
            try:
                self.jobs[job]['proc'].wait(timeout=120)
            except subprocess.TimeoutExpired:
                pass


def _read_lines(path):
    out = []
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            data = f.read().decode('utf-8', 'replace')
    except OSError:
        return out
    for line in data.splitlines():
        line = line.strip()
        if line.startswith('{'):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def _err_parts(doc, fallback):
    e = (doc or {}).get('error') or {}
    return e.get('code', 'INTERNAL'), e.get('message', fallback), e.get('hint')


def main():
    Server().serve()


if __name__ == '__main__':
    main()
