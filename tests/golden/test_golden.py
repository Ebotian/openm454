"""Table-driven golden cases for the AWG, the old repeat snapshot and the DSWave loop-mode setup block.

The new remote's repeat trace golden stays in test_repeat_golden.py; the old repeat snapshot and the new
repeat golden coexist under golden/. The recorded DSWave loop-mode block is the first setup-program golden that
runs without local traces (the mso/packet golden families still read their own recorded fixtures).
"""
import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from openm454 import awg, mso

FIX = json.loads((Path(__file__).resolve().parents[1] / 'fixtures' / 'awg_programs.json').read_text())


def _final(seq):
    d = {}
    for a, v in seq:
        d[a] = v
    return d


@pytest.mark.parametrize('case', FIX, ids=[c['name'] for c in FIX])
def test_awg_program_matches_dswave(case):
    a = case['awg']
    wave = ['sine', 'triangle', 'square', 'noise'][a['wave']]
    mine = awg.program(wave, a['freq'], a['high'], a['low'], a['duty'], a['phase'] / 1e6)
    rec = case['writes'][:-3]
    assert case['writes'][-3:] == [list(x) for x in awg.START]
    assert _final(mine[1:]) == _final(rec[1:])


def test_awg_rejects_out_of_range():
    for kw in (dict(high=3.5), dict(low=-0.1), dict(freq=2e6), dict(wave='triangle', freq=500e3)):
        args = dict(wave='sine', freq=1000, high=3.0, low=0.0)
        args.update(kw)
        with pytest.raises(Exception):
            awg.program(**args)


REPEAT_FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'dswave' / 'triggered_repeat_setup.json'
REPEAT_NORMALIZED_SHA256 = '925b8f21b53c02b9d06998793bc8c8e14167c6140d5d3d17ad1025e705bbf49b'


def test_the_repeat_recording_is_pinned():
    """The old real DSWave repeat-mode snapshot; the new remote trace golden is separate and coexists."""
    recorded = json.loads(REPEAT_FIXTURE.read_text())

    assert len(recorded['program']) == 75
    assert recorded['program'][0] == '01=0000'
    assert recorded['program'][-2:] == ['00=0001', 'F9=0000']
    assert '30=0003' in recorded['program']
    assert set(recorded['wildcards']) == {'43', '53', '63', '73'}

    normalized = []
    for entry in recorded['program']:
        addr = entry.partition('=')[0]
        normalized.append(addr + '=*' if addr in recorded['wildcards'] else entry)
    assert hashlib.sha256(json.dumps(normalized).encode()).hexdigest() == REPEAT_NORMALIZED_SHA256


# ------------------------------------------------------------------ DSWave loop mode (Recorder)
FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'dswave' / 'loop_mode_setup.json'


def _config(spec):
    cfg = mso.MsoConfig()
    fields = {f.name for f in dataclasses.fields(cfg)}
    for key, value in spec.items():
        assert key in fields, 'unknown MsoConfig field in the fixture: %s' % key
        if key == 'analog':
            value = [mso.AnalogChannel(**ch) for ch in value]
        elif key == 'digital':
            value = list(value)
        setattr(cfg, key, value)
    return cfg


@pytest.fixture(scope='module')
def recorded():
    return json.loads(FIXTURE.read_text())


def _parse(entry):
    addr, value = entry.split('=')
    return int(addr, 16), int(value, 16)


def _program(cfg):
    # vga=[] falls back to the recorded default gain table; pwm_scale is the recorded unit's, hence the wildcards
    return mso.setup_program(cfg, [mso.ProbeCal(0, 0, 0, [])] * 4, include_cross=False)


def test_the_block_is_the_expected_size(recorded):
    assert len(recorded['program']) == 61
    assert set(recorded['wildcards']) == {'43', '53', '63', '73'}
    # every recorded address is classed exactly once: wildcard, echo of a config input, derived, or constant
    classes = [set(recorded['wildcards']), set(recorded['constants'])]
    classes += [set(key.split()) for key in list(recorded['echoes']) + list(recorded['derived'])]
    named = [a for c in classes for a in c]
    assert len(named) == len(set(named)), 'an address is classed twice'
    assert set(named) == {'%02X' % _parse(e)[0] for e in recorded['program']}


def test_recorded_program_is_reproduced(recorded):
    wild = {int(a, 16) for a in recorded['wildcards']}
    ours = _program(_config(recorded['config']))
    expected = [_parse(e) for e in recorded['program']]

    assert len(ours) == len(expected), 'write count differs: %d vs %d recorded' % (len(ours), len(expected))
    for i, ((addr, value), (want_addr, want_value)) in enumerate(zip(ours, expected)):
        assert addr == want_addr, 'write %d: 0x%02X vs 0x%02X recorded' % (i, addr, want_addr)
        if addr in wild:
            continue                      # per-unit PWM calibration of the recorded unit, see the fixture
        assert value == want_value, 'write %d: W 0x%02X = 0x%04X, recorded 0x%04X' % (i, addr, value, want_value)


@pytest.mark.parametrize('sample_time_ps', [10 ** 9, 25 * 10 ** 10, 10 ** 14])
def test_the_window_does_not_enter_the_loop_program(recorded, sample_time_ps):
    """DSWave's Recorder ignores :SAMPLEtime (Mso_SetHoriTime returns at once in loop mode), so the fixture has no
    window: with explicit rates the program is the same for any (1 ms, 250 ms above the 200 ms roll limit, 100 s)."""
    cfg = _config(recorded['config'])
    base = _program(cfg)
    cfg.sample_time_ps = sample_time_ps
    assert _program(cfg) == base


def test_the_recording_is_loop_mode_with_the_trigger_off(recorded):
    """What the recorded writes say about the acquisition, and the geometry the config gives for it. Rates, trigger
    and depth are config inputs, so they are read back from the writes here; the rest is derived and pinned to
    numbers, so a change in the geometry constants shows up here."""
    cfg = _config(recorded['config'])
    g = mso.geometry(cfg)
    regs = dict(_parse(e) for e in recorded['program'])      # last value per address

    # the mode: loop sets bit 2 and forces bit 1 (roll is not consulted), repeat bit clear
    assert regs[mso.HW_BASE_MODE] == g.hw_mode == 6
    assert cfg.loop_mode and not cfg.repeat_mode
    # trigger off: TRIG_BASE bit 0 clear and no trigger-memory program after W 0x09=0 / W 0x3A
    assert not cfg.trig_enable and not regs[0x30] & 1
    assert mso.TRIG_DATA not in regs and mso.TRIG_SHFT not in regs
    # 1 MSa/s on both kinds: 4 analog + 8 digital at fold 0, divider 100 on both
    assert (regs[0x28], regs[0x20]) == (g.n_analog * 0x100 + g.afold_log2, g.n_digital * 0x100 + g.dfold_log2)
    assert (g.n_analog, g.n_digital, g.afold_log2, g.dfold_log2) == (4, 8, 0, 0)
    for lo, rate in ((0x15, g.analog_rate), (0x17, g.digital_rate)):
        div = regs[lo] * ((regs[lo + 1] >> 8) + 1)
        assert mso.ANALOG_BASE_RATE // div == rate == 1_000_000
    assert (regs[0x19] + 1, regs[0x1A] + 1) == (g.analog_mtxs, g.digital_mtxs) == (1, 1)
    # block, depth, read words, pre-trigger
    assert mso.BYTES_PER_FRAME - 4 * regs[0x1B] == g.blk_bytes == 320
    assert (regs[0x11] | regs[0x12] << 16) << 6 == g.hw_depth == 0xF5000
    assert ((regs[0x13] | regs[0x14] << 16) + 1) * 4 == g.rd_bytes == g.pkt_bytes == 100160   # one 20 ms packet
    assert regs[0x31] | regs[0x32] << 16 == g.pretrig == 0x7A140

    # what pins loop mode beyond the MODE word: outside it the same inputs give MODE 2 and a whole-record read
    flat = cfg.copy()
    flat.loop_mode = False
    gf = mso.geometry(flat)
    assert gf.hw_mode == 2 and gf.rd_bytes != g.rd_bytes
