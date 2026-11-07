"""End-to-end offline test: recorded DSWave captures replayed through Scope.capture (compile -> engine -> decode
-> crop -> measure) with a fake engine. Skips when the git-ignored traces are not present."""
import json, os
import numpy as np
import pytest

from openm454 import scope as scopemod
from openm454.acquire import RawCapture

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESS = os.path.join(ROOT, 're', 'traces', 'S01_warm')
pytestmark = pytest.mark.skipif(not os.path.exists(os.path.join(SESS, 'bulk_in.bin')), reason='recorded traces not present')


def recorded(name):
    r = json.load(open(os.path.join(SESS, 'scen', name + '.json')))
    pk = sorted([b for b in r['bulk_in'] if b['pipe'] == 0x86 and b['actual']], key=lambda b: b['seq'])
    raw = RawCapture()
    with open(os.path.join(SESS, 'bulk_in.bin'), 'rb') as f:
        for i, b in enumerate(pk):
            f.seek(b['bin_off']); chunk = f.read(b['actual'])
            if i == 0:
                raw.head = chunk
            else:
                raw.data += chunk
    rec_prog = []
    for kind, name_, addr, val in r['run_writes']:
        rec_prog.append((addr, val))
        if addr == 0xF9:
            break
    return r, raw, rec_prog


class FakeEngine:
    def __init__(self, name):
        self.name = name
        self.programs = []

    def run(self, prog, **kw):
        self.programs.append(prog)
        r, raw, rec = recorded(self.name)
        self.recorded_program = rec
        cb = kw.get('on_chunk')
        if cb:
            cb(raw.head)
            pk = sorted([b for b in r['bulk_in'] if b['pipe'] == 0x86 and b['actual']], key=lambda b: b['seq'])[1:]
            off = 0
            for b in pk:
                cb(bytes(raw.data[off:off + b['actual']])); off += b['actual']
        return raw


class FakeDev:
    def __init__(self):
        self.zero_info = open(os.path.join(ROOT, 'state', 'cache', 'zero_info_from_trace.bin'), 'rb').read()
        self.info = {}


def settings_for(cfg, trig_source, pos=50.0):
    st = scopemod.Settings()
    st.analog = ['A%d' % i for i in range(4) if cfg['an'][i]]
    st.digital = ['D%d' % i for i in range(8) if cfg['dn'][i]]
    st.probe = {c: 1.0 for c in ('A0', 'A1', 'A2', 'A3')}
    st.vdiv = {'A%d' % i: cfg['vdiv'][i] / 1e6 for i in range(4)}
    st.offset = {'A%d' % i: (128 - cfg['voff'][i]) * (cfg['vdiv'][i] / 1e6) / 29.375 for i in range(4)}
    st.coupling = {'A%d' % i: 'ac' if cfg['coup'][i] else 'dc' for i in range(4)}
    st.threshold = cfg['thr'] / 1e6
    st.duration = cfg['time'] / 1e12
    st.samples = cfg['depth'] // 4
    st.trig_source = trig_source
    st.trig_edge = {0: 'rise', 1: 'fall', 2: 'any'}[cfg['ttype']]
    i = int(trig_source[1])
    st.trig_level = (cfg['tlev'][i] - cfg['voff'][i]) * (cfg['vdiv'][i] / 1e6) / 29.375 if trig_source[0] == 'A' else None
    st.trig_mode = 'normal'
    st.trig_pos = pos
    st.sensitivity = cfg['margin']
    return st


def run_capture(name, trig_source):
    sc = {s['name']: s for f in ('corpus2.json', 'corpus3.json') for s in json.load(open(os.path.join(ROOT, 'tests', 'fixtures', 'scenarios', f)))}
    cfg = sc[name]['cfg']
    s = scopemod.Scope()
    s.dev = FakeDev()
    s.engine = FakeEngine(name)
    cap = s.capture(settings_for(cfg, trig_source))
    return cap, s.engine


def test_program_matches_recording_te_a1_normal():
    cap, eng = run_capture('te_a1_normal', 'A1')
    mine = [w for w in eng.programs[0] if w[0] not in (0x44, 0x54, 0x64, 0x74)]
    rec = [w for w in eng.recorded_program if w[0] not in (0x44, 0x54, 0x64, 0x74)]
    assert mine == rec


def test_triggered_square_at_t0():
    cap, _ = run_capture('te_a1_normal', 'A1')
    assert cap.meta['acq']['triggered']
    m = cap.measure('A1', edges_n=10)
    assert m['freq_hz'] is None or abs(m['freq_hz'] - 998.75) < 3     # 1 ms window = 1 period only
    assert any(abs(t - 0.5e-3) < 20e-6 for t in m['edges_fall_s'])
    assert abs(m['vtop_v'] - 2.83) < 0.2 and abs(m['vbase_v']) < 0.1
    t_edges = list(m['edges_rise_s'])
    assert min(abs(t) for t in t_edges) < 15e-6          # slow (~8 us) edge, trigger at 1.4 V
    d = cap.measure('D0', edges_n=5)
    assert d['state'] == 'toggling'
    td = min(abs(t) for t in d['edges_rise_s'])
    assert td < 15e-6
    assert cap.measure('D1')['state'] == 'low'


@pytest.mark.parametrize('name,freq,lo,hi', [('awg_sine_10k', 10e3, 0.0, 2.0), ('awg_sq_5k_d25', 5e3, 0.0, 3.0),
                                             ('awg_sine_1M', 1e6, 0.0, 3.0)])
def test_awg_loopback_recordings(name, freq, lo, hi):
    cap, _ = run_capture(name, 'A0')
    m = cap.measure('A0')
    assert abs(m['freq_hz'] - freq) / freq < 0.005
    assert abs(m['vmax_v'] - hi) < 0.25 and abs(m['vmin_v'] - lo) < 0.25
    assert abs(len(cap.analog['A0']) - cap.meta['acq']['samples']) == 0


def test_stream_roll_mode_live_lines():
    sc = {x['name']: x for x in json.load(open(os.path.join(ROOT, 'tests', 'fixtures', 'scenarios', 'corpus4.json')))}
    st = scopemod.Settings()
    st.duration, st.samples = 2.0, 100_000
    st.offset = {c: 0.0 for c in ('A0', 'A1', 'A2', 'A3')}
    s = scopemod.Scope(); s.dev = FakeDev(); s.engine = FakeEngine('roll_mode')
    lines = []
    cap = s.stream(st, every=0.5, emit=lines.append)
    assert len(lines) >= 3 and lines[-1].get('final')
    mid = lines[1]
    assert 'A1' in mid and mid['A1']['vmax_v'] > 2.0 and abs(mid['A1']['vmin_v']) < 0.3
    assert 0.3 < mid['D0']['high_frac'] < 0.7 and mid['D0']['edges'] > 100
    m = cap.measure('A1')
    assert abs(m['freq_hz'] - 998.75) < 5
    assert cap.measure('D1')['state'] == 'low'
