"""Golden tests for dscope/mso.py against recorded DSWave 1.2.1 runs (no hardware).

The recorded runs live in re/traces/S01_warm/scen/*.json (git-ignored: they contain the device's
security key) and the calibration blob in state/cache/zero_info_from_trace.bin; the tests skip when
those are absent.  The runs are replayed in recording order (re/scenarios/corpus1..4.json, which is
also the file-mtime order), accumulating the SCPI state exactly like DSWave's HAL does (e.g.
:TRIGger:Enable 1 persists from corpus2 on, :REPEATModeEnable persists until modes_reset).

For every run we check:
* setup_program() == the recorded block from the first W 0x01=0 through W 0xF9=0 (order and values).
  DSWave re-sends the 4 x 257 ANAn_CROSS writes only when the enabled analog set changed since its
  previous run (mso+0x88 cache); that is modelled with include_cross = (mask != previous mask).
* rates_and_depths() == the recorded :ANALOG/DIGITAL:SAMPLE:Rate?/Depth? answers.
* geometry().frame_packet_samples == the HAL log's "MSO: send samples" values of the first frame.
* geometry().transfer_size / transfer_count == the recorded bulk-IN data length / number of
  transfers cancelled at stop.
"""
import json
from collections import Counter
from functools import lru_cache
from pathlib import Path

import pytest

from dscope import mso
from dscope.regs import name as reg_name

REPO = Path(__file__).resolve().parents[1]
SCEN_DIR = REPO / 're' / 'traces' / 'S01_warm' / 'scen'
ZERO_BIN = REPO / 'state' / 'cache' / 'zero_info_from_trace.bin'
CORPORA = [REPO / 're' / 'scenarios' / ('corpus%d.json' % i) for i in range(1, 5)]

QUERY_INDEX = {':ANALOG:SAMPLE:Rate?': 0, ':ANALOG:SAMPLE:Depth?': 1,
               ':DIGITAL:SAMPLE:Rate?': 2, ':DIGITAL:SAMPLE:Depth?': 3}


def _order():
    names = []
    for p in CORPORA:
        if p.exists():
            names += [s['name'] for s in json.loads(p.read_text())]
    return names


ORDER = _order()
HAVE_DATA = SCEN_DIR.is_dir() and ZERO_BIN.exists() and bool(ORDER)
pytestmark = pytest.mark.skipif(not HAVE_DATA, reason='recorded DSWave runs / zero info not available')


@lru_cache(maxsize=None)
def replay():
    """Replay all recorded runs; returns {name: run-dict} for runs that have a setup block."""
    cfg = mso.MsoConfig()
    prev_mask = 0               # DSWave starts with mso+0x88 = 0 (Mso_Init)
    runs = {}
    for name in ORDER:
        d = json.loads((SCEN_DIR / (name + '.json')).read_text())
        queries = []
        for item in d['scpi']:
            cmd, ret = item[0], item[1]
            if cmd.endswith('?'):
                if cmd in QUERY_INDEX and len(item) > 2:
                    queries.append((cmd, int(item[2], 16), mso.rates_and_depths(cfg)[QUERY_INDEX[cmd]]))
                continue
            if ret == 0:        # e.g. :ROLLModeEnable is rejected by the HAL (-8) and changes nothing
                cfg.apply_scpi(cmd)
        rw = d.get('run_writes') or []
        end = next((i for i, w in enumerate(rw) if w[2] == mso.FLUSH_DATA), None)
        if end is None:
            continue
        mask = mso.cross_mask(cfg)
        runs[name] = dict(
            cfg=cfg.copy(), include_cross=(mask != prev_mask),
            recorded=[(w[2], w[3]) for w in rw[:end + 1]],
            queries=queries,
            sends=[int(l.rsplit(':', 1)[1]) for l in d['log'] if 'MSO: send samples:' in l],
            data_lens=Counter(b['len'] for b in d.get('bulk_in', []) if b['pipe'] == 0x86 and b['len'] != 4096),
            cancelled=sum(1 for l in d['log'] if 'LIBUSB_TRANSFER_CANCELLED' in l),
        )
        prev_mask = mask
    return runs


@lru_cache(maxsize=None)
def cal():
    return mso.parse_zero_info(ZERO_BIN.read_bytes())


def _run(name):
    runs = replay()
    if name not in runs:
        pytest.skip('%s has no setup block (configuration-only scenario)' % name)
    return runs[name]


def _fmt(w):
    return '%02X %-22s %04X' % (w[0], reg_name(w[0]), w[1])


@pytest.mark.parametrize('name', ORDER)
def test_setup_program(name):
    r = _run(name)
    prog = mso.setup_program(r['cfg'], cal(), include_cross=r['include_cross'])
    rec = r['recorded']
    if prog != rec:
        i = next((k for k, (a, b) in enumerate(zip(prog, rec)) if a != b), min(len(prog), len(rec)))
        ctx = '\n'.join('  #%d rec %s | got %s' % (k, _fmt(rec[k]) if k < len(rec) else '-',
                                                    _fmt(prog[k]) if k < len(prog) else '-')
                        for k in range(max(0, i - 2), min(max(len(prog), len(rec)), i + 6)))
        pytest.fail('%s: %d computed vs %d recorded writes, first difference at #%d\n%s'
                    % (name, len(prog), len(rec), i, ctx))


@pytest.mark.parametrize('name', ORDER)
def test_rates_and_depths(name):
    r = _run(name)
    assert r['queries'], 'no rate/depth queries recorded'
    for cmd, expected, got in r['queries']:
        assert got == expected, '%s: %s -> %d, recorded %d' % (name, cmd, got, expected)


@pytest.mark.parametrize('name', ORDER)
def test_send_samples(name):
    r = _run(name)
    if not r['sends']:
        pytest.skip('no frame was received (normal trigger never fired)')
    frame = mso.geometry(r['cfg']).frame_packet_samples
    assert r['sends'][:len(frame)] == frame


@pytest.mark.parametrize('name', ORDER)
def test_transfer_size_and_count(name):
    r = _run(name)
    g = mso.geometry(r['cfg'])
    if r['data_lens']:
        assert g.transfer_size in r['data_lens'], (g.transfer_size, dict(r['data_lens']))
    assert g.transfer_count == r['cancelled']


def test_cross_cache_model_is_exercised():
    """The CROSS caching must both emit (channel set changed) and skip (unchanged) in the corpus."""
    flags = Counter(r['include_cross'] for r in replay().values())
    assert flags[True] >= 10 and flags[False] >= 100


def test_zero_info_parse():
    c = cal()
    assert [p.pwm_scale for p in c] == [481, 492, 485, 482]
    assert all(len(p.vga) == 12 and len(p.cross_a) == 4 for p in c)
    assert c[0].vga[9] == (0x10018, 0xCC)            # 1 V/div: gain 0x18, relay 1, pwm base 204
    assert c[0].adc_trim == -6
    assert c[0].cross_a == [0, -5, -16, 0] and c[0].cross_b == [0, 18, -8, 2]


def test_setup_program_defaults():
    """Default include_cross=True: the block always carries the 4 CROSS tables and ends at F9=0."""
    cfg = mso.MsoConfig()
    full = mso.setup_program(cfg, cal())
    lean = mso.setup_program(cfg, cal(), include_cross=False)
    assert full[0] == (0x01, 0) and full[-1] == (0xF9, 0) and full[-2][0] == 0x00
    assert len(full) - len(lean) == 4 * 257
    for ch in range(4):
        writes = [v for a, v in full if a == 0x44 + 0x10 * ch]
        assert len(writes) == 257 and writes[0] == 0xFFFF


def test_apply_scpi():
    cfg = mso.MsoConfig()
    for cmd in (':ANALOG:CHANNEL:Enable 0x2,0x0', ':ANALOG:CHANNEL:Coupling 1,1', ':DIGITAL:CHANNEL:Enable 7,0',
                ':TRIGger:Channel 0x0000000000000007,0x0000000000000000', ':SAMPLEtime 5000000000',
                ':STORAGEdepth 0x61A80', ':REPEATModeEnable 1', ':ROLLModeEnable 1', ':AWG:Enable 0,1'):
        cfg.apply_scpi(cmd)
    assert not cfg.analog[2].enable and cfg.analog[1].coupling == 'ac' and not cfg.digital[7]
    assert (cfg.trig_channel, cfg.trig_channel_type) == (7, 0)
    assert cfg.sample_time_ps == 5_000_000_000 and cfg.storage_depth == 400_000 and cfg.repeat_mode
