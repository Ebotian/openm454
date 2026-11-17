"""The setup program DSWave 1.2.1 sends for its Recorder mode, byte for byte (no hardware).

The fixture is a recording of one acquisition made by DSWave itself (see tests/fixtures/dswave/loop_mode_setup.json:
usbmon on the host while DSWave drove the scope through a VM). It pins the half of the protocol that until now was
derived only from the disassembly: the order of the setup block and every register value in it, including the ones
that follow from the geometry (packet size, read bytes, trigger memory, pre-trigger, divider).

57 of the 61 writes are expected to match exactly without any per-unit data. The other four are the per-probe
VPWM word, which mixes the probe's PWM full-scale and the division's PWM offset, both from the EEPROM zero info:
with this unit's zero info cached (state/cache/zero_info_from_trace.bin, per unit, not in the repo) all 61 match
and the second test checks that. The shippable test compares the four by address only.

The 12 writes DSWave sends before W 0x01=0 (its probe-configuration pre-pass, SetAfold/ClearAwr per probe) are not
part of setup_program(); our program folds the same settings into the per-probe block, so the two sequences differ
by that pre-pass and nothing else.
"""
import hashlib
import json
from pathlib import Path

import pytest

from openm454 import mso

FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'dswave' / 'loop_mode_setup.json'
ZERO_BIN = Path(__file__).resolve().parents[1] / 'state' / 'cache' / 'zero_info_from_trace.bin'


def _config(spec):
    cfg = mso.MsoConfig()
    for i, ch in enumerate(spec['analog']):
        cfg.analog[i] = mso.AnalogChannel(**ch)
    cfg.digital = list(spec['digital'])
    cfg.threshold_uv = spec['threshold_uv']
    cfg.sample_time_ps = spec['sample_time_ps']
    cfg.storage_depth = spec['storage_depth']
    cfg.auto_rate = spec['auto_rate']
    cfg.trig_enable = spec['trig_enable']
    cfg.trig_mode = spec['trig_mode']
    cfg.trig_channel_type = spec['trig_channel_type']
    cfg.trig_channel = spec['trig_channel']
    cfg.trig_type = spec['trig_type']
    cfg.trig_pulse_ps = spec['trig_pulse_ps']
    cfg.trig_margin = spec['trig_margin']
    cfg.trig_hold_ps = spec['trig_hold_ps']
    cfg.trig_sample_index = spec['trig_sample_index']
    cfg.loop_mode = spec['loop_mode']
    return cfg


@pytest.fixture(scope='module')
def recorded():
    return json.loads(FIXTURE.read_text())


def _parse(entry):
    addr, value = entry.split('=')
    return int(addr, 16), int(value, 16)


def test_the_block_is_the_expected_size(recorded):
    assert len(recorded['program']) == 61
    assert set(recorded['wildcards']) == {'43', '53', '63', '73'}


def test_recorded_program_is_reproduced(recorded):
    cfg = _config(recorded['config'])
    wild = {int(a, 16) for a in recorded['wildcards']}
    # vga=[] falls back to the recorded default gain table; pwm_scale is the unit's, hence the wildcards
    cal = [mso.ProbeCal(0, 0, 0, [])] * 4
    ours = mso.setup_program(cfg, cal, include_cross=False)
    expected = [_parse(e) for e in recorded['program']]

    assert len(ours) == len(expected), 'write count differs: %d vs %d recorded' % (len(ours), len(expected))
    for i, ((addr, value), (want_addr, want_value)) in enumerate(zip(ours, expected)):
        assert addr == want_addr, 'write %d: 0x%02X vs 0x%02X recorded' % (i, addr, want_addr)
        if addr in wild:
            continue                      # per-unit PWM calibration, see the fixture
        assert value == want_value, 'write %d: W 0x%02X = 0x%04X, recorded 0x%04X' % (i, addr, value, want_value)


@pytest.mark.skipif(not ZERO_BIN.exists(),
                    reason="this unit's zero info is not cached (state/cache/zero_info_from_trace.bin)")
def test_every_write_matches_with_the_units_zero_info(recorded):
    """With the unit's EEPROM zero info the four VPWM words match too: 61 of 61, order and values."""
    cfg = _config(recorded['config'])
    ours = mso.setup_program(cfg, mso.load_zero_info(), include_cross=False)
    assert ours == [_parse(e) for e in recorded['program']]


def test_the_recording_is_loop_mode_with_the_trigger_off(recorded):
    """What the trace says about the acquisition, so a change in the geometry constants shows up here."""
    cfg = _config(recorded['config'])
    g = mso.geometry(cfg)
    assert g.hw_mode == 6                 # loop (4) + roll (2), repeat bit clear
    assert g.analog_rate == 1_000_000 and g.digital_rate == 1_000_000
    assert g.n_analog == 4 and g.n_digital == 8
    assert g.hw_depth == 0xF5000
    assert g.rd_bytes == 100160
    assert g.blk_bytes == 320
    assert (g.analog_mtxs, g.digital_mtxs) == (1, 1)
    assert g.pretrig == 0x7A140


REPEAT_FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'dswave' / 'triggered_repeat_setup.json'
REPEAT_NORMALIZED_SHA256 = '925b8f21b53c02b9d06998793bc8c8e14167c6140d5d3d17ad1025e705bbf49b'


def test_the_repeat_recording_is_pinned():
    """Pin the real DSWave repeat-mode recording, not a generator comparison.

    DSWave re-applies its repeat state at Run, and its repeat mode handler also changes
    auto rate/trigger/transfer (0x3f3/0x3e9/0x3ef/0x5e1/0x5e2), so a same-config baseline
    that differs only in W 0x00 was not available from the UI. What is pinned here is the
    real 75-write setup block DSWave 1.2.1 sent for repeat mode; the four per-probe VPWM
    words are compared by address only.
    """
    recorded = json.loads(REPEAT_FIXTURE.read_text())

    assert len(recorded['program']) == 75
    assert recorded['program'][0] == '01=0000'
    assert recorded['program'][-2:] == ['00=0001', 'F9=0000']
    assert '30=0003' in recorded['program']       # auto trigger + trigger enable
    assert set(recorded['wildcards']) == {'43', '53', '63', '73'}

    normalized = []
    for entry in recorded['program']:
        addr = entry.partition('=')[0]
        normalized.append(addr + '=*' if addr in recorded['wildcards'] else entry)
    assert hashlib.sha256(json.dumps(normalized).encode()).hexdigest() == REPEAT_NORMALIZED_SHA256
