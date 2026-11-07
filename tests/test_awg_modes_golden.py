"""openm454.awg_modes (AWG modulation / sweep / burst) against DSWave 1.2.1.

Golden part: compares tests/fixtures/scenarios/corpus5.json with DSWave's recorded register programs. The recorded programs come
from tests/fixtures/awg_modes_programs.json (committed, like tests/fixtures/awg_programs.json for corpus3), made after
the hal_run recording with
    python tests/test_awg_modes_golden.py --extract re/traces/<SESSION>
or, while that fixture does not exist, straight from $DSCOPE_CORPUS5_SESSION or the newest re/traces/*/scen holding
corpus5 results (git-ignored, local only). Until one of them exists the golden tests skip.

Offline part (always runs): corpus sanity (DSWave's own SCPI spellings, no command that makes DSWave assert, stored
predictions current), program_* == the HAL replay, pinned values from the decompiled HAL's tables and the
argument validation.
"""
import functools
import glob
import json
import os
import random
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from openm454 import awg, awg_modes as M  # noqa: E402
from openm454.errors import DscopeError  # noqa: E402

CORPUS_PATH = os.path.join(ROOT, 'tests', 'fixtures', 'scenarios', 'corpus5.json')
FIXTURE_PATH = os.path.join(ROOT, 'tests', 'fixtures', 'awg_modes_programs.json')
CORPUS = json.load(open(CORPUS_PATH, encoding='utf-8'))
NAMES = [s['name'] for s in CORPUS]
BY_NAME = {s['name']: s for s in CORPUS}

#: AWG SCPI tokens from DSWave.exe's token table, an independent source
#: for the spellings in openm454.awg_modes.SCPI (which come from the GUI parameter table, RVA 0xBABBC0).
DSWAVE_AWG_TOKENS = (
    'Enable TRIGGERdelay TRIGGERoutput TRIGGERedge TRIGGERsource BURSTperiod BURSTgatePolarity BURSTcounter '
    'BURSTcyclesNum BURSTncycleMode BURSTmode SWEEPfrequencyMark SWEEPtime SWEEPendFrequency SWEEPstartFrequency '
    'SWEEPdirection SWEEPmode PSKpolarity FSKfrequency PMphase FMfrequency AMdepth MODULATIONfrequency MODULATIONwave '
    'MODULATIONsource MODULATIONmode FALLtime RISEtime DUTY LOWlevel HIGHlevel AMPtune PHASE FREQUENCY WAVE Mode'
).split()

#: Prior HAL states used to find which predicted writes depend on what ran before a scenario (masked in comparisons).
PERTURBED = (
    {},
    dict(running=1, enabled=0, mode=3, freq=1234.5, high=0.31, low=0.07, mod_type=1, depth=37.5, swp_type=1,
         swp_dir=1, swp_start=40000, swp_end=4000, swp_time=10 ** 12, ratio_pos=7),
    dict(running=0, enabled=1, mode=1, mod_type=0, depth=80.0, high=0.2, low=0.15),
)

RECORD_HINT = ('corpus5 not recorded yet: record tests/fixtures/scenarios/corpus5.json in a traced '
               'DSWave-copy session, then run python tests/test_awg_modes_golden.py --extract re/traces/<SESSION>')


# ---- recorded programs ---------------------------------------------------------------------------------------------
_REG_RX = re.compile(r'BSP: (?:RegWrite|Queue set) (AWG_\w+|Unknown AwgRegAddr)\(0x([0-9A-Fa-f]{2})\)=0x([0-9A-Fa-f]{4})')
_ID_RX = re.compile(r'BSP: AWG scpi command (\d+) handled')
_TS_RX = re.compile(r'Checking time_step: (\d+) ps, (fcw_step_hw|ratio_pos): (\d+)')


def extract_scenario(result):
    """hal_run result (SESSION/scen/<name>.json) -> {name, writes, ids, rets, time_step}. AWG writes are the pairs
    W 0x08 = awg_addr, W 0x07 = value in the HAL log (the log also covers writes printed after config_writes ends);
    time_step holds the "Checking time_step" lines per SCPI command ([kind, ts_ps, value] lists)."""
    lines = result.get('log') or []
    writes, addr, ts = [], None, []
    for line in lines:
        if 'SCPI command parsed successfully' in line:
            ts.append([])
            continue
        t = _TS_RX.search(line)
        if t and ts:
            ts[-1].append([t.group(2), int(t.group(1)), int(t.group(3))])
            continue
        m = _REG_RX.search(line)
        if not m:
            continue
        reg, val = int(m.group(2), 16), int(m.group(3), 16)
        if reg == 0x08:
            addr = val
        elif reg == 0x07 and addr is not None:
            writes.append([addr, val])
            addr = None
    if not lines:                                    # no log captured: fall back to the parsed register writes
        for _kind, name, reg, val in result.get('config_writes', []) + result.get('run_writes', []):
            if not name.startswith('AWG'):
                continue
            if reg == 0x08:
                addr = val
            elif reg == 0x07 and addr is not None:
                writes.append([addr, val])
                addr = None
    ids = [int(x) for line in lines for x in _ID_RX.findall(line)]
    rets = []
    for x in result.get('scpi', []):
        r = x[1] if len(x) > 1 else None
        rets.append(r - (1 << 32) if isinstance(r, int) and r >= 1 << 31 else r)
    return {'name': result['name'], 'writes': writes, 'ids': ids, 'rets': rets, 'time_step': ts}


def _session_candidates():
    env = os.environ.get('DSCOPE_CORPUS5_SESSION')
    if env:
        return [env if os.path.isabs(env) else os.path.join(ROOT, env)]
    dirs = [d for d in glob.glob(os.path.join(ROOT, 're', 'traces', '*')) if os.path.isdir(os.path.join(d, 'scen'))]
    return sorted(dirs, key=os.path.getmtime, reverse=True)


def load_session(session):
    out = {}
    for name in NAMES:
        p = os.path.join(session, 'scen', name + '.json')
        if os.path.exists(p):
            out[name] = extract_scenario(json.load(open(p, encoding='utf-8')))
    return out


@functools.lru_cache(maxsize=None)
def recorded():
    if os.path.exists(FIXTURE_PATH):
        return {e['name']: e for e in json.load(open(FIXTURE_PATH, encoding='utf-8'))}
    for session in _session_candidates():
        rec = load_session(session)
        if rec:
            return rec
    return {}


# ---- model predictions -----------------------------------------------------------------------------------------------
@functools.lru_cache(maxsize=None)
def chain_states():
    """HAL state before each scenario when corpus5 runs in file order from a fresh, enabled AWG."""
    hal, states = M.DswaveAwgHal(enabled=1), {}
    for s in CORPUS:
        states[s['name']] = hal.state()
        hal = M.replay(s['scpi'], hal)['hal']
    return states


def predictions(name):
    """Replays of one scenario from its chained state and from the PERTURBED prior states (first = chained)."""
    scpi = BY_NAME[name]['scpi']
    starts = [chain_states()[name]] + [dict(M.DswaveAwgHal().state(), **p) for p in PERTURBED]
    return [M.replay(scpi, M.DswaveAwgHal(**st)) for st in starts]


def _fmt(w):
    return '%s=%d' % (M.REG_NAMES.get(w[0], hex(w[0])), w[1])


def _require(name):
    rec = recorded()
    if not rec:
        pytest.skip(RECORD_HINT)
    if name not in rec:
        pytest.skip('%s not in the corpus5 recording' % name)
    return rec[name]


# ---- golden: HAL replay vs DSWave -----------------------------------------------------------------------------------
@pytest.mark.parametrize('name', NAMES)
def test_hal_replay_matches_dswave(name):
    """Every AWG write DSWave sent for the scenario, except the few that depend on what ran before it."""
    rec = _require(name)
    preds = predictions(name)
    p0 = preds[0]['writes']
    got = [tuple(w) for w in rec['writes']]
    assert preds[0]['ids'] == rec['ids'], 'HAL command ids: predicted %s, DSWave %s' % (preds[0]['ids'], rec['ids'])
    for i, r in enumerate(preds[0]['rets']):
        if r is not None and i < len(rec['rets']) and rec['rets'][i] is not None:
            assert rec['rets'][i] == r, 'return code of %r' % BY_NAME[name]['scpi'][i]
    if len({len(p['writes']) for p in preds}) == 1:
        assert len(got) == len(p0), 'write count: predicted %d, DSWave %d' % (len(p0), len(got))
        diffs = []
        for i, w in enumerate(p0):
            alts = {p['writes'][i] for p in preds}
            if len(alts) == 1 and got[i] != w:
                diffs.append('#%d predicted %s, DSWave %s' % (i, _fmt(w), _fmt(got[i])))
            elif len({a for a, _ in alts}) == 1 and got[i][0] != w[0]:
                diffs.append('#%d address: predicted %s, DSWave %s' % (i, _fmt(w), _fmt(got[i])))
        assert not diffs, '\n'.join(diffs)
    finals = [M.final(p['writes']) for p in preds]
    fin = M.final(got)
    bad = ['%s predicted %d, DSWave %s' % (M.REG_NAMES.get(a, hex(a)), v, fin.get(a))
           for a, v in finals[0].items() if all(f.get(a) == v for f in finals) and fin.get(a) != v]
    assert not bad, 'final registers:\n' + '\n'.join(bad)


@pytest.mark.parametrize('name', [s['name'] for s in CORPUS if s.get('expect_log')])
def test_sweep_time_step_log(name):
    """The HAL's own 'Checking time_step' debug lines (log level 5) against the replay, where state-independent."""
    rec = _require(name)
    if not any(rec.get('time_step') or []):
        pytest.skip('no "Checking time_step" lines in the recorded log (debug level not captured)')
    preds = predictions(name)
    scpi = BY_NAME[name]['scpi']
    assert len(rec['time_step']) == len(scpi), 'log segments: %d, SCPI commands: %d' % (len(rec['time_step']),
                                                                                       len(scpi))
    checked = 0
    for i, cmd in enumerate(scpi):
        segs = [[list(x) for x in p['per_cmd_log'][i]] for p in preds]
        if any(s != segs[0] for s in segs):
            continue                                             # depends on the sweep state before the scenario
        assert rec['time_step'][i] == segs[0], cmd
        checked += bool(segs[0])
    if not checked:
        pytest.skip('every time_step line of this scenario depends on the state before it')


@pytest.mark.parametrize('name', [s['name'] for s in CORPUS if s.get('driver')])
def test_driver_program_matches_dswave(name):
    """program_modulation / program_sweep / program_burst leave the registers exactly as DSWave did."""
    rec = _require(name)
    d = BY_NAME[name]['driver']
    prog = getattr(M, d['fn'])(**d['kwargs'])
    fin = M.final([tuple(w) for w in rec['writes']])
    bad = ['%s driver %d, DSWave %s' % (M.REG_NAMES.get(a, hex(a)), v, fin.get(a))
           for a, v in M.final(prog[1:]).items() if fin.get(a) != v]
    assert not bad, '\n'.join(bad)


# ---- offline: the corpus itself -------------------------------------------------------------------------------------
def test_corpus_format():
    assert len(NAMES) == len(set(NAMES)), 'scenario names must be unique'
    assert len(CORPUS) == 73
    for s in CORPUS:
        assert s['family'] in ('mod', 'sweep', 'burst'), s['name']
        assert s['run'] is None, s['name']                       # register programs only, no scope capture
        assert s['scpi'] and all(isinstance(c, str) for c in s['scpi']), s['name']
    assert CORPUS[-1]['name'] == 'bst_restore'                   # leaves the AWG stopped in basic mode at LOW 0
    last = M.replay(CORPUS[-1]['scpi'], M.DswaveAwgHal(**chain_states()['bst_restore']))
    fin = M.final(last['writes'])
    assert fin[M.A_ENABLE] == 2 and fin[M.A_MODE] == 0 and fin[M.A_LOW] == 0


def test_scpi_table_matches_dswave_tokens():
    assert set(M.SCPI) == {':AWG:' + t for t in DSWAVE_AWG_TOKENS}
    ids = sorted(cid for cid, _ in M.SCPI.values())
    assert ids == list(range(44, 80))


def test_corpus_scpi_spelling_and_arguments():
    for s in CORPUS:
        for c in s['scpi']:
            head, _, args = c.partition(' ')
            vals = args.split(',')
            assert len(vals) == 2 and all(re.fullmatch(r'\d+', v) for v in vals), (s['name'], c)
            if head in (':Work:RUN', ':Work:STOP'):
                assert vals == ['8', '0'], (s['name'], c)            # index 8 = the AWG (corpus3)
            else:
                assert head in M.SCPI, (s['name'], c)                # exact DSWave spelling
                assert vals[0] == '0' and int(vals[1]) < 2 ** 64, (s['name'], c)


def test_corpus_never_makes_dswave_assert():
    """No scenario hits a DSWave assert / fault (BURSTperiod 0, sweep start/end 0, a log sweep too fast, AM ~300 %),
    whatever state the HAL is in before it, and no scenario leaves HIGH above the 3 V code."""
    for start in PERTURBED:
        hal = M.DswaveAwgHal(**start)
        for s in CORPUS:
            res = M.replay(s['scpi'], hal)
            hal = res['hal']
            fin = M.final(res['writes'])
            assert fin.get(M.A_HIGH, 0) <= M.CODE_3V, s['name']
    for s in CORPUS:                                             # also each scenario alone (hal_run --only)
        for start in PERTURBED:
            M.replay(s['scpi'], M.DswaveAwgHal(**start))


def test_corpus_predictions_are_current():
    """The expect_* fields stored in corpus5.json equal the replay (chained in file order from an enabled AWG)."""
    hal = M.DswaveAwgHal(enabled=1)
    for s in CORPUS:
        res = M.replay(s['scpi'], hal)
        hal = res['hal']
        assert [list(w) for w in res['writes']] == s['expect_writes'], s['name']
        assert {'0x%02x' % a: v for a, v in M.final(res['writes']).items()} == s['expect_final'], s['name']
        assert res['ids'] == s['expect_ids'], s['name']
        assert [list(x) for x in res['log']] == s.get('expect_log', []), s['name']


def test_corpus_driver_entries_match_replay():
    n = 0
    for s in CORPUS:
        d = s.get('driver')
        if not d:
            continue
        prog = getattr(M, d['fn'])(**d['kwargs'])
        fin = {int(k, 16): v for k, v in s['expect_final'].items()}
        assert {a: v for a, v in M.final(prog[1:]).items()} == {a: fin[a] for a in M.final(prog[1:])}, s['name']
        n += 1
    assert n >= 50


def test_hal_replay_reproduces_corpus3_fixture():
    """The same replay reproduces the recorded basic-mode programs (corpus3) write for write when chained."""
    fix = json.load(open(os.path.join(ROOT, 'tests', 'fixtures', 'awg_programs.json'), encoding='utf-8'))
    hal = M.DswaveAwgHal(enabled=1, running=1)
    for case in fix:
        a = case['awg']
        scpi = [':AWG:Enable 0,1', ':AWG:Mode 0,0', ':AWG:WAVE 0,%d' % a['wave'],
                ':AWG:FREQUENCY 0,%d' % M.micro(a['freq']), ':AWG:PHASE 0,%d' % a['phase'],
                ':AWG:HIGHlevel 0,%d' % M.micro(a['high']), ':AWG:LOWlevel 0,%d' % M.micro(a['low']),
                ':AWG:DUTY 0,%d' % M.micro(a['duty']), ':Work:RUN 8,0']
        res = M.replay(scpi, hal)
        hal = res['hal']
        assert [list(w) for w in res['writes']] == case['writes'], case['name']


# ---- offline: program_* == HAL replay -------------------------------------------------------------------------------
def _cmd_writes(res, first, last=None):
    out = []
    for w in res['per_cmd'][first:last]:
        out += w
    return out


def _after_duty(prog):
    i = max(k for k, (a, _) in enumerate(prog) if a == M.A_FSLOPE)
    return prog[i + 1:]


MOD_CASES = [
    dict(kind='am', depth=50.0), dict(kind='am', depth=0.0, mod_freq=0.1), dict(kind='am', depth=100.0,
                                                                                mod_wave='square'),
    dict(kind='am', depth=120.0, high=2.5, low=0.5), dict(kind='am', depth=33.3, high=2.5, low=0.5, freq=20e3),
    dict(kind='fm', deviation=1000.0, mod_wave='square'), dict(kind='fm', deviation=123.45, wave='triangle',
                                                                freq=5000.0, mod_freq=20.0),
    dict(kind='fsk', hop=12345.678, wave='square', freq=1000.0, mod_freq=50.0),
    dict(kind='pm', phase_dev=90.0, experimental=True), dict(kind='ask', freq=200e3, mod_freq=20e3,
                                                             experimental=True),
    dict(kind='psk', psk_inverted=True, experimental=True),
]


@pytest.mark.parametrize('kw', MOD_CASES, ids=[str(i) for i in range(len(MOD_CASES))])
def test_program_modulation_equals_hal(kw):
    kw = dict(kw)
    exp = kw.pop('experimental', False)
    prog = M.program_modulation(experimental=exp, **kw)
    t = M.MOD_KINDS[kw['kind']]
    res = M.replay(M.modulation_scpi(**kw), M.DswaveAwgHal(enabled=1, mode=1, mod_type=t))
    assert prog[0] == (M.A_ENABLE, 2)
    assert prog[1:4] == res['per_cmd'][1]                        # Mode 0,1
    assert prog[4:5] == res['per_cmd'][2]                        # MODULATIONmode before the carrier/levels
    assert _after_duty(prog) == _cmd_writes(res, 9)              # source, wave, frequency, type parameter
    hal_final, mine = M.final(res['writes'][1:]), M.final(prog[1:])
    assert mine == hal_final                                     # carriers chosen where awg.py == HAL


SWEEP_CASES = [
    dict(start=1000.0, end=10000.0, time=1.0), dict(start=1000.0, end=10000.0, time=28.8),
    dict(start=1000.0, end=10000.0, time=150.0), dict(start=100.0, end=1e6, time=1e-3),
    dict(start=10000.0, end=1000.0, time=1.0, direction='down'), dict(start=100.0, end=100e3, time=1.0,
                                                                      sweep_type='log'),
    dict(start=100.0, end=100e3, time=1e-3, sweep_type='log'), dict(start=0.1, end=1e6, time=1.0, sweep_type='log'),
    dict(start=1e6, end=1000.0, time=1.0, sweep_type='log', direction='down'),
    dict(start=1000.0, end=10000.0, time=1.0, mark=7000.0), dict(start=1000.0, end=10000.0, time=1.0, wave='square'),
]


@pytest.mark.parametrize('kw', SWEEP_CASES, ids=[str(i) for i in range(len(SWEEP_CASES))])
def test_program_sweep_equals_hal(kw):
    prog = M.program_sweep(**kw)
    steady = M.DswaveAwgHal(enabled=1, swp_dir=M.SWEEP_DIRS[kw.get('direction', 'up')],
                            swp_start=M.fcw40(kw['start']), swp_end=M.fcw40(kw['end']), swp_time=M.pico(kw['time']))
    res = M.replay(M.sweep_scpi(**kw), steady)
    assert prog[0] == (M.A_ENABLE, 2)
    assert _after_duty(prog) == _cmd_writes(res, 8)              # sweep commands + Mode 2, write for write
    assert prog[-7:] == [(2, 1), (1, 2), (14, 4), (15, 0), (16, 4), (17, 0), (2, 0)]
    assert M.final(prog[1:]) == M.final(res['writes'][1:])


BURST_CASES = [
    dict(freq=10e3, cycles=3, period=1e-3), dict(freq=10e3, cycles=100, period=20e-3),
    dict(freq=10e3, cycles=3, period=350e-6), dict(freq=10e3, cycles=3, period=15.0),
    dict(freq=10e3, burst_mode='gated', gate_polarity='inverted', period=2e-3),
    dict(freq=10e3, cycles=3, period=1e-3, trig_source='external', trig_edge='falling'),
    dict(freq=10e3, cycles=3, period=1e-3, trig_source='manual', trig_out='falling'),
    dict(freq=10e3, cycles=3, period=1e-3, trig_delay=250e-6, counter=32),
    dict(wave='square', freq=1000.0, cycles=5, period=0.1), dict(),
]


@pytest.mark.parametrize('kw', BURST_CASES, ids=[str(i) for i in range(len(BURST_CASES))])
def test_program_burst_equals_hal(kw):
    prog = M.program_burst(**kw)
    res = M.replay(M.burst_scpi(**kw), M.DswaveAwgHal(enabled=1))
    assert prog[0] == (M.A_ENABLE, 2)
    assert prog[1:4] == res['per_cmd'][1]                        # Mode 0,3 before the carrier
    assert _after_duty(prog) == _cmd_writes(res, 8)              # the ten burst/trigger commands
    assert M.final(prog[1:]) == M.final(res['writes'][1:])


def test_programs_do_not_start_and_use_known_registers():
    progs = [M.program_modulation('am'), M.program_sweep(1000.0, 2000.0, 1.0), M.program_burst()]
    for p in progs:
        assert (M.A_ENABLE, 3) not in p                          # configure only; the caller appends awg.START
        assert all(a in M.REG_NAMES and 0 <= v <= 0xFFFF for a, v in p)
        assert not any(a in (M.A_REDGE0, M.A_REDGE1, M.A_FEDGE0, M.A_FEDGE1, M.A_SWP_MARK) for a, _ in p)


def test_am_levels_stay_inside_3v_for_gui_depths():
    rnd = random.Random(5)
    for _ in range(2000):
        low = round(rnd.uniform(0.0, 2.9), 3)
        high = round(rnd.uniform(low + 0.01, 3.0), 3)
        depth = round(rnd.uniform(0.0, M.am_depth_max(high, low)), 1)
        lv = M.am_levels(high, low, min(depth, M.am_depth_max(high, low)))
        assert 0 <= lv[M.A_LOW] <= lv[M.A_HIGH] <= M.CODE_3V, (high, low, depth, lv)


# ---- offline: pinned values (the decompiled HAL's tables) ------------------------------------------------------------
def test_pinned_modulation_values():
    assert M.am_state_param(50) == (5120, 16400)
    assert M.am_state_param(0) == (6144, 0)
    assert M.am_state_param(100) == (4096, 32800)
    assert M.am_state_param(100.1) == (4094, 32800)
    assert M.am_state_param(120) == (3686, 36432)
    assert M.am_state_param(200) == (2050, 0x10000)
    assert M.am_state_param(50, mod_type=6) == (0x803, 0x10000)
    assert M.am_state_param(33.3) == (5460, 10922)
    with pytest.raises(M.DswaveAssert):
        M.am_state_param(300)
    assert M.am_levels(3.0, 0.0, 50) == {M.A_HIGH: 409, M.A_LOW: 58}
    assert M.am_levels(3.0, 0.0, 0) == {M.A_HIGH: 351, M.A_LOW: 117}
    assert M.am_levels(3.0, 0.0, 100) == {M.A_HIGH: 468, M.A_LOW: 0}
    assert M.am_levels(2.5, 0.5, 120) == {M.A_HIGH: 405, M.A_LOW: 62}
    assert M.am_levels(2.8, 0.0, 120) == {M.A_HIGH: 458, M.A_LOW: 0}
    assert M.am_depth_max(3.0, 0.0) == 100.0 and M.am_depth_max(2.5, 0.5) == 120.0
    assert M.fm_fdev(1000) == 20000 and M.fm_fdev(123.45) == 2469 and M.fm_fdev(10000) == 200000
    assert M.pm_pdev(90) == 5 and M.pm_pdev(359.999999) == 19
    assert M.fcw40(0.1) == 4 and M.fcw40(12345.678) == 493827 and M.fcw40(123456.7) == 4938268


def test_pinned_sweep_values():
    i = M.sweep_info(1000, 10000, 1.0)
    assert (i['sfcw'], i['efcw'], i['step_fcw'], i['tinc'], i['one_cycle']) == (40000, 400000, 36, 5000, 0)
    assert M.sweep_info(1000, 10000, 0.01)['step_fcw'] == 3600
    i = M.sweep_info(1000, 10000, 28.8)
    assert (i['step_fcw'], i['one_cycle'], i['tinc']) == (1, 1, 5000)
    assert M.sweep_info(1000, 10000, 150.0)['tinc'] == 20834
    assert M.sweep_info(1000, 10000, 500.0)['tinc'] == 69445
    assert M.sweep_info(1000, 4125, 1.0)['step_fcw'] == 13            # 12.5 rounds half away from zero
    assert M.sweep_info(100, 1e6, 1e-3)['step_fcw'] == 3999600
    i = M.sweep_info(100, 100e3, 1.0, 'log')
    assert (i['logr'], i['tinc']) == (10, 7065)
    assert M.sweep_info(100, 100e3, 0.01, 'log')['logr'] == 3
    assert (M.sweep_info(100, 100e3, 100.0, 'log')['logr'], M.sweep_info(100, 100e3, 100.0, 'log')['tinc']) \
        == (10, 706514)
    i = M.sweep_info(100, 100e3, 1e-3, 'log')
    assert (i['logr'], i['tinc']) == (1, 2934)
    assert M.sweep_info(1000, 2000, 1.0, 'log')['logr'] == 13
    assert M.sweep_info(1000, 1500, 1.0, 'log')['logr'] == 14
    i = M.sweep_info(0.1, 1e6, 1.0, 'log')
    assert (i['sfcw'], i['logr'], i['tinc']) == (4, 9, 6052)
    assert M.sweep_info(1e6, 1000, 1.0, 'log', 'down')['tinc'] == 7072
    prog = M.final(M.program_sweep(100, 100e3, 1.0, 'log'))
    assert prog[M.A_SWP_SFCW0] | prog[M.A_SWP_SFCW1] << 16 == 4000 << 10
    prog = M.final(M.program_sweep(1e6, 1000, 1.0, 'log', 'down'))
    assert prog[M.A_SWP_SFCW2] == 9                              # 4e7 << 10 needs the third word
    assert M.mark_fcw(7000) == 300647 and M.mark_fcw(1234.567891) == 53024


def test_pinned_burst_and_edge_values():
    assert M.trig_fcw(1e-3) == 42949 and M.trig_fcw(10e-3) == 4294 and M.trig_fcw(350e-6) == 122713
    assert M.trig_fcw(20e-3) == 2147 and M.trig_fcw(15.0) == 2
    assert abs(M.burst_info(1e-3)['period_effective_s'] - 1.0000157e-3) < 1e-9
    d = M.trig_delay_ns(250e-6)
    assert (d & 0xFFFF, d >> 16) == (53392, 3)
    d = M.trig_delay_ns(10.0)
    assert (d & 0xFFFF, (d >> 16) & 0xFFFF) == (58368, 21515)    # the GUI's 10 s wraps to 1.41 s
    assert M.burst_cycles_max(10e3) == 100 and M.burst_cycles_max(5.0) == 74 and M.burst_cycles_max(0.1) == 1
    assert M.edge_param(1e6) == 4 and M.edge_param(10e3) == 385 and M.edge_param(1000) == 3846
    assert M.slopes(50)[1:] == (137, 137) and M.slopes(10)[1:] == (689, 76) and M.slopes(30)[1:] == (228, 98)
    assert M.level_code(3.0) == 468 and M.level_code(0.1) == 15 and M.level_code(0.0) == 0
    with pytest.raises(M.DswaveAssert):
        M.DswaveAwgHal().burst_period(0, 0)


def test_rise_fall_time_unsupported():
    hal = M.DswaveAwgHal()
    assert hal.scpi(':AWG:RISEtime 0,10000') == (M.HAL_UNSUPPORTED, [])
    assert hal.scpi(':AWG:FALLtime 0,1000000') == (M.HAL_UNSUPPORTED, [])


def test_hal_hazards():
    hal = M.DswaveAwgHal(enabled=1)
    with pytest.raises(M.DswaveAssert):
        hal.scpi(':AWG:SWEEPstartFrequency 0,10000')             # 0.01 Hz -> FCW 0 asserts
    hal = M.DswaveAwgHal(enabled=1)
    hal.scpi(':AWG:SWEEPmode 0,0')                               # linear recompute with start = end = 0: safe
    hal.scpi(':AWG:SWEEPmode 0,1')                               # log with start == end: n = 0, no assert
    hal.scpi(':AWG:SWEEPmode 0,0')
    hal.scpi(':AWG:SWEEPstartFrequency 0,1000000000')            # no recompute, end still 0
    with pytest.raises(M.DswaveAssert):
        hal.scpi(':AWG:SWEEPmode 0,1')                           # log recompute with end = 0 -> ts 0 -> assert
    hal = M.DswaveAwgHal(mode=1, mod_type=0)
    assert M.final(hal.scpi(':AWG:HIGHlevel 0,3000000')[1]) == {M.A_HIGH: 351, M.A_LOW: 117}   # AM, depth 0
    assert hal.scpi(':AWG:Mode 0,0')[1] == [(2, 1), (1, 0), (2, 0)]   # Mode does not recompute the levels
    assert M.final(hal.scpi(':AWG:LOWlevel 0,0')[1]) == {M.A_HIGH: 468, M.A_LOW: 0}


# ---- offline: validation ------------------------------------------------------------------------------------------
BAD_MOD = [
    dict(kind='pm', phase_dev=10.0), dict(kind='xx'), dict(kind='am', wave='noise'), dict(kind='am', freq=2e6),
    dict(kind='am', wave='triangle', freq=500e3), dict(kind='am', high=3.5), dict(kind='am', low=-0.1),
    dict(kind='am', high=1.0, low=2.0), dict(kind='am', mod_freq=0.05), dict(kind='am', mod_freq=2e6),
    dict(kind='am', depth=101.0), dict(kind='am', depth=-1.0), dict(kind='am', depth=121.0, high=2.5, low=0.5),
    dict(kind='am', depth=float('nan')), dict(kind='fm'), dict(kind='fm', deviation=10001.0),
    dict(kind='fm', deviation=-1.0), dict(kind='fm', deviation=100.0, mod_freq=20e3),
    dict(kind='fsk'), dict(kind='fsk', hop=2e6), dict(kind='fsk', hop=0.05), dict(kind='fsk', wave='triangle',
                                                                                    hop=500e3),
    dict(kind='fsk', wave='square', freq=1000.0, hop=1e6, duty=30.0), dict(kind='am', source='external'),
    dict(kind='am', source='other'), dict(kind='am', mod_wave='noise'), dict(kind='am', wave='triangle', duty=5.0),
    dict(kind='pm', phase_dev=400.0, experimental=True), dict(kind='pm', experimental=True),
]


@pytest.mark.parametrize('kw', BAD_MOD, ids=[str(i) for i in range(len(BAD_MOD))])
def test_program_modulation_rejects(kw):
    with pytest.raises(DscopeError) as e:
        M.program_modulation(**kw)
    assert e.value.code == 'INVALID_ARG'


BAD_SWEEP = [
    dict(start=0.05, end=1000.0, time=1.0), dict(start=1000.0, end=2e6, time=1.0),
    dict(start=1000.0, end=500e3, time=1.0, wave='triangle'), dict(start=5000.0, end=5000.0, time=1.0),
    dict(start=0.1, end=0.11, time=1.0), dict(start=2000.0, end=1000.0, time=1.0),
    dict(start=1000.0, end=2000.0, time=1.0, direction='down'), dict(start=1000.0, end=2000.0, time=0.5e-3),
    dict(start=1000.0, end=2000.0, time=600.0), dict(start=1000.0, end=2000.0, time=1.0, mark=3000.0),
    dict(start=1000.0, end=2000.0, time=1.0, sweep_type='exp'), dict(start=1000.0, end=2000.0, time=1.0,
                                                                     direction='sideways'),
    dict(start=1000.0, end=2000.0, time=1.0, wave='noise'), dict(start=1000.0, end=1e6, time=1.0, wave='square',
                                                                 duty=30.0),
    dict(start=1000.0, end=2000.0, time=1.0, high=3.2), dict(start=1000.0, end=2000.0, time=float('inf')),
]


@pytest.mark.parametrize('kw', BAD_SWEEP, ids=[str(i) for i in range(len(BAD_SWEEP))])
def test_program_sweep_rejects(kw):
    with pytest.raises(DscopeError) as e:
        M.program_sweep(**kw)
    assert e.value.code == 'INVALID_ARG'


BAD_BURST = [
    dict(wave='noise'), dict(cycles=0), dict(cycles=101, freq=10e3, period=0.1), dict(cycles=75, freq=5.0, period=15.0),
    dict(cycles=3.0), dict(counter=0), dict(counter=33), dict(freq=10e3, cycles=3, period=300e-6),
    dict(period=16.0), dict(period=0.0), dict(trig_delay=5.0), dict(trig_delay=-1e-6), dict(burst_mode='x'),
    dict(trig_source='x'), dict(trig_edge='x'), dict(trig_out='x'), dict(gate_polarity='x'), dict(freq=2e6),
    dict(high=0.0, low=0.0),
]


@pytest.mark.parametrize('kw', BAD_BURST, ids=[str(i) for i in range(len(BAD_BURST))])
def test_program_burst_rejects(kw):
    with pytest.raises(DscopeError) as e:
        M.program_burst(**kw)
    assert e.value.code == 'INVALID_ARG'


def test_limits_are_inclusive():
    M.program_modulation('am', high=2.5, low=0.5, depth=M.am_depth_max(2.5, 0.5))
    M.program_modulation('am', depth=100.0)
    M.program_modulation('fm', deviation=10e3, mod_freq=10e3)
    M.program_modulation('fsk', hop=1e6)
    M.program_modulation('fsk', wave='triangle', freq=400e3, hop=400e3)
    M.program_sweep(0.1, 1e6, 1e-3)
    M.program_sweep(1e6, 0.1, 500.0, 'log', 'down')
    M.program_sweep(1000.0, 2000.0, 1.0, mark=1000.0)
    M.program_burst(freq=10e3, cycles=3, period=350e-6)
    M.program_burst(freq=10e3, cycles=100, period=15.0, counter=32, trig_delay=M.TRIG_DELAY_MAX)
    M.program_burst(freq=0.1, cycles=1, period=15.0)
    assert M.gui_limits('burst', freq=10e3, cycles=3)['period_s'][0] == pytest.approx(350e-6)
    assert M.gui_limits('sweep', start=1000.0, end=2000.0)['start_hz'] == (0.1, 2000.0)


# ---- fixture extraction ----------------------------------------------------------------------------------------------
def _extract_main(argv):
    import argparse
    ap = argparse.ArgumentParser(description='Write tests/fixtures/awg_modes_programs.json from a hal_run session '
                                             'that recorded tests/fixtures/scenarios/corpus5.json.')
    ap.add_argument('--extract', required=True, metavar='SESSION', help='e.g. re/traces/S02_awg_modes')
    ap.add_argument('--out', default=FIXTURE_PATH)
    a = ap.parse_args(argv)
    session = a.extract if os.path.isabs(a.extract) else os.path.join(ROOT, a.extract)
    rec = load_session(session)
    if not rec:
        sys.exit('no corpus5 scenario results in %s/scen' % session)
    out = [rec[n] for n in NAMES if n in rec]
    with open(a.out, 'w', encoding='utf-8') as f:
        json.dump(out, f, indent=1)
    missing = [n for n in NAMES if n not in rec]
    print(json.dumps({'written': a.out.replace('\\', '/'), 'scenarios': len(out), 'missing': missing}))


if __name__ == '__main__':
    _extract_main(sys.argv[1:])
