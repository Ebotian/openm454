"""dscope command line. Every command prints ONE JSON document on stdout (watch: one JSON line per capture).

Exit codes: 0 ok, 2 bad argument, 3 device not found, 4 busy (DSWave / another dscope), 5 no trigger / timeout,
6 device or protocol error, 7 refused as unsafe, 1 other.
"""
import argparse
import json
import os
import sys
import threading
import time
import traceback

from .errors import DscopeError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(ROOT, 'captures')
STOP_REQUEST = os.path.join(ROOT, 'state', 'stop.request')
STREAM_MIN_RATE = 100e3          # default stream rate floor (samples/s per channel) when --samples/--rate are not given
_HEARTBEAT = [None]              # watch: called after every capture to re-arm the watchdog
_IDLE = [None]                   # called when the device is idle again (host post-processing follows)
_OUT = {'lock': threading.Lock(), 'owner': None}     # one output owner: main thread, or the watchdog once it fires


FOREVER = ('forever', 'inf', 'until-stop')


def is_forever(text):
    return bool(text) and text.strip().lower() in FOREVER


def stream_settings(args):
    """Settings for 'stream': untriggered, 10 s by default, at least STREAM_MIN_RATE unless the user chose."""
    from .scope import settings_from_args
    st = settings_from_args(args)
    if not (args.duration or args.timebase):
        st.duration = 10.0
    if not (args.samples or args.rate):
        st.samples = max(st.samples, int(st.duration * STREAM_MIN_RATE))
    st.trig_source, st.trig_mode = None, 'force'
    return st


def stop_requested():
    """True once 'dscope stop' was run (watch/stream delete stale requests when they start)."""
    return os.path.exists(STOP_REQUEST)


def _clear_stop_request():
    try:
        os.remove(STOP_REQUEST)
    except OSError:
        pass


def _throttled(fn, period=0.1):
    last = [0.0, False]

    def f():
        now = time.time()
        if now - last[0] >= period:
            last[0], last[1] = now, fn()
        return last[1]
    return f


def _emit(obj, owner=None):
    """Print one JSON document/line. After the watchdog has taken over the output, the main thread prints nothing."""
    with _OUT['lock']:
        if _OUT['owner'] is not None and _OUT['owner'] != owner:
            return
        sys.stdout.write(json.dumps(obj, ensure_ascii=True, separators=(',', ':')) + '\n')
        sys.stdout.flush()


def _idle(n_values=0):
    if _IDLE[0]:
        _IDLE[0](n_values)


def _n_values(cap):
    return sum(len(v) for v in cap.analog.values()) + sum(len(v) for v in cap.digital.values())


def _fwd(p):
    return p.replace('\\', '/') if isinstance(p, str) else p


# ------------------------------------------------------------------------------------------- arguments
def _capture_args(p):
    _vertical_args(p)
    _timing_trigger_output_args(p)


def _vertical_args(p):
    g = p.add_argument_group('channels / vertical')
    g.add_argument('-c', '--channels', default='A0-A3,D0-D7', help="e.g. 'A1', 'A0-A3,D0-D7', 'analog', 'digital'")
    g.add_argument('--vdiv', default='1V', help="volts/div at the probe tip: '1V' or 'A0=500mV,A2=2V' (hw range 1mV..5V x probe)")
    g.add_argument('--offset', default='0V', help="voltage shown at the vertical centre: '0V' or 'A0=1.5V'")
    g.add_argument('--coupling', default='dc', help="'dc' / 'ac' or per channel 'A1=ac'")
    g.add_argument('--bw20', default='', help='channels with 20 MHz bandwidth limit, e.g. A1,A2')
    g.add_argument('--invert', default='', help='channels to invert, e.g. A2')
    g.add_argument('--probe', default=None, help="probe factor: '10x' or 'A2=10x' (default 1x)")
    g.add_argument('--threshold', default='1.4V', help='digital input threshold 0..5 V (default 1.4V)')


def _timing_trigger_output_args(p):
    g = p.add_argument_group('timing')
    g.add_argument('--timebase', default=None, help='time/div; window = 10 divisions (e.g. 500us)')
    g.add_argument('--duration', default=None, help='window length (e.g. 10ms); default 10 x timebase or 5ms')
    g.add_argument('--samples', default=None, help='samples per channel (default 100k; up to 400M on one channel, fewer with more '
                   'channels: 4 Gbit memory)')
    g.add_argument('--rate', default=None, help='requested sample rate (e.g. 10M); overrides --samples')
    g = p.add_argument_group('trigger')
    g.add_argument('--trig', default=None,
                   help="SRC:EDGE:LEVEL, e.g. A1:rise:1.4V, D0:fall, A2:any:0.15V; pulse: A1:pulse>:1.4V:200us "
                        "(pulse>, pulse<, npulse>, npulse<)")
    g.add_argument('--trig-mode', default='auto', choices=['auto', 'normal', 'force'],
                   help='auto: wait --auto-after for a trigger, else capture untriggered; normal: wait --timeout; force: no trigger')
    g.add_argument('--trig-pos', default='50%', help='trigger position in the window (0..100%%, default 50%%)')
    g.add_argument('--auto-after', default='0.3s')
    g.add_argument('--timeout', default='5s', help='normal-mode trigger wait')
    g.add_argument('--holdoff', default='0s')
    g.add_argument('--sensitivity', type=int, default=5, help='trigger noise margin 0..15 (DSWave "Margin")')
    g = p.add_argument_group('output')
    g.add_argument('--measure', default='basic', choices=['basic', 'full', 'none'])
    g.add_argument('--edges', type=int, default=0, help='list the first N edge times per channel')
    g.add_argument('--dump', type=int, default=0, help='decimated min/max/mean series with N points per channel')
    g.add_argument('--window', default=None, help="restrict measurements/plot to t0:t1 (e.g. -1ms:1ms)")
    _decode_args(g)
    g.add_argument('--no-plot', action='store_true')
    g.add_argument('--no-save', action='store_true')
    g.add_argument('--out', default=DEFAULT_OUT)
    g.add_argument('--tag', default='cap')
    g.add_argument('--awg', default=None, help="also set the AWG first: 'sine,10k,0V,2V[,duty%%]'")


def _decode_args(g):
    g.add_argument('--decode', action='append', default=[], metavar='SPEC',
                   help="protocol decoder (repeatable): 'uart:rx=D4,baud=115200[,invert=1]', "
                        "'spi:clk=D1,mosi=D3,miso=D2,cs=D0,mode=0', 'i2c:scl=D6,sda=D7', 'can:rx=D6,bitrate=500k'")
    g.add_argument('--decode-limit', type=int, default=50,
                   help='frames listed per decoder in the JSON (all of them go to <capture>_decode.json)')


def build_parser():
    ap = argparse.ArgumentParser(prog='dscope', description='DSCope M454 oscilloscope / logic analyzer / AWG for agents')
    ap.add_argument('--max-wall', default=None,
                    help='watchdog: stop and release the device after this time (default: computed from the command)')
    ap.add_argument('--verbose', action='store_true', help='wire log on stderr')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('info', help='device, firmware, calibration, limits')
    c = sub.add_parser('capture', help='single triggered capture -> JSON measurements + PNG + NPZ')
    _capture_args(c)
    w = sub.add_parser('watch', help='repeated captures, one JSON line each (real-time monitoring)')
    _capture_args(w)
    w.add_argument('--every', default='0.5s')
    w.add_argument('--count', type=int, default=None)
    w.add_argument('--for', dest='duration_total', default=None, help='total watch time, e.g. 30s')
    w.add_argument('--fields', default=None, help='comma list like A1.freq_hz,A1.vpp_v,D0.state (default: key stats)')
    w.add_argument('--stop-when', default=None, help="python expression over fields, e.g. \"A1['vpp_v'] < 1\"")
    w.add_argument('--save-last', action='store_true')
    sm = sub.add_parser('stream', help='untriggered recording streamed while it is acquired, with live JSON statistics; '
                                       'longer than one record (400M samples / 512 MiB) it refreshes record after record')
    _capture_args(sm)
    sm.add_argument('--every', default='1s', help='signal time per live statistics line')
    sm.add_argument('--save-each', action='store_true', help='save every record (default: only the last one)')
    lg = sub.add_parser('log', help='gap-free endless recording (loop mode) at one rate for all channels, kept in RAM '
                                    '(default) or dumped to disk; --plan shows rate limits, duration and storage')
    _vertical_args(lg)
    lg.add_argument('--rate', default='1M', help='sample rate of every channel, 1k..100M (1-2-5 steps; default 1M); '
                                                  '200M with <= 2 analog and <= 4 digital channels, 400M with <= 1 analog '
                                                  'and <= 2 digital (interleaved)')
    lg.add_argument('--duration', default='10s', help="e.g. 60s, 2h, or 'forever' (until dscope stop)")
    lg.add_argument('--every', default='1s', help='signal time per live statistics line')
    lg.add_argument('--storage', default='ram', choices=['ram', 'disk', 'none'],
                    help='ram (default): keep the samples in RAM, write one .npz at the end; disk: dump chunk files '
                         'while running (wears an SSD at high rates: only for records needed in full); none: '
                         'statistics only')
    lg.add_argument('--keep-last', action='store_true', help='RAM log longer than the budget: keep the newest part '
                                                             '(a ring) instead of refusing ("forever" always does)')
    lg.add_argument('--chunk', default=None, help='disk storage: seconds per file (default: ~256 MB per file)')
    lg.add_argument('--ram', default=None, help='RAM budget (default: the allowance set with dscope bench --ram, else '
                                                 'min(4 GB, a quarter of the free RAM))')
    lg.add_argument('--plan', action='store_true', help='print the plan (rate verdict, max duration, storage) and exit '
                                                         'without touching the device')
    lg.add_argument('--no-save', action='store_true', help='same as --storage none')
    lg.add_argument('--force', action='store_true', help='run above the ~190 MB/s planning ceiling for longer than the '
                                                         'burst limit (stops with HOST_TOO_SLOW if the host falls behind)')
    lg.add_argument('--out', default=None, help='output folder (default: the data folder set with dscope bench '
                                                 '--dir, else captures/)')
    lg.add_argument('--tag', default='log')
    bn = sub.add_parser('bench', help="this PC's limits for recording: device status, free RAM, decode speed, disk "
                                      "write speed, USB readout; records the owner's RAM / disk allowance")
    bn.add_argument('--ram', default=None, help='RAM the owner allows for logs, e.g. 8GB (stored in state/host.json)')
    bn.add_argument('--disk', default=None, help='disk space the owner allows for disk logs, e.g. 50GB')
    bn.add_argument('--dir', default=None, help='data folder for logs (stored; default --out of log)')
    bn.add_argument('--disk-test-mb', type=int, default=256, help='size of the disk write test (written once, deleted)')
    bn.add_argument('--no-device', action='store_true', help='host measurements only (no USB readout test)')
    lg.add_argument('--awg', default=None, help="also set the AWG first: 'sine,10k,0V,2V[,duty%%]'")
    a = sub.add_parser('awg', help='signal generator on the MCX output (0..3 V)')
    asub = a.add_subparsers(dest='awg_cmd', required=True)
    s = asub.add_parser('set')
    s.add_argument('--wave', default='sine', choices=['sine', 'square', 'triangle', 'noise'])
    s.add_argument('--freq', default='1k')
    s.add_argument('--high', default=None); s.add_argument('--low', default=None)
    s.add_argument('--vpp', default=None); s.add_argument('--dc', default=None, help='centre voltage (with --vpp)')
    s.add_argument('--duty', default='50%'); s.add_argument('--phase', default='0')
    sw = asub.add_parser('sweep', help='frequency sweep start -> end in --time (repeats)')
    sw.add_argument('--start', required=True); sw.add_argument('--end', required=True)
    sw.add_argument('--time', default='100ms', help='sweep time 1 ms .. 500 s')
    sw.add_argument('--log', action='store_true', help='logarithmic sweep (default linear)')
    bu = asub.add_parser('burst', help='--cycles periods of the carrier every --period')
    bu.add_argument('--cycles', type=int, default=1); bu.add_argument('--period', default='10ms')
    mo = asub.add_parser('mod', help='AM / FM / FSK of the carrier (internal source)')
    mo.add_argument('--kind', choices=['am', 'fm', 'fsk'], default='am')
    mo.add_argument('--mod-wave', choices=['sine', 'triangle', 'square'], default='sine')
    mo.add_argument('--mod-freq', default='100')
    mo.add_argument('--depth', default='50%', help='AM depth')
    mo.add_argument('--deviation', default=None, help='FM peak deviation (Hz)')
    mo.add_argument('--hop', default=None, help='FSK hop frequency (Hz, absolute)')
    for x in (sw, bu, mo):
        x.add_argument('--wave', default='sine', choices=['sine', 'triangle', 'square'])
        x.add_argument('--high', default='3V'); x.add_argument('--low', default='0V'); x.add_argument('--duty', default='50%')
    for x in (bu, mo):
        x.add_argument('--freq', default='1k', help='carrier frequency')
    dcp = asub.add_parser('dc', help='steady DC level (0..3 V, 6.4 mV steps)')
    dcp.add_argument('--level', required=True)
    asub.add_parser('off'); asub.add_parser('status')
    pl = sub.add_parser('plot', help='render a saved capture (.npz) to PNG (no device)')
    pl.add_argument('file'); pl.add_argument('--window', default=None); pl.add_argument('--channels', default=None)
    pl.add_argument('--out', default=None)
    an = sub.add_parser('analyze', help='measure a saved capture (.npz) (no device)')
    an.add_argument('file'); an.add_argument('--window', default=None); an.add_argument('--edges', type=int, default=0)
    an.add_argument('--dump', type=int, default=0); an.add_argument('--channels', default=None)
    _decode_args(an)
    st = sub.add_parser('selftest', help='verify driver + bench fixture')
    st.add_argument('--quick', action='store_true'); st.add_argument('--no-awg', action='store_true')
    sub.add_parser('release', help='make sure the device is idle and released')
    sub.add_parser('recover', help='reset and reload the FPGA (like a power-up) when the scope hangs; a scope '
                                   'that does not answer register reads still needs a replug')
    sub.add_parser('stop', help='ask a running watch/stream/log (e.g. started in the background) to stop cleanly; '
                                'never kill those processes')
    d = sub.add_parser('debug', help='maintenance only (not for experiments)')
    dsub = d.add_subparsers(dest='debug_cmd', required=True)
    r = dsub.add_parser('regs-read'); r.add_argument('addr'); r.add_argument('len', nargs='?', default='2'); r.add_argument('ext', nargs='?', default='0')
    r = dsub.add_parser('regs-write'); r.add_argument('addr'); r.add_argument('value')
    r = dsub.add_parser('plan', help='print the register program for capture options without touching the device')
    _capture_args(r)
    return ap


# ------------------------------------------------------------------------------------------- commands
def cmd_capture(args, scope, emit=True):
    from .scope import settings_from_args
    from . import plot as plotmod
    from .units import window
    st = settings_from_args(args)
    if args.awg:
        scope.awg_from_spec(args.awg)
    cap = scope.capture(st)
    _idle(_n_values(cap))
    win = window(args.window)
    res = {'ok': True, 'cmd': 'capture', 'applied': cap.meta['applied'], 'acq': cap.meta['acq']}
    if args.measure != 'none':
        res.update(cap.summary(edges_n=args.edges, window=win))
    if args.dump:
        res['dump'] = {ch: cap.dump(ch, args.dump, win) for ch in cap.channels()}
    decoded = _decode(cap, args, win, res)
    files = {}
    if not args.no_save:
        from .capture import new_path, prune
        stem = new_path(args.out, args.tag)
        files['npz'] = _fwd(cap.save(stem + '.npz'))
        if decoded:
            files['decode'] = _fwd(_save_json(stem + '_decode.json', decoded))
        if not args.no_plot:
            files['png'] = _fwd(plotmod.plot(cap, stem + '.png', window=win, title=_title(cap)))
        prune(args.out)
    elif not args.no_plot:
        from .capture import new_path
        stem = new_path(args.out, args.tag)
        files['png'] = _fwd(plotmod.plot(cap, stem + '.png', window=win, title=_title(cap)))
    res['files'] = files
    res['hints'] = cap.meta.get('hints', [])
    res['warnings'] = cap.meta.get('warnings', [])
    if emit:
        _emit(res)
    return cap, res


def _decode(cap, args, win, res):
    """Run every --decode spec; res gets the size-limited results, the full ones are returned."""
    from .decode import decode, trim
    full = [decode(cap, spec, win, None) for spec in getattr(args, 'decode', None) or ()]
    if full:
        res['decode'] = [trim(r, args.decode_limit) for r in full]
    return full


def _save_json(path, obj):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, separators=(',', ':'))
    return path


def _title(cap):
    a = cap.meta['applied']; q = cap.meta['acq']
    tr = a.get('trigger', {})
    return 'DSCope M454  %s  %s Sa/s  %d samples  trig %s %s %s  %s' % (
        ','.join(a.get('channels', [])), '%.4g' % q['analog_rate_hz'] if q.get('analog_rate_hz') else '%.4g' % q.get('digital_rate_hz', 0),
        q.get('samples', 0), tr.get('source', '-'), tr.get('edge', ''), tr.get('level_v', ''),
        'triggered' if q.get('triggered') else 'NOT triggered')


def cmd_watch(args, scope):
    from .scope import settings_from_args
    from .units import si
    st = settings_from_args(args)
    if args.awg:
        scope.awg_from_spec(args.awg)
    every = si(args.every, 's', '--every')
    total = si(args.duration_total, 's', '--for') if args.duration_total else None
    fields = args.fields.split(',') if args.fields else None
    need = None                              # with --fields, measure only the channels the output names
    if fields:
        import re
        need = {f.split('.')[0] for f in fields}
        if args.stop_when:
            need |= set(re.findall(r'\b[AD][0-7]\b', args.stop_when))
    t0 = time.time(); i = 0; hist = {}; stopped_by = 'count'
    last = None
    while True:
        t_it = time.time()
        cap = scope.capture(st)
        s = cap.summary(channels=need)
        flat = {}
        for grp in ('analog', 'digital'):
            for ch, m in s[grp].items():
                for k, v in m.items():
                    flat['%s.%s' % (ch, k)] = v
        keys = fields or [k for k in flat if k.split('.')[1] in ('vpp_v', 'freq_hz', 'duty_pct', 'state', 'vmean_v')]
        line = {'i': i, 't_s': round(time.time() - t0, 3), 'triggered': cap.meta['acq'].get('triggered')}
        if getattr(args, 'decode', None):
            line['decode'] = [{k: r.get(k) for k in ('protocol', 'count', 'errors', 'frames') if k in r}
                              for r in _decode(cap, args, None, {})]
            for r in line['decode']:
                r['frames'] = r['frames'][:args.decode_limit]
        for k in keys:
            line[k] = flat.get(k)
            if isinstance(flat.get(k), (int, float)) and not isinstance(flat.get(k), bool):
                hist.setdefault(k, []).append(flat[k])
        _emit(line)
        if _HEARTBEAT[0]:
            _HEARTBEAT[0]()
        last = cap; i += 1
        if args.stop_when:
            env = {ch: m for grp in ('analog', 'digital') for ch, m in s[grp].items()}
            try:
                if eval(args.stop_when, {'__builtins__': {}}, env):
                    stopped_by = 'stop_when'; break
            except Exception:
                pass
        if stop_requested():
            stopped_by = 'stop_request'; _clear_stop_request(); break
        if args.count is not None and i >= args.count:
            stopped_by = 'count'; break
        if total is not None and time.time() - t0 >= total:
            stopped_by = 'duration'; break
        if args.count is None and total is None and i >= 20:
            stopped_by = 'default_count_20'; break
        dt = every - (time.time() - t_it)
        if dt > 0:
            time.sleep(dt)
    import statistics as stx
    summ = {k: {'min': min(v), 'max': max(v), 'mean': stx.fmean(v), 'std': stx.pstdev(v) if len(v) > 1 else 0.0}
            for k, v in hist.items()}
    out = {'summary': True, 'n': i, 'stopped_by': stopped_by, 'elapsed_s': round(time.time() - t0, 2), 'fields': summ}
    if args.save_last and last is not None:
        from .capture import new_path
        stem = new_path(args.out, 'watch_last')
        out['files'] = {'npz': _fwd(last.save(stem + '.npz'))}
    _emit(out)


def stream_plan(args):
    """-> (settings of one record, total seconds or None for 'forever', seconds per record, chained).
    One record holds at most 400M samples per channel and 512 MiB of sample memory; a longer stream (or
    'forever') is cut into records of the same rate, each refreshing the previous one (~0.2 s between them)."""
    import copy
    from .scope import Scope
    a = copy.copy(args)
    forever = is_forever(args.duration)
    if forever:
        a.duration = None
    st = stream_settings(a)
    total = None if forever else st.duration
    first_error = None
    if total is not None:
        try:
            pl = Scope().plan(st)
            if not any('samples capped' in w for w in pl['warnings']):
                return st, total, total, False
        except DscopeError as e:            # e.g. a long window squeezed into one record needs small packets
            if e.code != 'INVALID_ARG':
                raise
            first_error = e
    rate = st.rate or (st.samples / total if total else STREAM_MIN_RATE)   # --samples keeps its density

    def record(d):
        r = copy.copy(st)
        r.duration, r.rate = d, rate
        return r
    d = MAX_RECORD_SAMPLES / rate
    for _ in range(60):
        try:
            pl = Scope().plan(record(d))
            if not any('samples capped' in w for w in pl['warnings']):
                break
        except DscopeError as e:
            if e.code != 'INVALID_ARG':
                raise
        d *= 0.9
    else:
        raise first_error or DscopeError('INVALID_ARG', 'no record length fits these stream settings')
    if total is not None and total <= d:
        return record(total), total, total, False
    return record(d), total, d, True


MAX_RECORD_SAMPLES = 400_000_000


def cmd_stream(args, scope):
    from .units import si
    st, total, rec_s, chained = stream_plan(args)
    if args.awg:
        scope.awg_from_spec(args.awg)
    every = si(args.every, 's', '--every')
    if not chained:
        try:
            cap = scope.stream(st, every=every, emit=_emit, stop_flag=_throttled(stop_requested))
        except DscopeError as e:
            if e.code != 'STOPPED':
                raise
            _clear_stop_request()
            _emit({'ok': True, 'cmd': 'stream', 'stopped_by': 'stop_request', 'samples': 0, 'note': e.message})
            return
        _idle(_n_values(cap))
        if stop_requested():
            _clear_stop_request()
        res = {'ok': True, 'cmd': 'stream', 'applied': cap.meta['applied'], 'acq': cap.meta['acq']}
        res.update(cap.summary())
        _stream_files(args, cap, res)
        _emit(res)
        return
    import copy
    k, done, seams, prev_end, last, saved, stopped_by = 0, 0.0, [], None, None, [], None
    prev = None                                    # the previous record, not decoded (kept raw until replaced)
    while total is None or done < total - 1e-9:
        st_k = copy.copy(st)
        st_k.duration = rec_s if total is None else min(rec_s, total - done)
        is_last = total is not None and total - done <= rec_s * (1 + 1e-9)

        def emit_k(line, k=k, off=done):
            line = dict(line, record=k, t_signal_s=round(off + line['t_signal_s'], 4))
            if line.pop('final', False):
                line['record_end'] = True
            _emit(line)
        try:
            cap = scope.stream(st_k, every=every, emit=emit_k, stop_flag=_throttled(stop_requested),
                               build=lambda last_one=is_last: last_one or args.save_each or stop_requested())
        except DscopeError as e:
            if e.code != 'STOPPED':
                raise
            stopped_by = 'stop_request'
            if prev is not None:                   # stopped before this record had data: keep the previous one
                last = scope.build_capture(prev)
            break
        acq = cap.meta['acq']
        if prev_end is not None and acq.get('t_start_wall'):
            seams.append(round(acq['t_start_wall'] - prev_end, 3))
        prev_end = acq['t_start_wall'] + acq['duration_s'] if acq.get('t_start_wall') else None
        done += acq['duration_s']
        prev = cap
        if cap.meta.get('kept', True):
            last = cap
            if args.save_each and not is_last:
                res_k = {}
                _stream_files(args, cap, res_k, tag='stream%03d' % k, plot=False)
                saved.append(res_k['files'])
        k += 1
        if _HEARTBEAT[0]:
            _HEARTBEAT[0]()
        if stop_requested() or not acq.get('complete', True):
            stopped_by = 'stop_request' if stop_requested() else 'incomplete record'
            last = scope.build_capture(cap)
            break
    if stop_requested():
        _clear_stop_request()
    res = {'ok': True, 'cmd': 'stream', 'records': k, 'record_s': round(rec_s, 6), 'signal_s': round(done, 6),
           'seams_s': seams[:100], 'seam_max_s': max(seams) if seams else None, 'stopped_by': stopped_by,
           'note': 'each record refreshes the previous one: only the last record is kept (--save-each keeps all; '
                   '"log" records without gaps)'}
    if last is not None:
        _idle(_n_values(last))
        res.update({'applied': last.meta['applied'], 'acq': last.meta['acq']})
        res.update(last.summary())
        _stream_files(args, last, res)
        if saved:
            res['files']['records'] = saved
    _emit(res)


def _stream_files(args, cap, res, tag=None, plot=True):
    from . import plot as plotmod
    decoded = _decode(cap, args, None, res)
    files = {}
    if not args.no_save:
        from .capture import new_path
        stem = new_path(args.out, tag or (args.tag if args.tag != 'cap' else 'stream'))
        files['npz'] = _fwd(cap.save(stem + '.npz'))
        if decoded:
            files['decode'] = _fwd(_save_json(stem + '_decode.json', decoded))
        if plot and not args.no_plot:
            files['png'] = _fwd(plotmod.plot(cap, stem + '.png', title=_title(cap)))
    res['files'] = files
    res['hints'] = cap.meta.get('hints', [])
    res['warnings'] = cap.meta.get('warnings', [])


def _log_storage(args):
    return 'none' if args.no_save else args.storage


def _log_out(args):
    from . import host
    return args.out or host.data_dir(DEFAULT_OUT)


def log_plan(args, zero_info=None):
    """-> (settings, plan) of a 'log' command (offline)."""
    from .scope import vertical_from_args
    from .units import si
    from . import logger
    st = vertical_from_args(args)
    duration = None if is_forever(args.duration) else si(args.duration, 's', '--duration')
    storage = _log_storage(args)
    pl = logger.plan(st, si(args.rate, 'Hz', '--rate'), duration, si(args.every, 's', '--every'),
                     None if storage == 'none' else _log_out(args), storage,
                     int(si(args.ram, 'B', '--ram')) if args.ram else None,
                     si(args.chunk, 's', '--chunk') if args.chunk else None, zero_info=zero_info, force=args.force,
                     keep_last=args.keep_last)
    return st, pl


def cmd_log(args, scope):
    from . import logger
    from .capture import new_path
    st, pl = log_plan(args, scope.dev.zero_info)
    scope._check_link()
    if args.awg:
        scope.awg_from_spec(args.awg)
    stem = None if pl['storage'] == 'none' else new_path(_log_out(args), args.tag)
    _emit({'ok': True, 'cmd': 'log', 'started': True, 'plan': logger.public(pl), 'out_stem': _fwd(stem)})
    try:
        res = logger.run(scope, st, pl, stem, emit=_emit, stop_flag=_throttled(stop_requested),
                         heartbeat=_HEARTBEAT[0], on_final=_idle)
    except (DscopeError, KeyboardInterrupt) as e:    # the data kept so far was written: say where
        files = {k: _fwd(v) for k, v in (getattr(e, 'log_files', None) or {}).items()}
        if isinstance(e, KeyboardInterrupt):
            _emit({'ok': False, 'cmd': 'log', 'error': {'code': 'INTERRUPTED', 'message': 'interrupted (the scope was '
                                                        'stopped first)'}, 'files': files})
            return 1
        _emit({'ok': False, 'cmd': 'log', 'error': e.to_dict(), 'files': files})
        return e.exit_code
    if stop_requested():
        _clear_stop_request()
    for k in ('npz', 'dir', 'index'):
        if k in res.get('files', {}):
            res['files'][k] = _fwd(res['files'][k])
    _emit(res)
    if not res['ok']:
        from .errors import EXIT_CODES
        return EXIT_CODES.get(res['error']['code'], 1)
    return 0


BENCH_READOUT = (('2 analog + 8 digital', ['A0', 'A1'], 100e6), ('4 analog + 8 digital', ['A0', 'A1', 'A2', 'A3'], 50e6))


def cmd_bench(args, scope=None):
    """Record the owner's allowances, measure this PC (and the USB readout when the scope is open), recommend."""
    from . import host, logger
    from .scope import Settings
    from .units import si
    h = host.load()
    allow = dict(h.get('allow', {}))
    if args.ram:
        allow['ram_bytes'] = int(si(args.ram, 'B', '--ram'))
    if args.disk:
        allow['disk_bytes'] = int(si(args.disk, 'B', '--disk'))
    if args.dir:
        allow['data_dir'] = os.path.abspath(args.dir)
    d = allow.get('data_dir') or DEFAULT_OUT
    b = {'time': time.strftime('%Y-%m-%dT%H:%M:%S'), 'ram_free_bytes': logger.available_ram(),
         'decode_mb_s': round(host.bench_decode(), 1)}
    w, free = host.bench_disk(d, args.disk_test_mb)
    b.update(disk_dir=_fwd(os.path.abspath(d)), disk_write_mb_s=round(w, 1), disk_free_bytes=free)
    res = {'ok': True, 'cmd': 'bench'}
    if scope is not None:
        res['device'] = scope.info()['device']
        readout = {}
        for name, analog, rate in BENCH_READOUT:     # a 2 s burst above the sustained rate, nothing kept
            st = Settings()
            st.analog, st.digital = list(analog), ['D%d' % i for i in range(8)]
            r = scope.record(st, rate, 2.0, every=1.0, storage='none')
            readout[name] = {'data_mb_s': r['plan']['data_mb_s'], 'readout_mb_s': r['acq']['host_mb_s'],
                             'backlog_max_mb': r['acq']['backlog_max_mb']}
        b['usb_readout'] = readout
        b['usb_readout_mb_s'] = min(v['readout_mb_s'] for v in readout.values() if v['readout_mb_s'])
    else:                                          # --no-device keeps the last USB measurement
        for k in ('usb_readout', 'usb_readout_mb_s'):
            if k in h.get('bench', {}):
                b[k] = h['bench'][k]
    h['allow'], h['bench'] = allow, b
    host.save(h)
    ram = logger.default_ram_budget()
    live = min(b.get('usb_readout_mb_s') or logger.CEILING_BPS / 1e6, 0.8 * b['decode_mb_s'])
    res.update(allow={'ram_gb': round(allow['ram_bytes'] / 1e9, 2) if allow.get('ram_bytes') else None,
                      'disk_gb': round(allow['disk_bytes'] / 1e9, 1) if allow.get('disk_bytes') else None,
                      'data_dir': _fwd(allow.get('data_dir'))},
               bench=b, recommend={
                   'ram_budget_gb': round(ram / 1e9, 2),
                   'log_ram_max_mb_s': round(min(live, logger.CEILING_BPS / 1e6), 1),
                   'log_disk_max_mb_s': round(min(live, logger.CEILING_BPS / 1e6, 0.7 * w), 1),
                   'ram_window_s': {'%g MB/s' % x: round(ram / (x * 1e6), 1) for x in (0.1, 1, 10, 50, 150)},
                   'note': 'short logs: --storage ram (default); disk only for records needed in full'},
               file=_fwd(host.HOST_FILE))
    if not allow.get('ram_bytes'):
        res['hint'] = 'ask the owner how much RAM / disk the logs may use and record it: dscope bench --ram 8GB --disk 50GB'
    _emit(res)


def cmd_awg(args, scope):
    from .units import si
    if args.awg_cmd == 'off':
        _emit({'ok': True, 'cmd': 'awg', 'awg': scope.awg_off()}); return
    if args.awg_cmd in ('sweep', 'burst', 'mod'):
        lv = dict(high=si(args.high, 'V', '--high'), low=si(args.low, 'V', '--low'), duty=si(args.duty, None, '--duty'))
        if args.awg_cmd == 'sweep':
            st = scope.awg_sweep(si(args.start, 'Hz', '--start'), si(args.end, 'Hz', '--end'), si(args.time, 's', '--time'),
                                 'log' if args.log else 'linear', args.wave, **lv)
        elif args.awg_cmd == 'burst':
            st = scope.awg_burst(args.wave, si(args.freq, 'Hz', '--freq'), lv['high'], lv['low'], args.cycles,
                                 si(args.period, 's', '--period'), lv['duty'])
        else:
            st = scope.awg_modulate(args.kind, args.wave, si(args.freq, 'Hz', '--freq'), lv['high'], lv['low'],
                                    args.mod_wave, si(args.mod_freq, 'Hz', '--mod-freq'), si(args.depth, None, '--depth'),
                                    si(args.deviation, 'Hz', '--deviation') if args.deviation else None,
                                    si(args.hop, 'Hz', '--hop') if args.hop else None, lv['duty'])
        _emit({'ok': True, 'cmd': 'awg', 'awg': st, 'persists_after_exit': True})
        return
    if args.awg_cmd == 'dc':
        _emit({'ok': True, 'cmd': 'awg', 'awg': scope.awg_dc(si(args.level, 'V', '--level')), 'persists_after_exit': True})
        return
    if args.awg_cmd == 'status':
        _emit({'ok': True, 'cmd': 'awg', 'awg': scope.awg_status(), 'note': 'last state set by dscope (device is write-only)'}); return
    if args.vpp is not None:
        vpp = si(args.vpp, 'V', '--vpp'); dc = si(args.dc, 'V', '--dc') if args.dc else vpp / 2
        high, low = dc + vpp / 2, dc - vpp / 2
    else:
        high = si(args.high, 'V', '--high') if args.high else 3.0
        low = si(args.low, 'V', '--low') if args.low else 0.0
    st = scope.awg_set(args.wave, si(args.freq, 'Hz', '--freq'), high, low, si(args.duty, None, '--duty'), si(args.phase, None, '--phase'))
    _emit({'ok': True, 'cmd': 'awg', 'awg': st, 'persists_after_exit': True})


def cmd_offline(args):
    from .capture import load_capture
    from .units import window, channel_list
    from . import plot as plotmod
    cap = load_capture(args.file)
    win = window(args.window)
    chans = None
    if getattr(args, 'channels', None):
        a, d = channel_list(args.channels); chans = a + d
    if args.cmd == 'plot':
        out = args.out or os.path.splitext(args.file)[0] + ('_zoom.png' if win else '.png')
        _emit({'ok': True, 'cmd': 'plot', 'files': {'png': _fwd(plotmod.plot(cap, out, window=win, channels=chans, title=os.path.basename(args.file)))}})
    else:
        res = {'ok': True, 'cmd': 'analyze', 'applied': cap.meta.get('applied'), 'acq': cap.meta.get('acq')}
        s = cap.summary(edges_n=args.edges, window=win)
        if chans:
            s = {g: {c: m for c, m in s[g].items() if c in chans} for g in s}
        res.update(s)
        if args.dump:
            res['dump'] = {ch: cap.dump(ch, args.dump, win) for ch in (chans or cap.channels())}
        _decode(cap, args, win, res)
        _emit(res)


def _budget(args):
    """-> (seconds, per_iteration or None). The watchdog only has to catch hangs, so this is a generous upper
    bound; for watch it bounds each capture (re-armed after every capture) instead of the whole loop.
    Compiling the settings offline here also rejects out-of-range settings before the device is opened."""
    from .scope import Scope, settings_from_args
    from .units import si
    if args.cmd == 'selftest':
        return 1200.0, None
    if args.cmd == 'bench':
        return 180.0, None
    if args.cmd == 'log':
        st, pl = log_plan(args)                        # rejects what cannot work before the device is opened
        per = max(60.0, 3 * pl['every_s'] + 60.0)        # re-armed after every statistics line
        return 90.0 + per, per
    if args.cmd not in ('capture', 'watch', 'stream'):
        return 60.0, None
    chained = False
    if args.cmd == 'stream':
        st, total, rec_s, chained = stream_plan(args)
    else:
        st = settings_from_args(args)
    from .decode import check
    check(args.decode, list(st.analog) + list(st.digital))
    geom = Scope().plan(st)['geometry']
    rec = geom['record_s']
    collect = max(10.0, 1.5 * rec + 2.0)
    if args.cmd == 'stream':
        per = 5.0 + 2.5 * rec + 5.0 + 60.0 + 2e-8 * geom.get('acqu_bytes', 0)
        return (per, per) if chained else (per, None)       # a refreshing stream re-arms after every record
    wait = (st.timeout if st.trig_mode == 'normal' else (st.auto_after if st.trig_mode == 'auto' else 2.0)) + max(st.holdoff, 0.0)
    per = wait + rec + 0.5 + collect + 5.0
    if st.trig_mode == 'auto' and st.trig_source:      # forced fallback run: validate it too
        import copy
        fb = copy.copy(st)
        fb.trig_mode = 'force'
        rec2 = Scope().plan(fb)['geometry']['record_s']
        per += 2.5 + rec2 + max(10.0, 1.5 * rec2 + 2.0)
    per += 2e-7 * geom.get('acqu_bytes', 0)            # decoding and the hint checks of deep records
    if args.cmd == 'capture':
        return max(60.0, per + 30.0), None
    per_iter = per + si(args.every, 's', '--every') + 30.0
    return per_iter, per_iter


def _watchdog_fire(cmd, max_wall, scope, done, wait=5.0):
    """Take over the output, report, stop and exit. Nothing here may block for long: a reader that stopped draining
    stdout leaves the main thread stuck inside _emit (holding the output lock) and a pipe that accepts no more
    writes, and the device must be released anyway (found by the hot-unplug test, 2026-10-31)."""
    if _OUT['lock'].acquire(timeout=wait):
        try:
            if done.is_set():
                return
            _OUT['owner'] = 'watchdog'
        finally:
            _OUT['lock'].release()
    else:
        _OUT['owner'] = 'watchdog'
    msg = {'ok': False, 'cmd': cmd, 'error': {
        'code': 'TIMEOUT_WATCHDOG', 'message': 'command exceeded its time budget (%.0f s, --max-wall); capture '
                                               'stopped and device released' % max_wall}}
    out = threading.Thread(target=_emit, args=(msg,), kwargs={'owner': 'watchdog'}, daemon=True)
    out.start()
    out.join(wait)
    helper = threading.Thread(target=scope.emergency_stop, daemon=True)
    helper.start()
    helper.join(10.0)            # never wait for a hung close: the OS releases the handle at exit
    os._exit(6)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.cmd in ('plot', 'analyze'):
        try:
            cmd_offline(args)
            return 0
        except DscopeError as e:
            _emit({'ok': False, 'cmd': args.cmd, 'error': e.to_dict()}); return e.exit_code
        except Exception as e:
            _emit({'ok': False, 'cmd': args.cmd, 'error': {'code': 'INTERNAL', 'message': repr(e)}}); return 1
    if args.cmd == 'stop':
        os.makedirs(os.path.dirname(STOP_REQUEST), exist_ok=True)
        with open(STOP_REQUEST, 'w') as f:
            f.write(str(time.time()))
        _emit({'ok': True, 'cmd': 'stop', 'note': 'a running watch/stream/log stops after its current capture or block'})
        return 0
    from .units import si
    from .scope import Scope
    try:                                     # everything here is offline: nothing has been sent to the scope yet
        if args.cmd == 'bench' and args.no_device:
            cmd_bench(args, None)
            return 0
        if args.cmd == 'log' and args.plan:
            from . import logger
            st, pl = log_plan(args)
            _emit({'ok': True, 'cmd': 'log', 'plan': logger.public(pl)})
            return 0
        if args.cmd == 'debug' and args.debug_cmd == 'plan':
            from .scope import settings_from_args
            _emit({'ok': True, 'cmd': 'plan', **Scope().plan(settings_from_args(args))})
            return 0
        max_wall, per_iter = _budget(args)
        if args.max_wall:                    # an explicit value extends the computed budget, never shortens it
            given = si(args.max_wall, 's', '--max-wall')
            max_wall = max(max_wall, given)
            per_iter = max(per_iter, given) if per_iter else None
    except DscopeError as e:
        _emit({'ok': False, 'cmd': args.cmd, 'error': e.to_dict()})
        return e.exit_code
    except Exception as e:
        _emit({'ok': False, 'cmd': args.cmd, 'error': {'code': 'INTERNAL', 'message': repr(e),
                                                         'trace': traceback.format_exc().splitlines()[-6:]}})
        return 1
    if args.cmd in ('watch', 'stream', 'log'):
        _clear_stop_request()                # a request left from an earlier run must not stop this one
    scope = Scope(log=(lambda s: sys.stderr.write(s + '\n')) if args.verbose else None)
    done = threading.Event()
    deadline = [time.time() + max_wall]
    if per_iter:
        _HEARTBEAT[0] = lambda: deadline.__setitem__(0, time.time() + per_iter)
    _IDLE[0] = lambda n: deadline.__setitem__(0, max(deadline[0], time.time() + 120.0 + 1e-6 * n))

    def watchdog():
        while True:
            left = deadline[0] - time.time()
            if left <= 0:
                break
            if done.wait(min(left, 1.0)):
                return
        _watchdog_fire(args.cmd, max_wall, scope, done)
    threading.Thread(target=watchdog, daemon=True).start()

    code = 0
    stale = False
    try:
        scope.open(reload_fpga=args.cmd == 'recover')
        try:
            if args.cmd == 'info':
                _emit({'ok': True, 'cmd': 'info', **scope.info()})
            elif args.cmd == 'capture':
                cmd_capture(args, scope)
            elif args.cmd == 'watch':
                cmd_watch(args, scope)
            elif args.cmd == 'stream':
                cmd_stream(args, scope)
            elif args.cmd == 'log':
                code = cmd_log(args, scope)
            elif args.cmd == 'bench':
                cmd_bench(args, scope)
            elif args.cmd == 'awg':
                cmd_awg(args, scope)
            elif args.cmd == 'release':
                _emit({'ok': True, 'cmd': 'release', 'note': 'device opened, any left-over run stopped, idled and released'})
            elif args.cmd == 'recover':
                _emit({'ok': True, 'cmd': 'recover', 'device': scope.info()['device'],
                       'note': 'FPGA reset and reloaded; the signal generator is not reset by this (use awg off)'})
            elif args.cmd == 'selftest':
                from .selftest import run as selftest
                res = selftest(scope, quick=args.quick, awg=not args.no_awg)
                _emit(res)
                code = 0 if res.get('ok') else 1
            elif args.cmd == 'debug':
                _emit({'ok': True, 'cmd': 'debug', **scope.debug(args)})
        finally:
            dev = scope.dev
            stale = dev is not None and dev.t.stale
            scope.close()
    except DscopeError as e:
        _emit({'ok': False, 'cmd': args.cmd, 'error': e.to_dict()})
        code = e.exit_code
    except KeyboardInterrupt:
        _emit({'ok': False, 'cmd': args.cmd, 'error': {'code': 'INTERRUPTED', 'message': 'interrupted (any capture '
                                                                                         'was stopped first)'}})
        code = 1
    except Exception as e:
        _emit({'ok': False, 'cmd': args.cmd, 'error': {'code': 'INTERNAL', 'message': repr(e),
                                                         'trace': traceback.format_exc().splitlines()[-6:]}})
        code = 1
    finally:
        with _OUT['lock']:
            done.set()
            watchdog_owns = _OUT['owner'] == 'watchdog'
        if stale and not watchdog_owns:
            sys.stdout.flush()
            os._exit(code)           # libusb finalizers would wait for the stale transfers forever
    return code


if __name__ == '__main__':
    sys.exit(main())
