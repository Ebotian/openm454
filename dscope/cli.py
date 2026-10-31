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


def _emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=True, separators=(',', ':')) + '\n')
    sys.stdout.flush()


def _fwd(p):
    return p.replace('\\', '/') if isinstance(p, str) else p


# ------------------------------------------------------------------------------------------- arguments
def _capture_args(p):
    g = p.add_argument_group('channels / vertical')
    g.add_argument('-c', '--channels', default='A0-A3,D0-D7', help="e.g. 'A1', 'A0-A3,D0-D7', 'analog', 'digital'")
    g.add_argument('--vdiv', default='1V', help="volts/div at the probe tip: '1V' or 'A0=500mV,A2=2V' (hw range 1mV..5V x probe)")
    g.add_argument('--offset', default='0V', help="voltage shown at the vertical centre: '0V' or 'A0=1.5V'")
    g.add_argument('--coupling', default='dc', help="'dc' / 'ac' or per channel 'A1=ac'")
    g.add_argument('--bw20', default='', help='channels with 20 MHz bandwidth limit, e.g. A1,A2')
    g.add_argument('--invert', default='', help='channels to invert, e.g. A2')
    g.add_argument('--probe', default=None, help="probe factor: '10x' or 'A2=10x' (default 1x)")
    g.add_argument('--threshold', default='1.4V', help='digital input threshold 0..5 V (default 1.4V)')
    g = p.add_argument_group('timing')
    g.add_argument('--timebase', default=None, help='time/div; window = 10 divisions (e.g. 500us)')
    g.add_argument('--duration', default=None, help='window length (e.g. 10ms); default 10 x timebase or 5ms')
    g.add_argument('--samples', default=None, help='samples per channel (default 100k; max ~50M)')
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
    g.add_argument('--no-plot', action='store_true')
    g.add_argument('--no-save', action='store_true')
    g.add_argument('--out', default=DEFAULT_OUT)
    g.add_argument('--tag', default='cap')
    g.add_argument('--awg', default=None, help="also set the AWG first: 'sine,10k,0V,2V[,duty%%]'")


def build_parser():
    ap = argparse.ArgumentParser(prog='dscope', description='DSCope M454 oscilloscope / logic analyzer / AWG for agents')
    ap.add_argument('--max-wall', default='60s', help='watchdog: abort and release the device after this time')
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
    sm = sub.add_parser('stream', help='long untriggered recording with live JSON statistics (data logger)')
    _capture_args(sm)
    sm.add_argument('--every', default='1s', help='signal time per live statistics line')
    a = sub.add_parser('awg', help='signal generator on the MCX output (0..3 V)')
    asub = a.add_subparsers(dest='awg_cmd', required=True)
    s = asub.add_parser('set')
    s.add_argument('--wave', default='sine', choices=['sine', 'square', 'triangle', 'noise'])
    s.add_argument('--freq', default='1k')
    s.add_argument('--high', default=None); s.add_argument('--low', default=None)
    s.add_argument('--vpp', default=None); s.add_argument('--dc', default=None, help='centre voltage (with --vpp)')
    s.add_argument('--duty', default='50%'); s.add_argument('--phase', default='0')
    asub.add_parser('off'); asub.add_parser('status')
    pl = sub.add_parser('plot', help='render a saved capture (.npz) to PNG (no device)')
    pl.add_argument('file'); pl.add_argument('--window', default=None); pl.add_argument('--channels', default=None)
    pl.add_argument('--out', default=None)
    an = sub.add_parser('analyze', help='measure a saved capture (.npz) (no device)')
    an.add_argument('file'); an.add_argument('--window', default=None); an.add_argument('--edges', type=int, default=0)
    an.add_argument('--dump', type=int, default=0); an.add_argument('--channels', default=None)
    st = sub.add_parser('selftest', help='verify driver + bench fixture')
    st.add_argument('--quick', action='store_true'); st.add_argument('--no-awg', action='store_true')
    sub.add_parser('release', help='make sure the device is idle and released')
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
    win = window(args.window)
    res = {'ok': True, 'cmd': 'capture', 'applied': cap.meta['applied'], 'acq': cap.meta['acq']}
    if args.measure != 'none':
        res.update(cap.summary(edges_n=args.edges, window=win))
    if args.dump:
        res['dump'] = {ch: cap.dump(ch, args.dump, win) for ch in cap.channels()}
    files = {}
    if not args.no_save:
        from .capture import new_path, prune
        stem = new_path(args.out, args.tag)
        files['npz'] = _fwd(cap.save(stem + '.npz'))
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
    t0 = time.time(); i = 0; hist = {}; stopped_by = 'count'
    last = None
    while True:
        t_it = time.time()
        cap = scope.capture(st)
        s = cap.summary()
        flat = {}
        for grp in ('analog', 'digital'):
            for ch, m in s[grp].items():
                for k, v in m.items():
                    flat['%s.%s' % (ch, k)] = v
        keys = fields or [k for k in flat if k.split('.')[1] in ('vpp_v', 'freq_hz', 'duty_pct', 'state', 'vmean_v')]
        line = {'i': i, 't_s': round(time.time() - t0, 3), 'triggered': cap.meta['acq'].get('triggered')}
        for k in keys:
            line[k] = flat.get(k)
            if isinstance(flat.get(k), (int, float)) and not isinstance(flat.get(k), bool):
                hist.setdefault(k, []).append(flat[k])
        _emit(line)
        last = cap; i += 1
        if args.stop_when:
            env = {ch: m for grp in ('analog', 'digital') for ch, m in s[grp].items()}
            try:
                if eval(args.stop_when, {'__builtins__': {}}, env):
                    stopped_by = 'stop_when'; break
            except Exception:
                pass
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


def cmd_stream(args, scope):
    from .scope import settings_from_args
    from .units import si
    from . import plot as plotmod
    st = settings_from_args(args)
    if not (args.duration or args.timebase):
        st.duration = 10.0
    if args.awg:
        scope.awg_from_spec(args.awg)
    cap = scope.stream(st, every=si(args.every, 's', '--every'), emit=_emit)
    res = {'ok': True, 'cmd': 'stream', 'applied': cap.meta['applied'], 'acq': cap.meta['acq']}
    res.update(cap.summary())
    files = {}
    if not args.no_save:
        from .capture import new_path
        stem = new_path(args.out, args.tag if args.tag != 'cap' else 'stream')
        files['npz'] = _fwd(cap.save(stem + '.npz'))
        if not args.no_plot:
            files['png'] = _fwd(plotmod.plot(cap, stem + '.png', title=_title(cap)))
    res['files'] = files
    res['hints'] = cap.meta.get('hints', [])
    _emit(res)


def cmd_awg(args, scope):
    from .units import si
    if args.awg_cmd == 'off':
        _emit({'ok': True, 'cmd': 'awg', 'awg': scope.awg_off()}); return
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
        _emit(res)


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
    from .units import si
    from .scope import Scope
    max_wall = si(args.max_wall, 's', '--max-wall')
    if args.cmd == 'watch' and args.duration_total:
        max_wall = max(max_wall, si(args.duration_total, 's', '--for') + 30)
    if args.cmd in ('capture', 'watch', 'stream'):
        dur = si(args.duration, 's', '--duration') if args.duration else (
            10 * si(args.timebase, 's', '--timebase') if args.timebase else 0)
        if args.cmd == 'stream' and not dur:
            dur = 10.0
        max_wall = max(max_wall, 2 * dur + 30)
    scope = Scope(log=(lambda s: sys.stderr.write(s + '\n')) if args.verbose else None)
    done = threading.Event()

    def watchdog():
        if not done.wait(max_wall):
            sys.stdout.write(json.dumps({'ok': False, 'cmd': args.cmd, 'error': {
                'code': 'TIMEOUT_WATCHDOG', 'message': 'command exceeded --max-wall %.0fs; device released' % max_wall}}) + '\n')
            sys.stdout.flush()
            try:
                scope.emergency_stop()
            finally:
                os._exit(6)
    threading.Thread(target=watchdog, daemon=True).start()
    try:
        if args.cmd == 'debug' and args.debug_cmd == 'plan':
            from .scope import settings_from_args
            _emit({'ok': True, 'cmd': 'plan', **scope.plan(settings_from_args(args))})
            return 0
        scope.open()
        try:
            if args.cmd == 'info':
                _emit({'ok': True, 'cmd': 'info', **scope.info()})
            elif args.cmd == 'capture':
                cmd_capture(args, scope)
            elif args.cmd == 'watch':
                cmd_watch(args, scope)
            elif args.cmd == 'stream':
                cmd_stream(args, scope)
            elif args.cmd == 'awg':
                cmd_awg(args, scope)
            elif args.cmd == 'release':
                _emit({'ok': True, 'cmd': 'release', 'note': 'device opened, idled and released'})
            elif args.cmd == 'selftest':
                from .selftest import run as selftest
                res = selftest(scope, quick=args.quick, awg=not args.no_awg)
                _emit(res)
                return 0 if res.get('ok') else 1
            elif args.cmd == 'debug':
                _emit({'ok': True, 'cmd': 'debug', **scope.debug(args)})
        finally:
            scope.close()
        return 0
    except DscopeError as e:
        _emit({'ok': False, 'cmd': args.cmd, 'error': e.to_dict()})
        return e.exit_code
    except KeyboardInterrupt:
        _emit({'ok': False, 'cmd': args.cmd, 'error': {'code': 'INTERRUPTED', 'message': 'interrupted'}})
        return 1
    except Exception as e:
        _emit({'ok': False, 'cmd': args.cmd, 'error': {'code': 'INTERNAL', 'message': repr(e),
                                                         'trace': traceback.format_exc().splitlines()[-6:]}})
        return 1
    finally:
        done.set()


if __name__ == '__main__':
    sys.exit(main())
