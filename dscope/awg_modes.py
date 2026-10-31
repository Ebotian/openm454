"""AWG modulation, sweep and burst modes (DSWave 1.2.1 HAL replayed from static analysis; NOT yet recorded).

Status 2026-10-31: every formula below comes from the decompiled HAL (`re/decomp/hal/0x34db10_Awg_ScpiWrite.c`,
cited as "SW:L<line>", and the helpers named by RVA) cross-checked in the disassembly by the derivation notes
`re/notes/awg_mod.md`, `awg_sweep.md`, `awg_burst.md`. GUI limits come from DSWave.exe's tables: static ranges
`{id, min, max, step}` at RVA 0xBAB9E0, the parameter table with the SCPI paths at 0xBABBC0 and the dynamic ranges
in `awgparamprovider` FUN_140306300 ("GUI:case <id-7000>"). The basic-mode part of the same HAL replay reproduces
all 14 corpus3 recordings write for write. The mode registers themselves are confirmed by nothing yet: the
recording plan is `re/scenarios/corpus5.json` and `tests/test_awg_modes_golden.py` compares once it is recorded.

Confidence tags: [H] decompiled + disassembly checked, [M] code, but depends on GUI/HAL state or an inference,
[L] what the FPGA does with the value (only an A0 capture can tell). `TODO(recording)` marks every constant or
meaning that only the corpus5 recording or an A0 capture can settle. No formula depends on a device-object value
read at run time: the only object constant is the AMPtune gain 0.91525 (Awg ctor 0x34cbe0 default; the GUI never
sends AMPtune), already confirmed by the corpus3 HIGH code 468 at 3 V.

Public API (all return ordered (awg_addr, value) writes that configure but do not start the AWG; append
`awg.START` to run it, exactly like `awg.program`):
  program_modulation(kind, ...)   MODE 1, AM / FM / FSK (PM / ASK / PSK only with experimental=True)
  program_sweep(start, end, time, ...)  MODE 2, linear or log, up or down
  program_burst(...)              MODE 3, N-cycle or gated, internal / external / manual trigger
The carrier registers (WAVE, FCW, RPAR/FPAR, PHASE, HIGH/LOW, DUTY, RSLOPE/FSLOPE) are `dscope.awg.program()`'s
writes, placed where DSWave sends them, so they inherit awg.py's formulas (which differ from the HAL by 1 LSB in
some places: HIGH/LOW rounding, RSLOPE/FSLOPE, RPAR constant, fractional PHASE; see awg_burst.md section 7). The AM
levels are the one exception: AMdepth rewrites HIGH/LOW with the HAL's own formula (am_levels()).
Also: DswaveAwgHal (exact replay of Awg_ScpiWrite, used by the tests and the corpus predictions), the canonical
SCPI builders *_scpi(), and helpers am_levels(), edge_param(), slopes(), sweep_info(), burst_info(), gui_limits().

Edge times: DSWave 1.2.1 has no case for RISEtime / FALLtime (ids 53/54 fall to `default: return -8`, SW:L286;
the S01_warm trace shows ret -8) and never writes REDGE/FEDGE (0x12-0x15). So no rise/fall helper exists; the
only edge registers DSWave drives are RPAR/FPAR (edge_param()) and RSLOPE/FSLOPE (slopes()).
"""
import math

from . import awg
from .errors import DscopeError

# ---- AWG register addresses (Awg_AddrName 0x34cf70, jump table 0xc98d9c) ------------------------------------------
A_WAVE, A_MODE, A_SWITCH, A_ENABLE = 0x00, 0x01, 0x02, 0x03
A_FCW0, A_FCW1, A_PH0, A_PH1, A_HIGH, A_LOW = 0x04, 0x05, 0x06, 0x07, 0x08, 0x09
A_DUTY0, A_DUTY1, A_RSLOPE, A_FSLOPE = 0x0A, 0x0B, 0x0C, 0x0D
A_RPAR0, A_RPAR1, A_FPAR0, A_FPAR1 = 0x0E, 0x0F, 0x10, 0x11
A_REDGE0, A_REDGE1, A_FEDGE0, A_FEDGE1 = 0x12, 0x13, 0x14, 0x15          # never written by DSWave 1.2.1
A_MOD_TYPE, A_MOD_SRC, A_MOD_WAVE, A_MOD_FCW0, A_MOD_FCW1 = 0x1A, 0x1B, 0x1C, 0x1D, 0x1E
A_AM_STATE, A_AM_PARAM0, A_AM_PARAM1 = 0x1F, 0x20, 0x21
A_FM_FDEV0, A_FM_FDEV1, A_PM_PDEV0, A_PM_PDEV1 = 0x22, 0x23, 0x24, 0x25
A_FSK_FCW0, A_FSK_FCW1, A_PSK_POLA = 0x26, 0x27, 0x28
A_SWP_TYPE, A_SWP_DIR, A_SWP_1CYC, A_SWP_MARK = 0x3A, 0x3B, 0x3C, 0x3D   # SWP_MARK is never written by DSWave
A_SWP_SFCW0, A_SWP_SFCW1, A_SWP_SFCW2, A_SWP_EFCW0, A_SWP_EFCW1 = 0x3E, 0x3F, 0x40, 0x41, 0x42
A_SWP_STEP0, A_SWP_STEP1, A_SWP_LOGR0, A_SWP_LOGR1 = 0x43, 0x44, 0x45, 0x46
A_SWP_MFCW0, A_SWP_MFCW1, A_SWP_TINC0, A_SWP_TINC1 = 0x47, 0x48, 0x49, 0x4A
A_BST_MODE, A_BST_NMODE, A_BST_NNUM, A_BST_CNT, A_BST_POLA = 0x51, 0x52, 0x53, 0x54, 0x55
A_TRIG_SRC, A_TRIG_EDGE, A_TRIG_OUT = 0x60, 0x61, 0x62
A_TRIG_FCW0, A_TRIG_FCW1, A_TRIG_DELY0, A_TRIG_DELY1 = 0x63, 0x64, 0x65, 0x66

REG_NAMES = {
    0x00: 'WAVE', 0x01: 'MODE', 0x02: 'SWITCH', 0x03: 'ENABLE', 0x04: 'FCW0', 0x05: 'FCW1', 0x06: 'PHASE0',
    0x07: 'PHASE1', 0x08: 'HIGH', 0x09: 'LOW', 0x0A: 'DUTY0', 0x0B: 'DUTY1', 0x0C: 'RSLOPE', 0x0D: 'FSLOPE',
    0x0E: 'RPAR0', 0x0F: 'RPAR1', 0x10: 'FPAR0', 0x11: 'FPAR1', 0x12: 'REDGE0', 0x13: 'REDGE1', 0x14: 'FEDGE0',
    0x15: 'FEDGE1', 0x1A: 'MOD_TYPE', 0x1B: 'MOD_SRC', 0x1C: 'MOD_WAVE', 0x1D: 'MOD_FCW0', 0x1E: 'MOD_FCW1',
    0x1F: 'AM_STATE', 0x20: 'AM_PARAM0', 0x21: 'AM_PARAM1', 0x22: 'FM_FDEV0', 0x23: 'FM_FDEV1', 0x24: 'PM_PDEV0',
    0x25: 'PM_PDEV1', 0x26: 'FSK_FCW0', 0x27: 'FSK_FCW1', 0x28: 'PSK_POLA', 0x3A: 'SWP_TYPE', 0x3B: 'SWP_DIR',
    0x3C: 'SWP_1CYC', 0x3D: 'SWP_MARK', 0x3E: 'SWP_SFCW0', 0x3F: 'SWP_SFCW1', 0x40: 'SWP_SFCW2', 0x41: 'SWP_EFCW0',
    0x42: 'SWP_EFCW1', 0x43: 'SWP_STEP0', 0x44: 'SWP_STEP1', 0x45: 'SWP_LOGR0', 0x46: 'SWP_LOGR1',
    0x47: 'SWP_MFCW0', 0x48: 'SWP_MFCW1', 0x49: 'SWP_TINC0', 0x4A: 'SWP_TINC1', 0x51: 'BST_MODE',
    0x52: 'BST_NMODE', 0x53: 'BST_NNUM', 0x54: 'BST_CNT', 0x55: 'BST_POLA', 0x60: 'TRIG_SRC', 0x61: 'TRIG_EDGE',
    0x62: 'TRIG_OUT', 0x63: 'TRIG_FCW0', 0x64: 'TRIG_FCW1', 0x65: 'TRIG_DELY0', 0x66: 'TRIG_DELY1'}

#: carrier registers (written by dscope.awg.program); everything else is mode specific
CARRIER_REGS = frozenset(range(0x00, 0x12))

# ---- enums (GUI enum tables in .rdata; HAL semantics in brackets) ---------------------------------------------------
MODE_BASIC, MODE_MODULATION, MODE_SWEEP, MODE_BURST = 0, 1, 2, 3     # Channel Mode 0xc3abe0 [H]
CARRIER_WAVES = ('sine', 'triangle', 'square')                     # wave list for mod/sweep/burst 0xc3ab40: no noise
MOD_KINDS = {'am': 0, 'fm': 1, 'pm': 2, 'fsk': 3, 'ask': 4, 'psk': 5}   # HAL/preview switch; GUI enum 0xc3ab00
GUI_MOD_KINDS = ('am', 'fm', 'fsk')                                # = {0 AM, 1 FM, 3 FSK} [H]
MOD_SOURCES = {'internal': 0, 'external': 1}                       # 0xc3aac0 (2 = "Other"); GUI keeps Internal [M]
MOD_WAVES = {'sine': 0, 'triangle': 1, 'square': 2}                # 0xc3aa60 [M: same table as the carrier list]
SWEEP_TYPES = {'linear': 0, 'log': 1}                              # Sweep Mode enum [H]
SWEEP_DIRS = {'up': 0, 'down': 1}                                  # HAL stores (value == 1) at +0x5c [H]
BURST_MODES = {'ncycle': 0, 'gated': 1}                            # 0xc3a960 [H]
NCYCLE_LIMITED = 0                                                 # 0xc3a930 has this one entry ("Limited") [H]
GATE_POLARITIES = {'positive': 0, 'inverted': 1}                   # 0xc3a900 [H]
TRIG_SOURCES = {'internal': 0, 'external': 1, 'manual': 2}         # 0xc3a8a0 [H]
TRIG_EDGES = {'rising': 0, 'falling': 1}                           # 0xc3a840 [H]
TRIG_OUTS = {'closed': 0, 'rising': 1, 'falling': 2}               # 0xc3a800 [H]

# ---- constants ----------------------------------------------------------------------------------------------------
K_AMP = 0.91525            # Awg ctor 0x34cbe0 default (.rdata 0x140c994c8) [H]; confirmed by corpus3 (HIGH 468 at 3 V)
FCW_PER_HZ = 40.0          # carrier/MOD/FSK/sweep frequency words: trunc(f * 40) (.rdata 0x140c99540) [H]
ACC32_PER_HZ = 42.94967296  # 2^32 / 1e8: TRIG_FCW (BURSTperiod) and SWP_MFCW (mark) scale [H]
EDGE_K = 3846153.0         # Awg_UpdateEdgeTime 0x34d2d0 line 16 (.rdata 0xc994f0) [H]
TS_DEFAULT_PS, TS_QUANTUM_PS = 100_000_000, 20_000   # Awg_UpdateSweepTime l.22 / magic divide by 20000 [H]
F_MIN = 0.1                # GUI:case 0x4 / 0x17 / 0x1c / 0x34 / 0x35: 0.1 Hz (1e5 uHz) .. 1 MHz, triangle 400 kHz
F_MAX = {'sine': 1e6, 'square': 1e6, 'triangle': 400e3}
V_MAX = 3.0                # GUI static table ids 0x1b5e/0x1b5f: 0 .. 3,000,000 uV
CODE_3V = 468              # HIGH code DSWave writes for 3.0 V (trunc(0.5 * 0.91525 * 1023)) [H, recorded]
AM_DEPTH_GUI_MAX = 120.0   # GUI:case 0x19 [H]
SWEEP_TIME_RANGE = (1e-3, 500.0)        # GUI static table id 0x1b8e (RVA 0xBABAF8): 1e9 .. 5e14 ps, step 1e9 ps [H]
BURST_PERIOD_MAX = 15.0                 # GUI:case 0x5d max 1.5e13 ps [H]
BURST_COUNTER_RANGE = (1, 32)           # static table id 0x1ba1 [H]
BURST_CYCLES_MAX = 100                  # static table id 0x1ba0 / GUI:case 0x48 [H]
TRIG_DELAY_MAX = (2 ** 32 - 1) * 1e-9   # TRIG_DELY is a 32-bit ns count (SW:L989-1016); the GUI's 10 s wraps [H]
SYMMETRY_RANGE = (10.0, 90.0)           # triangle "Symmetry" (-> DUTY), static table id 0x1b61 [H]
PM_DEV_MAX = 360.0                      # static table id 0x1b73 [H]

#: SCPI path -> (HAL command id, DswaveAwgHal method). Paths are the GUI parameter table strings (RVA 0xBABBC0);
#: ids are the Awg_ScpiWrite switch cases (recorded 44-52; the rest from the token order + what each case writes).
SCPI = {
    ':AWG:Enable': (44, 'enable'), ':AWG:Mode': (45, 'set_mode'), ':AWG:WAVE': (46, 'wave'),
    ':AWG:FREQUENCY': (47, 'frequency'), ':AWG:PHASE': (48, 'phase'), ':AWG:AMPtune': (49, 'amptune'),
    ':AWG:HIGHlevel': (50, 'high_level'), ':AWG:LOWlevel': (51, 'low_level'), ':AWG:DUTY': (52, 'duty'),
    ':AWG:RISEtime': (53, None), ':AWG:FALLtime': (54, None),
    ':AWG:MODULATIONmode': (55, 'modulation_mode'), ':AWG:MODULATIONsource': (56, 'modulation_source'),
    ':AWG:MODULATIONwave': (57, 'modulation_wave'), ':AWG:MODULATIONfrequency': (58, 'modulation_frequency'),
    ':AWG:AMdepth': (59, 'am_depth'), ':AWG:FMfrequency': (60, 'fm_deviation'), ':AWG:PMphase': (61, 'pm_phase'),
    ':AWG:FSKfrequency': (62, 'fsk_frequency'), ':AWG:PSKpolarity': (63, 'psk_polarity'),
    ':AWG:SWEEPmode': (64, 'sweep_mode'), ':AWG:SWEEPdirection': (65, 'sweep_direction'),
    ':AWG:SWEEPstartFrequency': (66, 'sweep_start'), ':AWG:SWEEPendFrequency': (67, 'sweep_end'),
    ':AWG:SWEEPtime': (68, 'sweep_time'), ':AWG:SWEEPfrequencyMark': (69, 'sweep_mark'),
    ':AWG:BURSTmode': (70, 'burst_mode'), ':AWG:BURSTncycleMode': (71, 'burst_ncycle_mode'),
    ':AWG:BURSTcyclesNum': (72, 'burst_cycles'), ':AWG:BURSTcounter': (73, 'burst_counter'),
    ':AWG:BURSTgatePolarity': (74, 'burst_gate_polarity'), ':AWG:BURSTperiod': (75, 'burst_period'),
    ':AWG:TRIGGERsource': (76, 'trigger_source'), ':AWG:TRIGGERedge': (77, 'trigger_edge'),
    ':AWG:TRIGGERoutput': (78, 'trigger_output'), ':AWG:TRIGGERdelay': (79, 'trigger_delay'),
}
HAL_UNSUPPORTED = -8       # Awg_ScpiWrite default: return 0xfffffff8 (SW:L286-287)


class DswaveAssert(Exception):
    """DSWave would hit a _wassert / CPU fault here and abort (never inject such a command into DSWave)."""


# ---- compiled conversions (x64 SSE2 semantics) ----------------------------------------------------------------------
_U64 = 0xFFFFFFFFFFFFFFFF
_2_63 = 2.0 ** 63


def _cvtt64(x):
    """CVTTSD2SI r64: truncate toward zero; NaN / out of range -> 0x8000000000000000 (integer indefinite)."""
    if x != x or x >= _2_63 or x < -_2_63:
        return 0x8000000000000000
    return int(x) & _U64


def _cvtt32(x):
    """CVTTSD2SI r32 (C `(int)double`), returned as an unsigned 32-bit pattern."""
    if x != x or x >= 2.0 ** 31 or x < -2.0 ** 31:
        return 0x80000000
    return int(x) & 0xFFFFFFFF


def _d2u64(x):
    """Compiled `(unsigned long long)double`: x >= 2^63 goes through subtract + btc (inf -> 0); negatives wrap."""
    return _cvtt64(x - _2_63) ^ 0x8000000000000000 if x >= _2_63 else _cvtt64(x)


def _div(a, b):
    """IEEE double division (inf / NaN instead of ZeroDivisionError)."""
    if b == 0.0:
        if a == 0.0 or a != a:
            return math.nan
        return math.copysign(math.inf, a) * math.copysign(1.0, b)
    return a / b


def _ln(x):
    """mingw log 0x59d5a0 (x87 FLDLN2/FYL2X). Python's log may differ by 1 ulp: TODO(recording) settles it (it only
    matters when T*ln(r)/ln(ratio) lands within ~1e-6 of a TINC boundary)."""
    if x != x or x < 0.0:
        return math.nan
    if x == 0.0:
        return -math.inf
    if x == math.inf:
        return math.inf
    return math.log(x)


def _round(x):
    """mingw round() 0x59aa20: half away from zero."""
    if x != x or abs(x) >= 2.0 ** 52:
        return x
    return math.copysign(math.floor(abs(x) + 0.5), x)


def _lo_hi(addr0, v):
    return [(addr0, v & 0xFFFF), (addr0 + 1, (v >> 16) & 0xFFFF)]


def micro(x):
    """User value -> the u64 'micro' unit DSWave's SCPI carries (uHz, uV, u%, udeg): round(x * 1e6)."""
    return int(round(x * 1e6))


def pico(seconds):
    """Seconds -> the u64 picosecond unit of SWEEPtime / BURSTperiod / TRIGGERdelay / RISE/FALLtime [H]."""
    return int(round(seconds * 1e12))


# ---- exact HAL formulas (each also used by DswaveAwgHal) ------------------------------------------------------------
def fcw40(f_hz):
    """MOD_FCW / FSK_FCW / SWP_SFCW / SWP_EFCW: trunc((uHz / 1e6) * 40) [H] (SW:L346, L510, L632, L669)."""
    return _d2u64((micro(f_hz) / 1000000.0) * 40.0)


def fm_fdev(dev_hz):
    """FM_FDEV = trunc((uHz / 1e6) * 40 * 0.5) = trunc(dev * 20) [H] (SW:L442). Whether the FPGA swings +-FDEV or
    +-2*FDEV per Hz of deviation is [L]: TODO(recording) scenario mod_fm_dev1k_sq on A0."""
    return _d2u64((micro(dev_hz) / 1000000.0) * 40.0 * 0.5)


def pm_pdev(dev_deg):
    """PM_PDEV = trunc(((udeg div 1e6) / 360) * 40 * 0.5) = trunc(int_deg / 18) -> 0..20 [H] (SW:L469, integer
    division first). Almost certainly an unfinished DSWave path (the FM scale copied); effect [L]."""
    return _d2u64(((micro(dev_deg) // 1000000) / 360.0) * 40.0 * 0.5)


def mark_fcw(f_hz):
    """SWP_MFCW = trunc((uHz / 1e6) * 42.94967296) [H] (SW:L792): the 2^32/1e8 scale, not the x40 FCW scale.
    DSWave never writes SWP_MARK (0x3d), so whether the mark does anything is [L]: TODO(recording)."""
    return _d2u64((micro(f_hz) / 1000000.0) * ACC32_PER_HZ)


def trig_fcw(period_s):
    """TRIG_FCW = trunc((1e12 / P_ps) * 42.94967296) [H] (SW:L919). P_ps == 0 asserts in DSWave (SW:L719)."""
    ps = pico(period_s)
    if ps <= 0:
        raise DswaveAssert('period != 0 (awg.cpp:0x1cd)')
    return _d2u64((1000000000000.0 / float(ps)) * ACC32_PER_HZ) & 0xFFFFFFFF


def trig_delay_ns(delay_s):
    """TRIG_DELY = ((P_ps >> 3) // 125) = floor(P_ps / 1000) ns, written lo/hi & 0xffff (SW:L990-993) [H]."""
    return (pico(delay_s) >> 3) // 125


def edge_param(f_hz):
    """RPAR = FPAR = (u64)(3846153.0 / f + 0.5) (Awg_UpdateEdgeTime 0x34d2d0 l.16) [H]; mode 2 forces f = 1 MHz -> 4
    (SW:L57-58). The physical meaning (edge interpolation length?) is [L]."""
    return _d2u64(_div(EDGE_K, micro(f_hz) / 1000000.0) + 0.5)


def slopes(duty_pct):
    """(DUTY32, RSLOPE, FSLOPE) exactly as the HAL writes them (SW:L236-247) [H]:
    DUTY32 = (u64)((u% / 1e6 / 100) * 4e9); RSLOPE = (int)(65472 / ((DUTY32 >> 22) & 0x3ff)) & 0xffff;
    FSLOPE = (int)(65472 / ((4e9 - DUTY32) >> 22)) & 0xffff (a zero divisor gives inf -> 0x80000000 -> 0)."""
    u = _d2u64(((micro(duty_pct) / 1000000.0) / 100.0) * 4000000000.0)
    rs = _cvtt32(_div(65472.0, float((u >> 22) & 0x3FF))) & 0xFFFF
    fs = _cvtt32(_div(65472.0, float(((4000000000 - (u & 0xFFFFFFFF)) & _U64) >> 22))) & 0xFFFF
    return u, rs, fs


def am_state_param(depth_pct, mod_type=0):
    """AMdepth (SW:L372-390) -> (AM_STATE, AM_PARAM32) [H]:
    type 6: (0x803, 0x10000); else i2 = (int)((2 - D/100) * 512 + 512) = trunc(512 * (3 - m));
    D > 100: (4*i2 + 2, 0x2000000 // i2) (u64 DIV; i2 == 0 near D = 300 % faults); else (4*i2, trunc(D*2^25/100/1023)).
    Field meaning (gain 512*(3-m), over-modulation bit 1, AM_PARAM ~ m in Q15) is [L]: TODO(recording) + A0."""
    d = micro(depth_pct) / 1000000.0
    if mod_type == 6:
        return 0x803, 0x10000
    i2 = _cvtt32((2.0 - d / 100.0) * 512.0 + 512.0)
    i2s = i2 - (1 << 32) if i2 & 0x80000000 else i2
    if 100.0 < d:
        if i2s == 0:
            raise DswaveAssert('AMdepth ~300 %: u64 DIV by zero in Awg_ScpiWrite (DSWave crashes)')
        return ((i2 * 4) + 2) & 0xFFFF, (0x2000000 // (i2s & _U64)) & 0xFFFFFFFF
    return (i2 * 4) & 0xFFFF, _cvtt64(((d * 33554432.0) / 100.0) / 1023.0) & 0xFFFFFFFF


def am_levels(high_v, low_v, depth_pct):
    """HIGH/LOW codes the HAL writes in AM (mode 1, MOD_TYPE 0) (Awg_UpdateAmplitude 0x34d9a0 / SW:L200-216) [H]:
    h = uV/6e6 * k, d = (h - l) * 0.5 * (100 - D) / 100 * 0.5, HIGH = trunc(min(1, h - d) * 1023),
    LOW = trunc(max(0, l + d) * 1023): the programmed swing becomes (H - L)(1 + m)/2 around the same midpoint.
    How the FPGA turns these into an envelope is [L]: TODO(recording) A0 envelope for m = 0 .. 1.2."""
    hal = DswaveAwgHal(mode=MODE_MODULATION, mod_type=0, depth=micro(depth_pct) / 1000000.0,
                       high=micro(high_v) / 6000000.0, low=micro(low_v) / 6000000.0)
    return dict(hal.update_amplitude())


def level_code(volts):
    """HIGH/LOW code DSWave writes outside AM: trunc(uV/6e6 * 0.91525 * 1023) [H] (awg.py rounds V*156)."""
    return _cvtt32(((micro(volts) / 6000000.0) * K_AMP) * 1023.0) & 0xFFFF


def am_depth_max(high_v, low_v):
    """GUI AM depth limit (GUI:case 0x19): min(120, 100 + 400 * min(L, 3 V - H) / (H - L)) %, 120 when H == L [H].
    It keeps the widened levels (m > 1) inside 0..3 V."""
    hi, lo = float(micro(high_v)), float(micro(low_v))
    if hi == lo:
        return AM_DEPTH_GUI_MAX
    head = min(lo, 3000000.0 - hi)
    return min(AM_DEPTH_GUI_MAX, (head * 400.0) / (hi - lo) + 100.0)


def burst_cycles_max(freq_hz):
    """GUI:case 0x48: N-cycle max = min(100, max(1, floor(15 * f - 0.5))) [H]."""
    n = math.floor(15.0 / (1.0 / (micro(freq_hz) / 1000000.0)) - 0.5)
    return int(min(BURST_CYCLES_MAX, max(1, n)))


def gui_limits(mode, wave='sine', freq=1000.0, high=3.0, low=0.0, kind='am', cycles=1, start=None, end=None,
               direction='up'):
    """DSWave GUI ranges for one mode (for display / validation). Units: Hz, %, s, degrees."""
    fmax = F_MAX.get(wave, 1e6)
    if mode == 'modulation':
        lim = {'carrier_hz': (F_MIN, fmax), 'mod_freq_hz': (F_MIN, freq if kind == 'fm' else 1e6)}
        if kind == 'am':
            lim['depth_pct'] = (0.0, am_depth_max(high, low))
        elif kind == 'fm':
            lim['deviation_hz'] = (0.0, freq)
        elif kind == 'fsk':
            lim['hop_hz'] = (F_MIN, fmax)
        elif kind == 'pm':
            lim['phase_dev_deg'] = (0.0, PM_DEV_MAX)
        return lim
    if mode == 'sweep':
        lim = {'time_s': SWEEP_TIME_RANGE}
        if end is not None:
            lim['start_hz'] = (F_MIN, end) if direction == 'up' else (end, fmax)     # GUI:case 0x34
        if start is not None:
            lim['end_hz'] = (start, fmax) if direction == 'up' else (F_MIN, start)   # GUI:case 0x35
        if start is not None and end is not None:
            lim['mark_hz'] = (min(start, end), max(start, end))                     # GUI:case 0x37
        return lim
    if mode == 'burst':
        return {'carrier_hz': (F_MIN, fmax), 'cycles': (1, burst_cycles_max(freq)), 'counter': BURST_COUNTER_RANGE,
                'period_s': ((cycles + 0.5) / freq, BURST_PERIOD_MAX), 'trig_delay_s': (0.0, TRIG_DELAY_MAX)}
    raise DscopeError('INVALID_ARG', 'mode must be modulation, sweep or burst')


# ---- the HAL replay -------------------------------------------------------------------------------------------------
class DswaveAwgHal:
    """Exact replay of DSWave 1.2.1's Awg_ScpiWrite (RVA 0x34db10) and its helpers.

    Each command method returns the ordered (awg_addr, value) writes (W 0x08 = addr, W 0x07 = value) and updates the
    same state as the HAL's Awg object (ctor 0x34cbe0), which persists across commands and across hal_run scenarios:
      +0x08 running  +0x09 enabled  +0x0c mode  +0x10 freq  +0x30 k  +0x38 high (uV/6e6)  +0x40 low  +0x48 mod_type
      +0x50 depth (%)  +0x58 swp_type  +0x5c swp_dir  +0x60 swp_start (FCW)  +0x68 swp_end  +0x70 swp_time (ps)
      +0x78 ratio_pos
    `log` collects the HAL's "Checking time_step" debug lines as (kind, ts_ps, value); `ids` the handled command ids.
    Commands that would make DSWave assert or fault raise DswaveAssert.
    """
    STATE = dict(running=0, enabled=0, mode=0, freq=0.0, k=K_AMP, high=0.0, low=0.0, mod_type=0, depth=0.0,
                 swp_type=0, swp_dir=0, swp_start=0, swp_end=0, swp_time=0, ratio_pos=0)

    def __init__(self, **state):
        for key, val in self.STATE.items():
            setattr(self, key, val)
        for key, val in state.items():
            if key not in self.STATE:
                raise TypeError('unknown HAL state field %r' % key)
            setattr(self, key, val)
        self.log, self.ids = [], []

    def state(self):
        return {key: getattr(self, key) for key in self.STATE}

    def copy(self):
        return DswaveAwgHal(**self.state())

    # -- basic family (ids 44-52); verified write for write on corpus3 --
    def enable(self, idx, v):                                    # id 44, SW:L27-49
        if not 0 <= idx < 15:
            raise DswaveAssert('index >= 0 && index < 15')
        self.enabled = 1 if v else 0
        return [(A_ENABLE, (self.running + (self.enabled << ((idx + 1) & 0x1F))) & 0xFFFF)]

    def set_mode(self, idx, v):                                  # id 45, SW:L50-96
        self.mode = v & 0xFFFFFFFF
        w = [(A_SWITCH, 1), (A_MODE, v & 0xFFFF)]
        if self.mode == MODE_SWEEP:                              # UpdateEdgeTime(1e6) -> RPAR/FPAR 4
            w += self._edge(1000000.0)
        return w + [(A_SWITCH, 0)]

    def wave(self, idx, v):                                      # id 46, SW:L97-123
        return [(A_SWITCH, 1), (A_WAVE, v & 0xFFFF), (A_SWITCH, 0)]

    def _edge(self, f):                                          # Awg_UpdateEdgeTime 0x34d2d0
        r = _d2u64(_div(EDGE_K, f) + 0.5)
        return _lo_hi(A_RPAR0, r) + _lo_hi(A_FPAR0, r)

    def frequency(self, idx, uhz):                               # id 47, SW:L124-163
        self.freq = uhz / 1000000.0
        fcw = _cvtt64(self.freq * 40.0) & 0xFFFFFFFF             # (int)(longlong)(f * 40)
        w = [(A_SWITCH, 1)] + _lo_hi(A_FCW0, fcw)
        if self.mode != MODE_SWEEP:
            w += self._edge(self.freq)
        return w + [(A_SWITCH, 0)]

    def phase(self, idx, udeg):                                  # id 48, SW:L164-191: whole degrees only
        return _lo_hi(A_PH0, _d2u64(((udeg // 1000000) / 360.0) * 4000000000.0))

    def amptune(self, idx, v):                                   # id 49, SW:L192-194 (never sent by the GUI)
        self.k = (v * 0.91525) / 1000000.0
        return self.update_amplitude()

    def high_level(self, idx, uv):                               # id 50, SW:L195-197
        self.high = uv / 6000000.0
        return self.update_amplitude()

    def low_level(self, idx, uv):                                # id 51, SW:L198-199
        self.low = uv / 6000000.0
        return self.update_amplitude()

    def update_amplitude(self):                                  # Awg_UpdateAmplitude 0x34d9a0 (SW:L200-216)
        h = self.high * self.k
        lo = self.k * self.low
        d = (((h - lo) * 0.5 * (100.0 - self.depth)) / 100.0) * 0.5
        hd = h - d
        cap = hd if hd <= 1.0 else 1.0                           # MINSD 1.0
        if self.mode == MODE_MODULATION and self.mod_type == 0:  # AM only
            lo = d + lo
            h = cap
            if lo < 0.0:
                lo = 0.0
        return [(A_HIGH, _cvtt32(h * 1023.0) & 0xFFFF), (A_LOW, _cvtt32(lo * 1023.0) & 0xFFFF)]

    def duty(self, idx, upct):                                   # id 52, SW:L235-285
        u = _d2u64(((upct / 1000000.0) / 100.0) * 4000000000.0)
        rs = _cvtt32(_div(65472.0, float((u >> 22) & 0x3FF))) & 0xFFFF
        fs = _cvtt32(_div(65472.0, float(((4000000000 - (u & 0xFFFFFFFF)) & _U64) >> 22))) & 0xFFFF
        return _lo_hi(A_DUTY0, u) + [(A_RSLOPE, rs), (A_FSLOPE, fs)]

    def start(self):                                             # Awg_Start 0x34fc30 (:Work:RUN 8,0)
        self.running = 1
        return [(A_SWITCH, 1), (A_ENABLE, (self.enabled * 2 + 1) & 0xFFFF), (A_SWITCH, 0)]

    def stop(self):                                              # 0x34fd00 (:Work:STOP 8,0), no SWITCH
        self.running = 0
        return [(A_ENABLE, (self.enabled * 2) & 0xFFFF)]

    # -- modulation family (ids 55-63) [H] --
    def modulation_mode(self, idx, v):                           # id 55, SW:L288-303: no SWITCH, no level update
        self.mod_type = v & 0xFFFFFFFF
        return [(A_MOD_TYPE, v & 0xFFFF)]

    def modulation_source(self, idx, v):                         # id 56, SW:L304-317
        return [(A_MOD_SRC, v & 0xFFFF)]

    def modulation_wave(self, idx, v):                           # id 57, SW:L318-331
        return [(A_MOD_WAVE, v & 0xFFFF)]

    def modulation_frequency(self, idx, uhz):                    # id 58, SW:L332-371
        return [(A_SWITCH, 1)] + _lo_hi(A_MOD_FCW0, _d2u64((uhz / 1000000.0) * 40.0)) + [(A_SWITCH, 0)]

    def am_depth(self, idx, upct):                               # id 59, SW:L372-427, then UpdateAmplitude
        self.depth = upct / 1000000.0
        if self.mod_type == 6:
            state, param = 0x803, 0x10000
        else:
            i2 = _cvtt32((2.0 - self.depth / 100.0) * 512.0 + 512.0)
            i2s = i2 - (1 << 32) if i2 & 0x80000000 else i2
            if 100.0 < self.depth:
                if i2s == 0:
                    raise DswaveAssert('AMdepth ~300 %: u64 DIV by zero (DSWave crashes)')
                state, param = ((i2 * 4) + 2) & 0xFFFF, (0x2000000 // (i2s & _U64)) & 0xFFFFFFFF
            else:
                state = (i2 * 4) & 0xFFFF
                param = _cvtt64(((self.depth * 33554432.0) / 100.0) / 1023.0) & 0xFFFFFFFF
        return [(A_AM_STATE, state), (A_AM_PARAM0, param & 0xFFFF), (A_AM_PARAM1, (param >> 16) & 0xFFFF)] \
            + self.update_amplitude()

    def fm_deviation(self, idx, uhz):                            # id 60, SW:L428-467
        return [(A_SWITCH, 1)] + _lo_hi(A_FM_FDEV0, _d2u64((uhz / 1000000.0) * 40.0 * 0.5)) + [(A_SWITCH, 0)]

    def pm_phase(self, idx, udeg):                               # id 61, SW:L468-495: no SWITCH
        return _lo_hi(A_PM_PDEV0, _d2u64(((udeg // 1000000) / 360.0) * 40.0 * 0.5))

    def fsk_frequency(self, idx, uhz):                           # id 62, SW:L496-535
        return [(A_SWITCH, 1)] + _lo_hi(A_FSK_FCW0, _d2u64((uhz / 1000000.0) * 40.0)) + [(A_SWITCH, 0)]

    def psk_polarity(self, idx, v):                              # id 63, SW:L536-549
        return [(A_PSK_POLA, 1 if v != 0 else 0)]

    # -- sweep family (ids 64-69) [H] --
    def sweep_mode(self, idx, v):                                # id 64, SW:L550-589
        self.swp_type = v & 0xFFFFFFFF
        return [(A_SWITCH, 1)] + self._update_sweep_time() + [(A_SWP_TYPE, v & 0xFFFF), (A_SWITCH, 0)]

    def sweep_direction(self, idx, v):                           # id 65, SW:L590-629
        self.swp_dir = 1 if v == 1 else 0
        return [(A_SWITCH, 1)] + self._update_sweep_time() + [(A_SWP_DIR, self.swp_dir), (A_SWITCH, 0)]

    def sweep_start(self, idx, uhz):                             # id 66, SW:L630-667 (no recompute)
        self.swp_start = _d2u64((uhz / 1000000.0) * 40.0)
        if not self.swp_start:
            raise DswaveAssert('m_sweep_start != 0 (awg.cpp:0x16d)')
        return [(A_SWITCH, 1)] + self._update_sweep_start() + [(A_SWITCH, 0)]

    def sweep_end(self, idx, uhz):                               # id 67, SW:L668-717 (no recompute)
        self.swp_end = _d2u64((uhz / 1000000.0) * 40.0)
        if not self.swp_end:
            raise DswaveAssert('m_sweep_end != 0 (awg.cpp:0x17b)')
        return [(A_SWITCH, 1)] + _lo_hi(A_SWP_EFCW0, self.swp_end) + [(A_SWITCH, 0)]

    def sweep_time(self, idx, ps):                               # id 68, SW:L750-777
        self.swp_time = ps
        return [(A_SWITCH, 1)] + self._update_sweep_time() + [(A_SWITCH, 0)]

    def sweep_mark(self, idx, uhz):                              # id 69, SW:L778-816
        return [(A_SWITCH, 1)] + _lo_hi(A_SWP_MFCW0, _d2u64((uhz / 1000000.0) * ACC32_PER_HZ)) + [(A_SWITCH, 0)]

    def _update_sweep_start(self):                               # Awg_UpdateSweepStart 0x34d3f0
        v = self.swp_start
        if self.swp_type == 1:
            v = (v << (self.ratio_pos & 63)) & _U64
        return [(A_SWP_SFCW0, v & 0xFFFF), (A_SWP_SFCW1, (v >> 16) & 0xFFFF), (A_SWP_SFCW2, (v >> 32) & 0xFFFF)]

    def _linear_step(self):                                      # Awg_UpdateSweepTime 0x34d4f0 linear branch
        ts = TS_DEFAULT_PS
        t_ps = self.swp_time
        diff = float(self.swp_end) - float(self.swp_start)
        d = abs(diff)
        q = _div(float(t_ps), d)
        if q > 100000000.0:                                      # slower than 1 FCW per 100 us: ceil to 20 ns
            c = q / 20000.0
            if abs(c) < 2.0 ** 52:
                t = float(int(c))
                c = t + (1.0 if t < c else 0.0)
            ts = _d2u64(c * 20000.0)                             # q = inf (start == end) -> 0
        step = 1
        if diff != 0.0:
            while t_ps != 0:
                step = _cvtt64(_round(_div(float(ts) * d, float(t_ps)))) & 0xFFFFFFFF
                self.log.append(('fcw_step_hw', ts, step))
                if step:
                    break
                ts += TS_QUANTUM_PS
        return step, ts

    def _calc_time_step(self):                                   # Awg_CalcTimeStep 0x34cc70 (log branch)
        if self.swp_start == self.swp_end:
            return 0, None
        t_ps = self.swp_time
        lq = _ln(_div(float(self.swp_end), float(self.swp_start)))
        for n in range(14, 0, -1):
            r = 1.0 / float(1 << n)
            r = (1.0 - r) if self.swp_dir else (r + 1.0)
            ts = _d2u64(float(t_ps) * _div(_ln(r), lq))
            self.log.append(('ratio_pos', ts, n))
            if ts > 100000000 and t_ps // ts <= 9999:
                return n, ts
        ts = _d2u64(float(t_ps) * _div(_ln(0.5 if self.swp_dir else 1.5), lq))
        self.log.append(('ratio_pos', ts, 1))
        if ts > 1000000:
            return 1, ts
        raise DswaveAssert('*time_step > HAL_u2pS(1) (awg.cpp:0x28e): sweep too fast for this ratio')

    def _update_sweep_time(self):                                # Awg_UpdateSweepTime 0x34d4f0
        w = []
        ts = TS_DEFAULT_PS
        if self.swp_type == 0:
            step, ts = self._linear_step()
            w += [(A_SWP_STEP0, step & 0xFFFF), (A_SWP_STEP1, step >> 16), (A_SWP_1CYC, int(step == 1))]
            tinc = (ts // TS_QUANTUM_PS) & 0xFFFFFFFF
        elif self.swp_type == 1:
            n, t2 = self._calc_time_step()
            if t2 is not None:
                ts = t2
            self.ratio_pos = n & 0xFFFFFFFF
            w += [(A_SWP_LOGR0, n & 0xFFFF), (A_SWP_LOGR1, (n >> 16) & 0xFFFF)]
            tinc = (ts // TS_QUANTUM_PS) & 0xFFFFFFFF
        else:
            tinc = 5000
        return w + _lo_hi(A_SWP_TINC0, tinc) + self._update_sweep_start()

    # -- burst / trigger family (ids 70-79) [H]; no HAL state --
    def burst_mode(self, idx, v):                                # id 70, SW:L834-847
        return [(A_BST_MODE, v & 0xFFFF)]

    def burst_ncycle_mode(self, idx, v):                         # id 71, SW:L848-861
        return [(A_BST_NMODE, v & 0xFFFF)]

    def burst_cycles(self, idx, v):                              # id 72, SW:L862-875
        return [(A_BST_NNUM, v & 0xFFFF)]

    def burst_counter(self, idx, v):                             # id 73, SW:L876-889
        return [(A_BST_CNT, v & 0xFFFF)]

    def burst_gate_polarity(self, idx, v):                       # id 74, SW:L890-903
        return [(A_BST_POLA, 1 if v != 0 else 0)]

    def burst_period(self, idx, ps):                             # id 75, SW:L904-946
        if ps == 0:
            raise DswaveAssert('period != 0 (awg.cpp:0x1cd)')
        t = _d2u64((1000000000000.0 / float(ps)) * ACC32_PER_HZ)
        return [(A_SWITCH, 1)] + _lo_hi(A_TRIG_FCW0, t) + [(A_SWITCH, 0)]

    def trigger_source(self, idx, v):                            # id 76, SW:L947-960
        return [(A_TRIG_SRC, v & 0xFFFF)]

    def trigger_edge(self, idx, v):                              # id 77, SW:L961-974
        return [(A_TRIG_EDGE, v & 0xFFFF)]

    def trigger_output(self, idx, v):                            # id 78, SW:L975-988
        return [(A_TRIG_OUT, v & 0xFFFF)]

    def trigger_delay(self, idx, ps):                            # id 79, SW:L989-1016
        x = ps >> 3
        return [(A_TRIG_DELY0, (x // 0x7D) & 0xFFFF), (A_TRIG_DELY1, (x // 0x7D0000) & 0xFFFF)]

    # -- SCPI front end --
    def command(self, path, idx, value):
        """One AWG SCPI command -> (ret, writes). ret is 0, or -8 for commands the HAL does not support."""
        hit = SCPI.get(path)
        if hit is None:
            raise KeyError('not an AWG SCPI path known to DSWave 1.2.1: %r' % path)
        cid, meth = hit
        if meth is None:                                         # RISEtime / FALLtime: no case -> -8
            return HAL_UNSUPPORTED, []
        writes = getattr(self, meth)(idx, value)
        self.ids.append(cid)
        return 0, writes

    def scpi(self, line):
        """hal_run shorthand ':AWG:X idx,val' (decimal or 0x) -> (ret, writes); ':Work:RUN/STOP 8,0' start/stop the
        AWG; other :Work: commands return (None, [])."""
        head, _, args = line.strip().partition(' ')
        vals = [int(a, 0) for a in args.split(',')] if args.strip() else []
        if head in (':Work:RUN', ':Work:STOP'):
            if vals and vals[0] == 8:
                return 0, (self.start() if head == ':Work:RUN' else self.stop())
            return None, []
        idx, val = (vals + [0, 0])[:2]
        return self.command(head, idx, val)


def replay(lines, hal=None):
    """Replay SCPI lines through a DswaveAwgHal -> dict(writes, per_cmd, rets, ids, log, per_cmd_log, hal)."""
    hal = hal if hal is not None else DswaveAwgHal()
    hal.log, hal.ids = [], []
    writes, per_cmd, rets, per_cmd_log = [], [], [], []
    for line in lines:
        n0 = len(hal.log)
        ret, w = hal.scpi(line)
        writes += w
        per_cmd.append(w)
        rets.append(ret)
        per_cmd_log.append(list(hal.log[n0:]))
    return {'writes': writes, 'per_cmd': per_cmd, 'rets': rets, 'ids': list(hal.ids), 'log': list(hal.log),
            'per_cmd_log': per_cmd_log, 'hal': hal}


def final(writes):
    """Last value per AWG address."""
    out = {}
    for a, v in writes:
        out[a] = v
    return out


# ---- validation -----------------------------------------------------------------------------------------------------
def _bad(msg):
    raise DscopeError('INVALID_ARG', msg)


def _num(name, x):
    if isinstance(x, bool) or not isinstance(x, (int, float)) or x != x or x in (math.inf, -math.inf):
        _bad('%s must be a finite number' % name)
    return x


def _choice(name, value, table):
    if value not in table:
        _bad('%s must be one of %s' % (name, ', '.join(table)))
    return table[value] if isinstance(table, dict) else value


def _check_carrier(wave, freq, high, low, duty, phase, pulse_freq=None):
    _choice('wave', wave, CARRIER_WAVES)
    for n, x in (('freq', freq), ('high', high), ('low', low), ('duty', duty), ('phase', phase)):
        _num(n, x)
    if wave == 'triangle' and not SYMMETRY_RANGE[0] <= duty <= SYMMETRY_RANGE[1]:
        _bad('triangle symmetry (duty) must be 10 .. 90 % (DSWave GUI)')
    if wave == 'square' and pulse_freq is not None and pulse_freq > freq:
        if min(duty, 100 - duty) / 100.0 / pulse_freq < awg.MIN_PULSE_S:
            _bad('square pulse narrower than 380 ns at %g Hz (the highest frequency of this mode)' % pulse_freq)


def _carrier_blocks(wave, freq, high, low, duty, phase):
    """dscope.awg.program() split into the blocks DSWave sends per SCPI command (validates the carrier)."""
    blocks = {k: [] for k in ('stop', 'mode', 'wave', 'freq', 'phase', 'levels', 'duty')}
    prog = awg.program(wave, freq, high, low, duty, phase)
    i = 0
    while i < len(prog):
        a, v = prog[i]
        if a == A_SWITCH and v == 1:
            j = i + 1
            while prog[j] != (A_SWITCH, 0):
                j += 1
            inner = prog[i + 1]
            key = {A_MODE: 'mode', A_WAVE: 'wave', A_FCW0: 'freq'}.get(inner[0])
            if key is None:
                raise AssertionError('unexpected SWITCH block in awg.program: %r' % (prog[i:j + 1],))
            blocks[key] += prog[i:j + 1]
            i = j + 1
            continue
        key = {A_ENABLE: 'stop', A_PH0: 'phase', A_PH1: 'phase', A_HIGH: 'levels', A_LOW: 'levels',
               A_DUTY0: 'duty', A_DUTY1: 'duty', A_RSLOPE: 'duty', A_FSLOPE: 'duty'}.get(a)
        if key is None:
            raise AssertionError('unexpected write in awg.program: %r' % ((a, v),))
        blocks[key].append((a, v))
        i += 1
    return blocks


def _mode_block(mode):
    """`:AWG:Mode 0,m` (SW:L50-96): SWITCH 1, MODE m, [mode 2: RPAR/FPAR = edge_param(1 MHz) = 4], SWITCH 0."""
    return DswaveAwgHal().set_mode(0, mode)


# ---- modulation ----------------------------------------------------------------------------------------------------
def modulation_scpi(kind='am', wave='sine', freq=10e3, high=3.0, low=0.0, duty=50.0, phase=0.0, mod_wave='sine',
                    mod_freq=100.0, depth=50.0, deviation=None, hop=None, phase_dev=None, psk_inverted=False,
                    source='internal'):
    """Canonical SCPI order for a modulation setup (re/notes/awg_mod.md section 7): Mode and MODULATIONmode before the
    levels, the type parameter (AMdepth) last, so the final state does not depend on earlier HAL state."""
    c = [':AWG:Enable 0,1', ':AWG:Mode 0,1', ':AWG:MODULATIONmode 0,%d' % MOD_KINDS[kind],
         ':AWG:WAVE 0,%d' % awg.WAVES[wave], ':AWG:FREQUENCY 0,%d' % micro(freq), ':AWG:PHASE 0,%d' % micro(phase),
         ':AWG:HIGHlevel 0,%d' % micro(high), ':AWG:LOWlevel 0,%d' % micro(low), ':AWG:DUTY 0,%d' % micro(duty),
         ':AWG:MODULATIONsource 0,%d' % MOD_SOURCES[source], ':AWG:MODULATIONwave 0,%d' % MOD_WAVES[mod_wave],
         ':AWG:MODULATIONfrequency 0,%d' % micro(mod_freq)]
    if kind == 'am':
        c.append(':AWG:AMdepth 0,%d' % micro(depth))
    elif kind == 'fm':
        c.append(':AWG:FMfrequency 0,%d' % micro(deviation))
    elif kind == 'pm':
        c.append(':AWG:PMphase 0,%d' % micro(phase_dev))
    elif kind == 'fsk':
        c.append(':AWG:FSKfrequency 0,%d' % micro(hop))
    elif kind == 'psk':
        c.append(':AWG:PSKpolarity 0,%d' % (1 if psk_inverted else 0))
    return c


def program_modulation(kind='am', wave='sine', freq=10e3, high=3.0, low=0.0, duty=50.0, phase=0.0, mod_wave='sine',
                       mod_freq=100.0, depth=50.0, deviation=None, hop=None, phase_dev=None, psk_inverted=False,
                       source='internal', experimental=False):
    """Writes for a modulated carrier (MODE 1). Units: Hz, V, %, degrees.

    kind: 'am' (depth, %), 'fm' (deviation, Hz), 'fsk' (hop, Hz: an absolute frequency [M]); with experimental=True
    also 'pm' (phase_dev, degrees; PDEV = int_deg // 18 [H], effect [L]), 'ask' (no parameter), 'psk'
    (psk_inverted). mod_wave sine/triangle/square at mod_freq (for FSK: the keying rate [M]).
    Order = the canonical SCPI order (modulation_scpi), final-state equivalent to DSWave executing it.
    """
    _choice('kind', kind, MOD_KINDS)
    if kind not in GUI_MOD_KINDS and not experimental:
        _bad("modulation '%s' is HAL-only in DSWave 1.2.1 (its GUI offers am, fm, fsk) and unverified; pass "
             "experimental=True to send it anyway" % kind)
    _choice('source', source, MOD_SOURCES)
    if source != 'internal' and not experimental:
        _bad('modulation source must be internal (DSWave 1.2.1 does not let the user change it; the external input '
             'pin is unknown); pass experimental=True to send it anyway')
    _choice('mod_wave', mod_wave, MOD_WAVES)
    _num('mod_freq', mod_freq)
    pulse_freq = None
    if kind == 'fsk':
        if hop is None:
            _bad('fsk needs hop (Hz)')
        _num('hop', hop)
        if not F_MIN <= hop <= F_MAX.get(wave, 1e6):
            _bad('fsk hop frequency must be 0.1 Hz .. %g Hz' % F_MAX.get(wave, 1e6))
        pulse_freq = max(freq, hop)                              # GUI:case 0x8: duty limit uses max(carrier, hop)
    _check_carrier(wave, freq, high, low, duty, phase, pulse_freq)
    blocks = _carrier_blocks(wave, freq, high, low, duty, phase)
    fmax_mod = freq if kind == 'fm' else 1e6                     # GUI:case 0x17
    if not F_MIN <= mod_freq <= fmax_mod:
        _bad('modulation frequency must be 0.1 Hz .. %g Hz%s' % (fmax_mod, ' (FM: at most the carrier)'
                                                                   if kind == 'fm' else ''))
    if kind == 'am':
        _num('depth', depth)
        dmax = am_depth_max(high, low)
        if not 0.0 <= depth <= dmax + 1e-9:
            _bad('AM depth must be 0 .. %.4g %% at %g/%g V (above 100 %% the levels widen; 0..3 V is kept)'
                 % (dmax, high, low))
    elif kind == 'fm':
        if deviation is None:
            _bad('fm needs deviation (Hz)')
        _num('deviation', deviation)
        if not 0.0 <= deviation <= freq:                         # GUI:case 0x1a
            _bad('FM deviation must be 0 .. the carrier frequency (%g Hz)' % freq)
    elif kind == 'pm':
        if phase_dev is None:
            _bad('pm needs phase_dev (degrees)')
        _num('phase_dev', phase_dev)
        if not 0.0 <= phase_dev <= PM_DEV_MAX:
            _bad('PM phase deviation must be 0 .. 360 degrees')
    hal = DswaveAwgHal(enabled=1, mode=MODE_MODULATION, mod_type=MOD_KINDS[kind],
                       high=micro(high) / 6000000.0, low=micro(low) / 6000000.0)
    w = list(blocks['stop'])                                     # ENABLE 2: stopped while configuring
    w += _mode_block(MODE_MODULATION)                            # :AWG:Mode 0,1
    w += hal.modulation_mode(0, MOD_KINDS[kind])                 # before the levels (awg_mod.md 4.3)
    w += blocks['wave'] + blocks['freq'] + blocks['phase'] + blocks['levels'] + blocks['duty']
    w += hal.modulation_source(0, MOD_SOURCES[source])
    w += hal.modulation_wave(0, MOD_WAVES[mod_wave])
    w += hal.modulation_frequency(0, micro(mod_freq))
    if kind == 'am':
        w += hal.am_depth(0, micro(depth))                       # AM_STATE, AM_PARAM0/1, then HIGH/LOW (HAL formula)
    elif kind == 'fm':
        w += hal.fm_deviation(0, micro(deviation))
    elif kind == 'pm':
        w += hal.pm_phase(0, micro(phase_dev))
    elif kind == 'fsk':
        w += hal.fsk_frequency(0, micro(hop))
    elif kind == 'psk':
        w += hal.psk_polarity(0, 1 if psk_inverted else 0)
    return w


# ---- sweep ---------------------------------------------------------------------------------------------------------
def sweep_scpi(start, end, time, sweep_type='linear', direction='up', mark=None, wave='sine', high=3.0, low=0.0,
               duty=50.0, phase=0.0, freq=None):
    """Canonical SCPI order (re/notes/awg_sweep.md section 4): basic carrier in mode 0, SWEEPmode 0 (linear, never
    asserts), direction, start, end, time, [mark], SWEEPmode <type> (the final recompute), Mode 2 last."""
    c = [':AWG:Enable 0,1', ':AWG:Mode 0,0', ':AWG:WAVE 0,%d' % awg.WAVES[wave],
         ':AWG:FREQUENCY 0,%d' % micro(start if freq is None else freq), ':AWG:PHASE 0,%d' % micro(phase),
         ':AWG:HIGHlevel 0,%d' % micro(high), ':AWG:LOWlevel 0,%d' % micro(low), ':AWG:DUTY 0,%d' % micro(duty),
         ':AWG:SWEEPmode 0,0', ':AWG:SWEEPdirection 0,%d' % SWEEP_DIRS[direction],
         ':AWG:SWEEPstartFrequency 0,%d' % micro(start), ':AWG:SWEEPendFrequency 0,%d' % micro(end),
         ':AWG:SWEEPtime 0,%d' % pico(time)]
    if mark is not None:
        c.append(':AWG:SWEEPfrequencyMark 0,%d' % micro(mark))
    return c + [':AWG:SWEEPmode 0,%d' % SWEEP_TYPES[sweep_type], ':AWG:Mode 0,2']


def _sweep_tail(start, end, time, sweep_type, direction, mark):
    """The sweep commands of sweep_scpi() replayed from a HAL that already holds the same settings (steady state):
    exactly what DSWave sends for them; from another prior state only the two leading recomputes differ."""
    typ, down = SWEEP_TYPES[sweep_type], SWEEP_DIRS[direction]
    hal = DswaveAwgHal(enabled=1, swp_type=0, swp_dir=down, swp_start=fcw40(start), swp_end=fcw40(end),
                       swp_time=pico(time))
    w = hal.sweep_mode(0, 0) + hal.sweep_direction(0, down) + hal.sweep_start(0, micro(start)) \
        + hal.sweep_end(0, micro(end)) + hal.sweep_time(0, pico(time))
    if mark is not None:
        w += hal.sweep_mark(0, micro(mark))
    return w + hal.sweep_mode(0, typ), hal


def program_sweep(start, end, time, sweep_type='linear', direction='up', mark=None, wave='sine', high=3.0, low=0.0,
                  duty=50.0, phase=0.0, freq=None):
    """Writes for a frequency sweep (MODE 2) from `start` to `end` Hz in `time` s.

    direction 'up' needs start < end, 'down' start > end (DSWave GUI:case 0x34/0x35; the log maths assumes it too).
    `freq` is the carrier FREQUENCY register (default: start); the GUI greys Frequency out in sweep mode and
    whether the FPGA uses it then is TODO(recording) (corpus scenarios use a 300 Hz base to tell).
    Register program [H]: SFCW/EFCW = trunc(f * 40); linear STEP = round(1e8 * dFCW / T_ps) per 100 us (TINC 5000)
    or, if slower than 1 FCW per 100 us, STEP 1 and TINC = ceil(T/dFCW/20000); log: LOGR n (14..1), TINC = ts/20000,
    SFCW << n. What the FPGA does with them (20 ns ticks, x(1 +- 2^-n) per step, 1CYC, retrace) is [M]/[L]:
    TODO(recording) with an A0 capture.
    """
    _choice('sweep_type', sweep_type, SWEEP_TYPES)
    _choice('direction', direction, SWEEP_DIRS)
    for n, x in (('start', start), ('end', end), ('time', time)):
        _num(n, x)
    _choice('wave', wave, CARRIER_WAVES)
    fmax = F_MAX[wave]
    for n, x in (('start', start), ('end', end)):
        if not F_MIN <= x <= fmax:
            _bad('sweep %s frequency must be 0.1 Hz .. %g Hz for %s' % (n, fmax, wave))
    if start == end or fcw40(start) == fcw40(end):
        _bad('sweep start and end must differ (use basic mode for a fixed frequency)')
    if (direction == 'up') != (end > start):
        _bad("direction 'up' needs end > start and 'down' needs end < start (DSWave GUI)")
    if not SWEEP_TIME_RANGE[0] - 1e-15 <= time <= SWEEP_TIME_RANGE[1]:
        _bad('sweep time must be 1 ms .. 500 s (DSWave GUI)')
    if mark is not None:
        _num('mark', mark)
        if not min(start, end) <= mark <= max(start, end):
            _bad('sweep mark must lie between start and end')
    _check_carrier(wave, start if freq is None else freq, high, low, duty, phase, max(start, end))
    blocks = _carrier_blocks(wave, start if freq is None else freq, high, low, duty, phase)
    try:
        tail, _hal = _sweep_tail(start, end, time, sweep_type, direction, mark)
    except DswaveAssert as e:                                    # cannot happen inside the GUI ranges; kept as a guard
        _bad('sweep rejected: DSWave would assert (%s)' % e)
    w = []
    for key in ('stop', 'mode', 'wave', 'freq', 'phase', 'levels', 'duty'):
        w += blocks[key]
    return w + tail + _mode_block(MODE_SWEEP)


def sweep_info(start, end, time, sweep_type='linear', direction='up'):
    """Register values and the predicted sweep timing (timing is the [M] hypothesis of awg_sweep.md 3.2/3.3:
    the output frequency steps every TINC x 20 ns by +-STEP FCW (linear) or x(1 +- 2^-n) (log)). TODO(recording)."""
    tail, hal = _sweep_tail(start, end, time, sweep_type, direction, None)
    regs = final(tail)
    tinc = regs[A_SWP_TINC0] | (regs[A_SWP_TINC1] << 16)
    s, e = fcw40(start), fcw40(end)
    out = {'sfcw': s, 'efcw': e, 'tinc': tinc, 'step_s': tinc * 20e-9}
    if sweep_type == 'linear':
        step = regs[A_SWP_STEP0] | (regs[A_SWP_STEP1] << 16)
        nsteps = math.ceil(abs(e - s) / step)
        out.update(step_fcw=step, one_cycle=regs[A_SWP_1CYC], steps=nsteps, duration_s=nsteps * tinc * 20e-9)
    else:
        n = regs[A_SWP_LOGR0]
        r = 1.0 - 2.0 ** -n if direction == 'down' else 1.0 + 2.0 ** -n
        nsteps = math.ceil(math.log(e / s) / math.log(r))
        out.update(logr=n, steps=nsteps, duration_s=nsteps * tinc * 20e-9)
    return out


# ---- burst ---------------------------------------------------------------------------------------------------------
def burst_scpi(wave='sine', freq=1000.0, high=3.0, low=0.0, duty=50.0, phase=0.0, cycles=1, period=10e-3,
               burst_mode='ncycle', gate_polarity='positive', counter=1, trig_source='internal', trig_edge='rising',
               trig_out='rising', trig_delay=0.0):
    """DSWave's burst parameter-list order (RVA 0xC3A5C0): Enable, Mode 3, carrier, then the ten burst/trigger
    commands; FREQUENCY after Mode 3 so RPAR/FPAR follow the carrier."""
    return [':AWG:Enable 0,1', ':AWG:Mode 0,3', ':AWG:WAVE 0,%d' % awg.WAVES[wave],
            ':AWG:FREQUENCY 0,%d' % micro(freq), ':AWG:PHASE 0,%d' % micro(phase),
            ':AWG:HIGHlevel 0,%d' % micro(high), ':AWG:LOWlevel 0,%d' % micro(low),
            ':AWG:DUTY 0,%d' % micro(duty), ':AWG:BURSTmode 0,%d' % BURST_MODES[burst_mode],
            ':AWG:BURSTncycleMode 0,%d' % NCYCLE_LIMITED, ':AWG:BURSTcyclesNum 0,%d' % cycles,
            ':AWG:BURSTcounter 0,%d' % counter, ':AWG:BURSTgatePolarity 0,%d' % GATE_POLARITIES[gate_polarity],
            ':AWG:BURSTperiod 0,%d' % pico(period), ':AWG:TRIGGERsource 0,%d' % TRIG_SOURCES[trig_source],
            ':AWG:TRIGGERedge 0,%d' % TRIG_EDGES[trig_edge], ':AWG:TRIGGERoutput 0,%d' % TRIG_OUTS[trig_out],
            ':AWG:TRIGGERdelay 0,%d' % pico(trig_delay)]


def program_burst(wave='sine', freq=1000.0, high=3.0, low=0.0, duty=50.0, phase=0.0, cycles=1, period=10e-3,
                  burst_mode='ncycle', gate_polarity='positive', counter=1, trig_source='internal',
                  trig_edge='rising', trig_out='rising', trig_delay=0.0):
    """Writes for a burst (MODE 3): `cycles` carrier cycles (N-cycle) or a gate (gated) every `period` s.

    Defaults are DSWave's (RVA 0xBAB6E0). Register program [H]: BST_* raw, TRIG_FCW = trunc(1e12/P_ps x 2^32/1e8),
    TRIG_DELY = ns (32 bits). Meaning, all TODO(recording) on A0: the internal trigger as a 32-bit accumulator at
    100 MHz (burst every 2^32/(1e8 TRIG_FCW) s, burst_info()) [M]; bursts start at PHASE and idle at the start-phase
    value [L]; gated = gate open for the first half of the period [L]; BURSTcounter [L]; external trigger pin, manual
    trigger and trig-out pin unknown [L].
    """
    _choice('burst_mode', burst_mode, BURST_MODES)
    _choice('gate_polarity', gate_polarity, GATE_POLARITIES)
    _choice('trig_source', trig_source, TRIG_SOURCES)
    _choice('trig_edge', trig_edge, TRIG_EDGES)
    _choice('trig_out', trig_out, TRIG_OUTS)
    _check_carrier(wave, freq, high, low, duty, phase)
    blocks = _carrier_blocks(wave, freq, high, low, duty, phase)
    for n, x in (('cycles', cycles), ('counter', counter)):
        if isinstance(x, bool) or not isinstance(x, int):
            _bad('%s must be an integer' % n)
    nmax = burst_cycles_max(freq)
    if not 1 <= cycles <= nmax:
        _bad('burst cycles must be 1 .. %d at %g Hz (DSWave GUI: min(100, 15 f - 0.5))' % (nmax, freq))
    if not BURST_COUNTER_RANGE[0] <= counter <= BURST_COUNTER_RANGE[1]:
        _bad('burst counter must be 1 .. 32 (DSWave GUI)')
    _num('period', period)
    pmin = (cycles + 0.5) / freq                                 # GUI:case 0x5d
    if not pmin * (1 - 1e-12) <= period <= BURST_PERIOD_MAX:
        _bad('burst period must be (cycles + 0.5)/f = %.6g s .. 15 s' % pmin)
    _num('trig_delay', trig_delay)
    if not 0.0 <= trig_delay <= TRIG_DELAY_MAX:
        _bad('trigger delay must be 0 .. 4.294967295 s (32-bit ns register; DSWave allows 10 s and wraps)')
    hal = DswaveAwgHal(enabled=1, mode=MODE_BURST)
    w = list(blocks['stop']) + _mode_block(MODE_BURST)
    w += blocks['wave'] + blocks['freq'] + blocks['phase'] + blocks['levels'] + blocks['duty']
    w += hal.burst_mode(0, BURST_MODES[burst_mode]) + hal.burst_ncycle_mode(0, NCYCLE_LIMITED)
    w += hal.burst_cycles(0, cycles) + hal.burst_counter(0, counter)
    w += hal.burst_gate_polarity(0, GATE_POLARITIES[gate_polarity])
    w += hal.burst_period(0, pico(period))
    w += hal.trigger_source(0, TRIG_SOURCES[trig_source]) + hal.trigger_edge(0, TRIG_EDGES[trig_edge])
    w += hal.trigger_output(0, TRIG_OUTS[trig_out]) + hal.trigger_delay(0, pico(trig_delay))
    return w


def burst_info(period, trig_delay=0.0):
    """TRIG_FCW and the effective burst period under the [M] model of a 32-bit accumulator clocked at 100 MHz
    (TODO(recording): measure the repetition at 1 ms, predicted 1.0000157 ms). Long periods quantize: 1 s -> 1.023 s."""
    t = trig_fcw(period)
    return {'trig_fcw': t, 'period_effective_s': (2 ** 32) / (1e8 * t) if t else math.inf,
            'trig_delay_ns': trig_delay_ns(trig_delay) & 0xFFFFFFFF}
