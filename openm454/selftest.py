"""Self-test on the bench fixture. Wiring (as of 2026-10-31):
  A0 <- AWG output;  A1 (1x probe), A2 (10x probe), A3 (1x probe) <- ~2.8 Vpp 1 kHz calibration square;
  D0, D2, D5, D7 <- the same square;  D1, D3, D4, D6 floating.
Each check returns {id, desc, pass, measured, expected, gating}. Run: python -m openm454 selftest [--quick]
When state/fixture.json says the digital leads are wired to something else (openm454.fixture), the checks that need
the square on D0-D7 are reported as skipped.

Order = risk: device info, then one forced (untriggered) single-channel capture, then triggered captures. The first
device-level failure (DEVICE_ERROR, USB_ERROR, TIMEOUT_DATA) ends the run: every later check is reported as skipped
instead of talking to a device that is in an unknown state.
"""
import time

from . import fixture
from .errors import DscopeError
from .scope import Settings

ANALOG_SQ = ('A1', 'A2', 'A3')
DIG_ON = ('D0', 'D2', 'D5', 'D7')
DIG_OFF = ('D1', 'D3', 'D4', 'D6')
FATAL = ('DEVICE_ERROR', 'USB_ERROR', 'TIMEOUT_DATA', 'TIMEOUT_WATCHDOG', 'PROTOCOL_ERROR', 'UNSUPPORTED')


def _st(**kw):
    st = Settings()
    st.probe = {'A0': 1.0, 'A1': 1.0, 'A2': 10.0, 'A3': 1.0}
    st.vdiv = {c: 1.0 for c in ('A0', 'A1', 'A2', 'A3')}
    st.offset = {c: 1.5 for c in ('A0', 'A1', 'A2', 'A3')}
    st.duration, st.samples = 5e-3, 500_000
    st.trig_source, st.trig_edge, st.trig_level, st.trig_mode = 'A1', 'rise', 1.4, 'normal'
    st.timeout = 3.0
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def _near(x, ref, rel=None, abs_=None):
    if x is None:
        return False
    tol = (abs(ref) * rel if rel is not None else 0) + (abs_ or 0)
    return abs(x - ref) <= tol


def _t_of_first_edge_near_zero(cap, ch, rising=True):
    m = cap.measure(ch, edges_n=50)
    key = 'edges_rise_s' if rising else 'edges_fall_s'
    ts = list(m.get(key) or [])                    # relative to the trigger
    return min(ts, key=abs) if ts else None


def run(scope, quick=False, awg=True):
    items = []
    t_all = time.time()
    fatal = {}
    fx = fixture.load()

    def check(cid, desc, fn, gating=True, needs=None):
        if needs and fx.get(needs) != 'standard':
            items.append({'id': cid, 'desc': desc, 'pass': None, 'gating': False, 'skipped': True,
                          'measured': {'skipped': '%s leads are not on the standard fixture (state/fixture.json)'
                                                  % needs}, 'expected': None, 'ms': 0})
            return
        if fatal:
            items.append({'id': cid, 'desc': desc, 'pass': False, 'gating': gating, 'skipped': True,
                          'measured': {'skipped': 'device failed in %s' % fatal['id']}, 'expected': None, 'ms': 0})
            return
        t0 = time.time()
        try:
            ok, measured, expected = fn()
        except DscopeError as e:
            ok, measured, expected = False, {'error': e.to_dict()}, None
            if e.code in FATAL:
                fatal.update(id=cid, error=e.to_dict())
        except Exception as e:
            ok, measured, expected = False, {'error': repr(e)}, None
        items.append({'id': cid, 'desc': desc, 'pass': bool(ok), 'gating': gating, 'measured': measured,
                      'expected': expected, 'ms': round((time.time() - t0) * 1000)})

    def v01():
        i = scope.info()['device']
        ok = i['fpga_done'] and i['security_pass'] and int(i['fw_version'], 16) >= 0x3110 and i['usb_speed'] == 'super'
        return ok, {k: i[k] for k in ('fw_version', 'fpga_bin_version', 'panel_version', 'security_pass', 'usb_speed')}, \
            'fw>=0x3110, security pass, USB 3 SuperSpeed link'
    check('V01', 'device info', v01)

    def v00():
        cap = scope.capture(_st(analog=['A1'], digital=[], trig_source=None, trig_mode='force', duration=2e-3,
                                samples=20_000))
        m = cap.measure('A1')
        acq = cap.meta['acq']
        ok = acq.get('stop') in ('normal', None) and acq.get('complete', True) and m['vpp_v'] is not None and m['vpp_v'] > 1.0
        return ok, {'vpp_v': m['vpp_v'], 'stop': acq.get('stop'), 'handshakes': acq.get('handshakes'),
                    'complete': acq.get('complete'), 'samples': acq.get('samples')}, \
            'forced single-channel capture stops normally and shows the ~2.8 Vpp square on A1'
    check('V00', 'smoke: forced capture on A1 (lowest-risk run)', v00, needs='analog')

    ref = {}

    def v02():
        cap = scope.capture(_st(analog=['A0', 'A1', 'A2', 'A3'], digital=[]))
        m = {c: cap.measure(c) for c in ANALOG_SQ}
        f1, top1 = m['A1']['freq_hz'], m['A1']['vtop_v']
        ref.update(freq=f1, top=top1)
        ok = cap.meta['acq']['triggered'] and _near(f1, 1000, rel=0.02) and _near(top1, 2.8, rel=0.12) \
            and all(_near(m[c]['vbase_v'], 0.0, abs_=0.15) for c in ANALOG_SQ) and _near(m['A1']['duty_pct'], 50, abs_=3)
        ok = ok and all(_near(m[c]['freq_hz'], f1, rel=0.001) for c in ANALOG_SQ) and _near(m['A2']['vtop_v'], top1, rel=0.08)
        return ok, {c: {k: m[c].get(k) for k in ('freq_hz', 'vtop_v', 'vbase_v', 'duty_pct')} for c in ANALOG_SQ} | \
            {'triggered': cap.meta['acq']['triggered'], 'rate': cap.meta['acq']['analog_rate_hz']}, \
            'A1-A3 ~1 kHz +-2%, top 2.8 V +-12%, base 0 +-0.15 V, duty 50+-3%, channels agree (A2 via 10x)'
    check('V02', 'analog calibration square on A1-A3', v02, needs='analog')

    def v03():
        tops = {}
        for vd, off in ((0.5, 1.5), (1.0, 1.5), (2.0, 0.0)):
            cap = scope.capture(_st(analog=['A1'], digital=[], vdiv={'A0': 1, 'A1': vd, 'A2': 1, 'A3': 1},
                                    offset={'A0': 0, 'A1': off, 'A2': 0, 'A3': 0}))
            tops['%gV/div' % vd] = cap.measure('A1')['vtop_v']
        r = ref.get('top', 2.8)
        return all(_near(v, r, rel=0.06) for v in tops.values()), tops, 'vtop within 6%% of %.3g V at every V/div' % r
    check('V03', 'V/div consistency (A1 at 0.5/1/2 V/div)', v03, needs='analog')

    def v04():
        out = {}
        cases = [('A1', ['A1'], [], 400e6), ('A1,A3', ['A1', 'A3'], [], 200e6), ('A0-A3', ['A0', 'A1', 'A2', 'A3'], [], 100e6)]
        if not quick:
            cases += [('A1@1M', ['A1'], [], 1e6), ('A1@10M', ['A1'], [], 10e6)]
        for name, an, dg, rate in cases:
            st = _st(analog=an, digital=dg, rate=rate)          # 5 ms = 5 periods: >= 2 rising edges at any phase
            cap = scope.capture(st)
            out[name] = {'rate': cap.meta['acq']['analog_rate_hz'], 'freq_hz': cap.measure('A1')['freq_hz']}
        f = ref.get('freq', 1000)
        ok = all(_near(v['freq_hz'], f, rel=0.005) for v in out.values())
        ok = ok and out['A1']['rate'] == 400e6 and out['A1,A3']['rate'] == 200e6
        return ok, out, 'freq within 0.5%% of %.6g Hz at every rate; 1 ch = 400 MSa/s, 2 ch = 200 MSa/s' % f
    check('V04', 'sample-rate consistency incl. fold modes', v04, needs='analog')

    def v05():
        cap = scope.capture(_st(analog=['A1'], digital=['D%d' % i for i in range(8)]))
        md = {c: cap.measure(c) for c in cap.digital}
        on_ok = all(md[c]['state'] == 'toggling' and _near(md[c].get('freq_hz'), ref.get('freq', 1000), rel=0.005) for c in DIG_ON)
        off_ok = all(md[c]['state'] in ('low', 'high') for c in DIG_OFF)
        ta = _t_of_first_edge_near_zero(cap, 'A1'); td = _t_of_first_edge_near_zero(cap, 'D0')
        dt = abs(ta - td) if ta is not None and td is not None else None
        al = dt is not None and dt <= 2e-6     # A1 edge is slow (~8 us 10-90 %); digital switches mid-edge
        return on_ok and off_ok and al, {c: md[c]['state'] for c in md} | {'A1_vs_D0_edge_s': dt}, \
            'D0/D2/D5/D7 toggling at the reference frequency, D1/D3/D4/D6 static, D0 edge within 2 us of A1 mid crossing'
    check('V05', 'digital channels + alignment with A1', v05, needs='digital')

    def v06():
        res = {}
        for thr in (0.5, 4.0):
            cap = scope.capture(_st(analog=[], digital=['D0'], threshold=thr, trig_source=None, trig_mode='force'))
            res['%gV' % thr] = cap.measure('D0')['state']
        return res['0.5V'] == 'toggling' and res['4V'] in ('low', 'high'), res, '0.5 V toggles, 4.0 V static'
    check('V06', 'digital threshold', v06, needs='digital')

    def v07():
        res = {}
        for pos in ((10, 50, 90) if not quick else (50,)):
            cap = scope.capture(_st(analog=['A1'], digital=[], trig_pos=pos))
            t = _t_of_first_edge_near_zero(cap, 'A1')
            res['%d%%' % pos] = {'edge_t_s': t, 't0_s': cap.t0('A1'), 'triggered': cap.meta['acq']['triggered']}
        ok = all(v['triggered'] and v['edge_t_s'] is not None and abs(v['edge_t_s']) < 15e-6 for v in res.values())
        return ok, res, 'rising edge (mid-level) within 15 us of t=0 (slow ~8 us edge, trigger at 1.4 V)'
    check('V07', 'trigger position', v07, needs='analog')

    def v08():
        cap = scope.capture(_st(analog=['A1'], digital=['D5'], trig_source='D5', trig_edge='fall'))
        t = _t_of_first_edge_near_zero(cap, 'D5', rising=False)
        return cap.meta['acq']['triggered'] and t is not None and abs(t) < 1e-6, {'D5_fall_t_s': t}, 'D5 falling edge at t=0 +-1 us'
    check('V08', 'digital trigger source (D5 falling)', v08, needs='digital')

    def v09():
        if awg:
            scope.awg_off()
        t0 = time.time()
        try:
            scope.capture(_st(analog=['A0'], digital=[], trig_source='A0', trig_level=1.5, timeout=1.0))
            return False, {'result': 'captured'}, 'TIMEOUT_NO_TRIGGER'
        except DscopeError as e:
            dt = time.time() - t0
            return e.code == 'TIMEOUT_NO_TRIGGER' and dt < 3.0, {'code': e.code, 'seconds': round(dt, 2)}, 'TIMEOUT_NO_TRIGGER within ~1 s'
    check('V09', 'normal-mode timeout with no signal (A0, AWG off)', v09, needs='analog')

    if awg:
        def v10():
            res = {}
            scope.awg_set('sine', 10e3, 2.0, 0.0)
            cap = scope.capture(_st(analog=['A0'], digital=[], trig_source='A0', trig_level=1.0,
                                    vdiv={'A0': 0.5, 'A1': 1, 'A2': 1, 'A3': 1}, offset={'A0': 1.0, 'A1': 0, 'A2': 0, 'A3': 0},
                                    duration=500e-6))
            m = cap.measure('A0'); res['sine10k'] = {k: m.get(k) for k in ('freq_hz', 'vpp_v', 'vmean_v')}
            ok = _near(m['freq_hz'], 10e3, rel=0.005) and _near(m['vpp_v'], 2.0, rel=0.12) and _near(m['vmean_v'], 1.0, abs_=0.1)
            scope.awg_set('square', 5e3, 3.0, 0.0, duty=25)
            cap = scope.capture(_st(analog=['A0'], digital=[], trig_source='A0', trig_level=1.5, duration=1e-3))
            m = cap.measure('A0'); res['square5k_25'] = {k: m.get(k) for k in ('freq_hz', 'duty_pct', 'vtop_v', 'vbase_v')}
            ok = ok and _near(m['freq_hz'], 5e3, rel=0.002) and _near(m['duty_pct'], 25, abs_=2)
            scope.awg_off()
            cap = scope.capture(_st(analog=['A0'], digital=[], trig_source=None, trig_mode='force'))
            m = cap.measure('A0'); res['off'] = {'vpp_v': m.get('vpp_v'), 'vmean_v': m.get('vmean_v')}
            ok = ok and m['vpp_v'] < 0.15 and abs(m['vmean_v']) < 0.15
            return ok, res, 'sine 10 kHz 2 Vpp mean 1 V; square 5 kHz 25 %; off = flat at 0 V'
        check('V10', 'AWG loopback on A0', v10, needs='analog')

    def v11():
        n, t0 = 5 if quick else 20, time.time()
        for _ in range(n):
            scope.capture(_st(analog=['A0', 'A1', 'A2', 'A3'], digital=['D%d' % i for i in range(8)], samples=100_000, duration=1e-3))
        per = (time.time() - t0) / n
        return per < 1.5, {'captures': n, 'seconds_per_capture': round(per, 3)}, '< 1.5 s per 4-ch 100k capture'
    check('V11', 'repeated captures / latency', v11, needs='analog')

    passed = sum(1 for i in items if i['pass'])
    skipped = sum(1 for i in items if i.get('skipped') and i['pass'] is None)
    res = {'ok': all(i['pass'] for i in items if i['gating']), 'cmd': 'selftest', 'passed': passed,
           'failed': len(items) - passed - skipped, 'skipped': skipped, 'fixture': fx,
           'seconds': round(time.time() - t_all, 1), 'items': items}
    if fatal:
        res['stopped_at'] = fatal['id']
        res['device_error'] = fatal['error']
    return res
