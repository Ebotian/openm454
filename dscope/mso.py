"""MSO settings compiler for the DSCope M454: a port of DSWave 1.2.1's HAL (``hal/mso/mso.cpp`` and
``hal/mso/msousbcontroller.cpp``) that turns SCPI-level oscilloscope settings into the FPGA register
program written before an acquisition.

Everything here is pure computation (no USB).  The source of every formula is noted next to it as the
RVA of the DSWave.exe function it was ported from (see re/notes/mso_formulas.md for the long version).

Public API
----------
* :class:`AnalogChannel`, :class:`MsoConfig` -- the settings, in the natural units of DSWave's SCPI
  layer (vdiv in uV, offset / trigger level as 8-bit ADC codes, times in ps).  ``MsoConfig.apply_scpi``
  applies one SCPI command string exactly like DSWave's HAL dispatcher does.
* :func:`rates_and_depths` -- the ``:ANALOG|DIGITAL:SAMPLE:Rate?|Depth?`` answers.
* :func:`geometry` -- the derived acquisition geometry (folds, MTXS, block sizes, packet sizes, ...).
* :func:`setup_program` -- the exact ordered (addr, value) register writes DSWave issues from
  ``W 0x01=0`` through ``W 0xF9=0`` (UsbBsp3x_SetupAcquire 0x34acf0).
* :func:`parse_zero_info` -- the 512-byte EEPROM calibration ("zero info") blob.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------------------------------
# Device profile (DSWave.exe .rdata/.data, prof_M454_usb3 0xbac920 / hw_M454_usb3 0xbac9a0)
# --------------------------------------------------------------------------------------------------

#: m454_samplerates_usb3 (0xc48fe0), Hz.  Also the "snap down" table of FUN_140350f50.
SAMPLERATES = (1_000, 2_000, 5_000, 10_000, 20_000, 50_000, 100_000, 200_000, 500_000,
               1_000_000, 2_000_000, 5_000_000, 10_000_000, 20_000_000, 50_000_000, 100_000_000)
#: m454_vdivs_usb3 (0xc49160), uV.  Index i of this table <-> VGA/calibration entry i.
VDIVS = (1_000, 2_000, 5_000, 10_000, 20_000, 50_000, 100_000, 200_000, 500_000,
         1_000_000, 2_000_000, 5_000_000)
#: m454_vga_usb3 (0xc49080): factory default {vdiv: (gain | relay << 16, pwm_offset)}; the EEPROM
#: zero info overrides both per channel.
VGA_DEFAULT = {1_000: (0x00031, 0xD7), 2_000: (0x00011, 0xD7), 5_000: (0x00010, 0xD7),
               10_000: (0x00000, 0xD7), 20_000: (0x00018, 0xD7), 50_000: (0x00002, 0xD7),
               100_000: (0x0001A, 0xD7), 200_000: (0x10001, 0xD7), 500_000: (0x10000, 0xD7),
               1_000_000: (0x10018, 0xD7), 2_000_000: (0x10008, 0xD7), 5_000_000: (0x1001A, 0xD7)}

N_ANALOG = 4                 # analog probe profile (m454_probes_usb3 record type 1) +0x04
N_DIGITAL = 8                # digital probe profile (record type 0) +0x04
STORAGE_DIV = 4              # prof+0x20: m_hori_depth = STORAGEdepth / 4  (Mso_ScpiCommand case 0xd)
HW_DEPTH = 0x2000_0000       # prof+0x30: hw_depth (bytes)
TIME_UNIT_PS = 10_000        # prof+0x38: pulse / hold time unit (10 ns)
ACH_INVERT = 0x0F            # hw+0x58: hardware analog channel inversion mask
PWM_BITS = 10                # hw+0x7c
ROLL_TIME_PS = 200_000_000_000   # hw+0x70: repeat-mode roll threshold (200 ms)
RATE_THRESHOLD_KBPS = {True: 102_400, False: 10_240}   # hw+0x68 (USB3 / USB2 profile)
ANALOG_BASE_RATE = 100_000_000   # analog probe profile +0x10 (also digital +0x10)
ANALOG_MIN_RATE = 1_000          # probe profile +0x08 hw_min_samplerate
ANALOG_DIV_M = 1                 # probe profile +0x28 (u32)
DIGITAL_MAX_FOLD_LOG2 = 2        # digital probe profile +0x24
ANALOG_FOLD_UNITS = 4            # analog probe profile +0x1c * +0x04 (= 1 * 4)
ABRH_BITS_PER_CH = 1             # analog probe profile +0x1c
THR_REF_MIN_UV, THR_REF_MAX_UV = 0, 5_000_000   # digital probe profile +0x30 / +0x38

# Ad9288 (vtbl_Ad9288 0xff1690; UsbBsp_CreateAdc picks it because hw+0x80 == 0)
ADC_BITS = 8                 # vt+0x38 FUN_1405c1f90
ADC_MARGIN = 15              # vt+0x68 FUN_14033c2d0
ADC_VREF = 5.0               # vt+0x48 FUN_1405c1fa0


def _adc_trim_gain(c: int) -> float:
    """Ad9288 vt+0x50 (FUN_14033c400): 2 / (10**(c*24/20/4096) - 1)."""
    return 2.0 / (math.pow(10.0, (((float(c) * 24.0) / 20.0) * 0.000244140625)) - 1.0)


BYTES_PER_FRAME = 0x1000     # m_bsp.BYTES_PER_FRMAE / UsbBsp3x_GetHeadSize (head packet bytes)
DEPTH_ALIGN_MASK = 0xFFF     # UsbBsp3x vt+0x190 FUN_1405c2390
PREW_DIV = 8                 # MsoUsbController+0x08 (ctor)
DTHR_SCALE = 1023            # MsoUsbController+0x0c
DTHR_FACTOR = 0.7575757575757576   # MsoUsbController+0x10 = 0x3FE83E0F83E0F83E (25/33)
PKT_TIME_MS = 20             # FUN_140340e20
XFER_TIME_MS = 200           # FUN_140340e40

# register addresses (dscope/regs.py names)
HW_BASE_MODE, HW_BASE_CTRL, TRIG_ADDR, TRIG_DATA, TRIG_SHFT, FLUSH_DATA = 0x00, 0x01, 0x09, 0x0A, 0x0B, 0xF9

# :ACROSS lookup by enabled-analog mask (MsoCtl_SetAnalogFoldCross 0x3544b0, stack table)
ACROSS_TABLE = (0xF, 0xF, 0xF, 0xA, 0xF, 0x5, 0x5, 0x0, 0xF, 0x5, 0xA, 0x0, 0xA, 0x0, 0x0, 0x0)

TRIG_RISE, TRIG_FALL, TRIG_ANY = 0, 1, 2

# --------------------------------------------------------------------------------------------------
# Zero info (EEPROM calibration)
# --------------------------------------------------------------------------------------------------


@dataclass
class ProbeCal:
    """One 128-byte zero-info record (MsoZero_LoadInfo 0x355890)."""
    pwm_scale: int                       # u16 @5  -> probe+0x30 (VPWM full-scale)
    adc_trim: int                        # s8  @7  -> probe+0x32 (fine gain trim, used when 1 ch on)
    extra: int                           # u16 @8  -> probe+0x34
    vga: List[Tuple[int, int]]           # n x (gain|relay<<16, pwm_offset), index-aligned to VDIVS
    cross_a: List[int] = field(default_factory=list)   # s16 pairs -> probe+0x68
    cross_b: List[int] = field(default_factory=list)   #              probe+0x80
    raw_a: bytes = b''                   # n_a bytes -> probe+0x38 (unused by Ad9288)
    raw_b: bytes = b''                   # n_b bytes -> probe+0x50

    def vga_entry(self, vdiv_uv: int) -> Optional[Tuple[int, int]]:
        try:
            i = VDIVS.index(vdiv_uv)
        except ValueError:
            return None
        if i < len(self.vga):
            return self.vga[i]
        return VGA_DEFAULT[vdiv_uv]


def parse_probe_cal(rec: bytes) -> ProbeCal:
    """Parse one channel record (byte0 tag 0xAB, byte1 n_vga, byte2 n_a, byte3 n_b, byte4 n_cross,
    u16@5 pwm_scale, s8@7 adc_trim, u16@8 extra, n_vga x {u16 pwm, u16 gain, u16 relay}, n_a + n_b
    bytes, n_cross x {s16 a, s16 b})."""
    if len(rec) < 10:
        raise ValueError('zero info record too short')
    if rec[0] != 0xAB:
        raise ValueError('zero info tag is 0x%02X, expected 0xAB' % rec[0])
    n_vga, n_a, n_b, n_cross = rec[1], rec[2], rec[3], rec[4]
    pwm_scale, = struct.unpack_from('<H', rec, 5)
    adc_trim, = struct.unpack_from('<b', rec, 7)
    extra, = struct.unpack_from('<H', rec, 8)
    off = 10
    if len(rec) < n_vga * 6 + 10:
        raise ValueError('zero info record truncated (vga)')
    vga = []
    for _ in range(n_vga):
        pwm, v32 = struct.unpack_from('<HI', rec, off)
        vga.append((v32, pwm))
        off += 6
    raw_a = bytes(rec[off:off + n_a]); off += n_a
    raw_b = bytes(rec[off:off + n_b]); off += n_b
    if len(rec) < off + 4 * n_cross:
        raise ValueError('zero info record truncated (cross)')
    ca, cb = [], []
    for _ in range(n_cross):
        a, b = struct.unpack_from('<hh', rec, off)
        ca.append(a); cb.append(b)
        off += 4
    return ProbeCal(pwm_scale, adc_trim, extra, vga, ca, cb, raw_a, raw_b)


def parse_zero_info(blob: bytes) -> List[ProbeCal]:
    """Parse the 512-byte zero-info blob (4 x 128 B, EEPROM 0x000-0x1FF, MsoCtl_LoadZero 0x354ef0)."""
    if len(blob) < N_ANALOG * 128:
        raise ValueError('zero info must be %d bytes' % (N_ANALOG * 128))
    return [parse_probe_cal(blob[i * 128:(i + 1) * 128]) for i in range(N_ANALOG)]


def load_zero_info(path=None) -> List[ProbeCal]:
    """Load the cached zero info (default: state/cache/zero_info_from_trace.bin)."""
    if path is None:
        path = Path(__file__).resolve().parent.parent / 'state' / 'cache' / 'zero_info_from_trace.bin'
    return parse_zero_info(Path(path).read_bytes())


def _as_cal(zero_info) -> List[ProbeCal]:
    if isinstance(zero_info, (bytes, bytearray, memoryview)):
        return parse_zero_info(bytes(zero_info))
    return list(zero_info)

# --------------------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------------------


@dataclass
class AnalogChannel:
    enable: bool = True
    coupling: str = 'dc'            # 'dc' | 'ac'      (:ANALOG:CHANNEL:Coupling 0=DC 1=AC)
    bw20: bool = False              # :ANALOG:CHANNEL:BWlimit
    invert: bool = False            # :ANALOG:CHANNEL:Invert
    vdiv_uv: int = 1_000_000        # :ANALOG:CHANNEL:Vdiv (uV/div, one of VDIVS)
    voff_code: int = 128            # :ANALOG:CHANNEL:Offset (8-bit code, 128 = mid)
    trig_level_code: int = 128      # :ANALOG:CHANNEL:TRIGGERlevel (8-bit code)


@dataclass
class MsoConfig:
    analog: List[AnalogChannel] = field(default_factory=lambda: [AnalogChannel() for _ in range(N_ANALOG)])
    digital: List[bool] = field(default_factory=lambda: [True] * N_DIGITAL)
    threshold_uv: int = 1_400_000   # :DIGITAL:Threshold (uV, 0..5 V)
    sample_time_ps: int = 1_000_000_000   # :SAMPLEtime (capture window, ps)
    storage_depth: int = 400_000    # :STORAGEdepth (total sample budget S)
    auto_rate: bool = True          # :AUTORATEModeEnable (DSWave default on)
    analog_rate: Optional[int] = None    # :ANALOG:SAMPLE:Rate (only used when auto_rate is False)
    digital_rate: Optional[int] = None   # :DIGITAL:SAMPLE:Rate (only used when auto_rate is False)
    # trigger
    trig_enable: bool = False       # :TRIGger:Enable
    trig_mode: int = 0              # :TRIGger:Mode (0 = auto, else normal)
    trig_channel: int = 0           # :TRIGger:Channel index,type
    trig_channel_type: int = 1      #   1 = analog, 0 = digital
    trig_type: int = TRIG_RISE      # :TRIGger:Type 0 rise 1 fall 2 any 3..6 pulse variants
    trig_pulse_ps: int = 0          # :TRIGger:PULSEtime
    trig_margin: int = 5            # :TRIGger:Margin
    trig_hold_ps: int = 0           # :TRIGger:HOLDtime
    trig_sample_index: int = 0      # :TRIGger:SAMPLEindex (pre-trigger samples)
    # modes (UsbDevice_SetModes 0x340630)
    repeat_mode: bool = False       # :REPEATModeEnable  (bsp+0x34)
    loop_mode: bool = False         # :LOOPModeEnable    (bsp+0x36)
    transfer_mode: bool = False     # :TRANSFERModeEnable (bsp+0x31; no effect on the setup block)
    usb3: bool = True               # profile prof_M454_usb3 (SuperSpeed) vs prof_M454_usb2

    @property
    def auto_trig(self) -> bool:
        """m_trigger.auto_trig = (Mode == 0)  (Mso_ScpiCommand case 0x12 -> FUN_140350ad0)."""
        return self.trig_mode == 0

    def copy(self) -> 'MsoConfig':
        return replace(self, analog=[replace(a) for a in self.analog], digital=list(self.digital))

    # ---- SCPI ----------------------------------------------------------------------------------
    def apply_scpi(self, command: str) -> bool:
        """Apply one SCPI command string (``':ANALOG:CHANNEL:Vdiv 0,1000000'``, hex or decimal args).
        Queries and commands that do not affect the MSO setup (AWG, :Work:RUN, ...) are ignored.
        Returns True when the command changed a field."""
        cmd = command.strip()
        if not cmd or cmd.endswith('?'):
            return False
        head, _, rest = cmd.partition(' ')
        args = [int(a, 0) for a in rest.split(',')] if rest.strip() else []
        h = head.upper()

        def arg(i):
            return args[i]
        if h.startswith(':ANALOG:CHANNEL:'):
            sub = h[len(':ANALOG:CHANNEL:'):]
            ch = self.analog[arg(0)]
            v = arg(1)
            if sub == 'ENABLE':
                ch.enable = bool(v)
            elif sub == 'COUPLING':
                ch.coupling = 'ac' if v else 'dc'
            elif sub == 'BWLIMIT':
                ch.bw20 = bool(v)
            elif sub == 'INVERT':
                ch.invert = bool(v)
            elif sub == 'VDIV':
                ch.vdiv_uv = v
            elif sub == 'OFFSET':
                ch.voff_code = v & 0xFFFF
            elif sub == 'TRIGGERLEVEL':
                ch.trig_level_code = v & 0xFFFF
            else:
                return False
            return True
        if h == ':DIGITAL:CHANNEL:ENABLE':
            self.digital[arg(0)] = bool(arg(1))
            return True
        table = {
            ':DIGITAL:THRESHOLD': 'threshold_uv', ':SAMPLETIME': 'sample_time_ps',
            ':STORAGEDEPTH': 'storage_depth', ':TRIGGER:MODE': 'trig_mode', ':TRIGGER:TYPE': 'trig_type',
            ':TRIGGER:PULSETIME': 'trig_pulse_ps', ':TRIGGER:MARGIN': 'trig_margin',
            ':TRIGGER:HOLDTIME': 'trig_hold_ps', ':TRIGGER:SAMPLEINDEX': 'trig_sample_index',
            ':ANALOG:SAMPLE:RATE': 'analog_rate', ':DIGITAL:SAMPLE:RATE': 'digital_rate'}
        btable = {':TRIGGER:ENABLE': 'trig_enable', ':REPEATMODEENABLE': 'repeat_mode',
                  ':LOOPMODEENABLE': 'loop_mode', ':TRANSFERMODEENABLE': 'transfer_mode',
                  ':AUTORATEMODEENABLE': 'auto_rate'}
        if h in table:
            setattr(self, table[h], arg(0))
            return True
        if h in btable:
            setattr(self, btable[h], bool(arg(0)))
            return True
        if h == ':TRIGGER:CHANNEL':
            self.trig_channel, self.trig_channel_type = arg(0), arg(1)
            return True
        return False   # :ROLLModeEnable (unsupported by the HAL, returns -8), :AWG:*, :Work:RUN ...

# --------------------------------------------------------------------------------------------------
# Derived state
# --------------------------------------------------------------------------------------------------


def _n_analog(cfg: MsoConfig) -> int:
    return sum(1 for a in cfg.analog if a.enable)


def _n_digital(cfg: MsoConfig) -> int:
    return sum(1 for d in cfg.digital if d)


def _floor_log2(x: int) -> int:
    return x.bit_length() - 1 if x > 0 else 0


def analog_fold_log2(cfg: MsoConfig) -> int:
    """mso+0x80 (FUN_1403502b0): floor(log2(4 / n_enabled)); 0 in loop mode or with no channel."""
    n = _n_analog(cfg)
    if cfg.loop_mode or n == 0:
        return 0
    return _floor_log2(ANALOG_FOLD_UNITS // n)


def digital_fold_log2(cfg: MsoConfig) -> int:
    """mso+0x81 (Mso_SetDigitalEnable 0x350620): min(floor(log2(8 / n)), 2)."""
    n = _n_digital(cfg)
    if cfg.loop_mode or n == 0:
        return 0
    return min(_floor_log2(N_DIGITAL // n), DIGITAL_MAX_FOLD_LOG2)


def hori_depth(cfg: MsoConfig) -> int:
    """m_hori_depth = STORAGEdepth / 4 (Mso_ScpiCommand case 0xd)."""
    return cfg.storage_depth // STORAGE_DIV


def snap_table_rate(t_hz: float) -> int:
    """Largest SAMPLERATES entry <= t_hz (0 if none)."""
    t = int(t_hz)
    best = 0
    for r in SAMPLERATES:
        if best < r <= t:
            best = r
    return best


def _snap_rate(cfg: MsoConfig) -> int:
    """FUN_140350f50: largest table rate <= (1e12 / m_hori_time) * m_hori_depth."""
    return snap_table_rate((1000000000000.0 / float(cfg.sample_time_ps)) * float(hori_depth(cfg)))


def rates_and_depths(cfg: MsoConfig) -> Tuple[int, int, int, int]:
    """(analog_rate, analog_depth, digital_rate, digital_depth) as answered by
    :ANALOG:SAMPLE:Rate? / :ANALOG:SAMPLE:Depth? / :DIGITAL:SAMPLE:Rate? / :DIGITAL:SAMPLE:Depth?
    (Mso_ScpiQuery 0x34ffb0; rates from FUN_140350f50 when AUTORATE is on)."""
    af, df = analog_fold_log2(cfg), digital_fold_log2(cfg)
    hd = hori_depth(cfg)
    if cfg.auto_rate:
        snap = _snap_rate(cfg)
        arate = snap * (1 << af) if _n_analog(cfg) > 0 else ANALOG_BASE_RATE
        drate = snap * (1 << df) if _n_digital(cfg) > 0 else ANALOG_BASE_RATE
    else:
        if cfg.analog_rate is None or cfg.digital_rate is None:
            raise ValueError('auto_rate is off: analog_rate and digital_rate must be given')
        arate, drate = cfg.analog_rate, cfg.digital_rate
    return arate, (1 << af) * hd, drate, (1 << df) * hd


@dataclass
class Geometry:
    """Derived acquisition geometry (Mso_ComputeGeometry 0x3512f0 + Mso_Setup 0x352720)."""
    n_analog: int
    n_digital: int
    afold_log2: int                 # mso+0x80
    dfold_log2: int                 # mso+0x81
    analog_rate: int                # mso+0x100
    digital_rate: int               # mso+0x108
    analog_depth: int
    digital_depth: int
    hori_depth: int                 # mso+0x70
    analog_mtxs: int                # mso+0x148
    digital_mtxs: int               # mso+0x14a
    analog_primary: bool            # mso+0x128
    pri_blk_samples: int            # mso+0xe0
    pri_blk_bytes: int              # mso+0xe8
    sec_blk_samples: int            # mso+0xf0
    sec_blk_bytes: int              # mso+0xf8
    blk_bytes: int                  # pri + sec bytes per block
    analog_blk_samples: int         # analog_mtxs * 64
    digital_blk_samples: int        # digital_mtxs * 64
    hw_depth: int                   # mso+0xd8 (samples, 4096-aligned) -> HORI_DEPTH
    n_blocks: int                   # ceil(primary depth / pri_blk_samples) + 2
    acqu_bytes: int                 # mso+0x110 (4096-aligned)
    pkt_pri_samples: int            # mso+0x118
    pkt_bytes: int                  # mso+0x120
    small_pkt: bool                 # mso+0x129
    pkt_analog_samples: int         # mso+0x130
    pkt_digital_samples: int        # mso+0x138
    rd_bytes: int                   # mso+0x140 -> HORI_RWORDS
    data_rate_kBps: float           # FUN_140350860
    roll: bool                      # bsp+0x35
    hw_mode: int                    # HW_BASE_MODE value (UsbBsp3x_vt158 0x34a7a0)
    transfer_size: int              # UsbBsp3x_GetDataTransferSize 0x349b00
    transfer_count: int             # UsbBsp3x_GetDataTransferCount 0x349bd0
    trig_blk: int                   # mtxs of the trigger channel kind (Mso_Setup 0x352720)
    pretrig: int                    # mso+0xa0 rounded pre-trigger samples (ParseHead)
    pretrig_blocks: int             # mso+0xa8
    frame_packet_samples: List[int] # expected "MSO: send samples" per packet of one frame

    @property
    def send_samples(self) -> int:
        return sum(self.frame_packet_samples)


def _roundup(x: int, mask: int) -> int:
    return (x + mask) & ~mask


def _u64(d: float) -> int:
    return int(d)


def geometry(cfg: MsoConfig) -> Geometry:
    nA, nD = _n_analog(cfg), _n_digital(cfg)
    af, df = analog_fold_log2(cfg), digital_fold_log2(cfg)
    arate, adepth, drate, ddepth = rates_and_depths(cfg)
    hd = hori_depth(cfg)
    if arate < ANALOG_MIN_RATE or drate < ANALOG_MIN_RATE:
        raise ValueError('sample rate below hw_min_samplerate (window too long for the depth)')

    # --- MTXS: rate ratio in lowest terms (0x3512f0) ---
    if drate * nD == 0 or nA * arate == 0:
        amtxs = dmtxs = 1
    else:
        g = math.gcd(drate, arate)
        dmtxs, amtxs = drate // g, arate // g
        if dmtxs > 0xFFFF or amtxs > 0xFFFF:
            raise ValueError('rate ratio too large')
    dblk, ablk = dmtxs * 64, amtxs * 64
    ana_bytes = (ADC_BITS * nA * ablk) >> 3
    dig_bytes = (nD * dblk) >> 3
    apri = dig_bytes <= ana_bytes
    if apri:
        pri_s, pri_b, sec_s, sec_b = ablk, max(ana_bytes, dig_bytes), dblk, min(ana_bytes, dig_bytes)
    else:
        pri_s, pri_b, sec_s, sec_b = dblk, max(ana_bytes, dig_bytes), ablk, min(ana_bytes, dig_bytes)
    ch_analog = cfg.trig_channel_type == 1
    trig_blk = ablk if ch_analog else dblk
    hw_a = _roundup((1 << af) * hd + trig_blk, DEPTH_ALIGN_MASK)
    hw_d = _roundup((1 << df) * hd + trig_blk, DEPTH_ALIGN_MASK)
    hw_depth = hw_a if ch_analog else hw_d

    blk = pri_b + sec_b
    if blk == 0:
        raise ValueError('no channel enabled')
    pri_depth = (1 << (af if apri else df)) * hd
    nblk_d = float(math.ceil(float(pri_depth) / float(pri_s))) + 2.0
    acqu = _roundup(_u64(float(blk) * nblk_d), DEPTH_ALIGN_MASK)
    if acqu < BYTES_PER_FRAME:
        raise ValueError('m_acqu_bytes >= m_bsp.BYTES_PER_FRMAE violated')

    # data rate (FUN_140350860), kB/s
    kbps = (((float(drate) / 1000.0) * float(nD)) * 0.125
            + ((float(arate) / 1000.0) * float(nA)) * float(math.ceil(ADC_BITS * 0.125)))
    thr = RATE_THRESHOLD_KBPS[cfg.usb3]
    if not cfg.repeat_mode:
        if kbps >= thr:
            roll = False
            n = float(0x100000 // blk)
        else:
            roll = (cfg.auto_trig if cfg.trig_enable else True)
            n = float(math.ceil((float(PKT_TIME_MS) * kbps) / float(blk)))
        pkt_pri = _u64(n) * pri_s
    else:
        # bsp+0x35 is only maintained by Mso_SetHoriTime / FUN_140350ad0 in repeat mode
        roll = cfg.auto_trig and cfg.sample_time_ps > ROLL_TIME_PS
        pkt_pri = _u64(float(pri_s) * nblk_d)
    if pkt_pri % pri_s:
        raise ValueError('m_pkt_pri_samples % m_pri_blk_samples != 0')
    nb = pkt_pri // pri_s
    pkt_bytes = blk * nb
    rd_bytes = pkt_bytes
    small = False
    pkt_a, pkt_d = nb * ablk, nb * dblk
    if not cfg.repeat_mode:
        if roll and pkt_bytes <= 0x3FF:
            small = True
            pkt_bytes = 0x1000
        if not cfg.loop_mode:
            pkt_a, pkt_d = _u64(float(ablk) * nblk_d), _u64(float(dblk) * nblk_d)
            rd_bytes = _u64(float(blk) * nblk_d)
        else:
            rd_bytes = blk * nb
    else:
        rd_bytes = blk * nb
        pkt_bytes = _roundup(blk * nb, DEPTH_ALIGN_MASK)
    if rd_bytes > HW_DEPTH:
        raise ValueError('m_rd_bytes <= hw_depth violated')

    hw_mode = int(cfg.repeat_mode) + int(cfg.loop_mode) * 4 + (2 if cfg.loop_mode else int(roll) * 2)
    xfer = pkt_bytes + (BYTES_PER_FRAME if cfg.repeat_mode else 0)
    if roll:
        xcount = min(64, int(math.ceil((float(XFER_TIME_MS) * kbps) / float(xfer))))
    elif cfg.repeat_mode:
        xcount = 2
    else:
        xcount = 16 if cfg.usb3 else 4

    # pre-trigger rounding (Mso_Setup 0x352720 / FUN_140352960)
    pblk = (amtxs if cfg.trig_channel_type != 0 else dmtxs) * 64
    pcount = cfg.trig_sample_index // pblk + 1
    pretrig = pblk * pcount

    # expected "MSO: send samples" per packet (MsoPkt_ParseData 0x358fd0 / MsoPkt_ParseTail 0x358760):
    # packets are full until the one holding the tail (at byte acqu_bytes of the stream); a tail
    # whose 4 KiB info block straddles two packets ("Fragmented tail") adds one more packet
    # reporting 0 samples.  In the small-packet mode each 4 KiB transfer carries one block only.
    if cfg.repeat_mode:
        frame = [(acqu // blk) * pri_s]
    else:
        unit = blk if small else pkt_bytes
        k = acqu // unit
        tail_off = acqu - k * unit
        frame = [(unit // blk) * pri_s] * k + [(tail_off // blk) * pri_s]
        if unit - tail_off < BYTES_PER_FRAME:
            frame.append(0)

    return Geometry(nA, nD, af, df, arate, drate, adepth, ddepth, hd, amtxs, dmtxs, apri,
                    pri_s, pri_b, sec_s, sec_b, blk, ablk, dblk, hw_depth, int(nblk_d), acqu, pkt_pri,
                    pkt_bytes, small, pkt_a, pkt_d, rd_bytes, kbps, roll, hw_mode, xfer, xcount,
                    trig_blk, pretrig, pcount, frame)

# --------------------------------------------------------------------------------------------------
# Register program
# --------------------------------------------------------------------------------------------------


def is_inverted(cfg: MsoConfig, idx: int) -> bool:
    """Mso_IsInverted 0x350a10: hardware inversion (hw+0x58) xor user invert."""
    return bool((ACH_INVERT >> idx) & 1) != bool(cfg.analog[idx].invert)


def _fold_source(cfg: MsoConfig, idx: int, afold: int) -> int:
    """FUN_140354400: a disabled channel borrows the nearest enabled neighbour while folding."""
    if cfg.analog[idx].enable or afold == 0:
        return idx
    n = len(cfg.analog)
    for k in range(1, n):
        j = (idx + n - k) % n
        if cfg.analog[j].enable:
            return j
        j = (idx + k) % n
        if cfg.analog[j].enable:
            return j
    return idx


def cross_mask(cfg: MsoConfig) -> int:
    """Enabled-analog mask that DSWave caches in mso+0x88 to decide whether to re-send CROSS."""
    return sum(1 << i for i, a in enumerate(cfg.analog) if a.enable)


def cross_table(cfg: MsoConfig, cal: Sequence[ProbeCal], idx: int) -> List[int]:
    """The 256 ANAn_CROSS values (MsoCtl_Slot17 0x354c40), without the leading 0xFFFF reset."""
    src = cal[_fold_source(cfg, idx, analog_fold_log2(cfg))]
    # (DSWave indexes the source record's pairs by the destination channel; a record with fewer
    # than 4 pairs would be read out of bounds there -- treat missing pairs as 0 here)
    a = src.cross_a[idx] if idx < len(src.cross_a) else 0
    b = src.cross_b[idx] if idx < len(src.cross_b) else 0
    full = (1 << ADC_BITS) - 1
    k = float(b - a) / (float(full + ADC_MARGIN * -2) * ADC_VREF)
    base = float(a) / ADC_VREF - float(ADC_MARGIN) * k
    out = []
    for i in range(full + 1):
        v = int(float(i) + base + float(i) * k + 0.5)
        out.append(0 if v < 0 else min(v, full))
    return out


def analog_regs(cfg: MsoConfig, cal: Sequence[ProbeCal], idx: int) -> dict:
    """Per-channel BASE/VERT/VOFF/VPWM values."""
    ch = cfg.analog[idx]
    pc = cal[idx]
    base = int(ch.enable) + int(ch.bw20) * 8 + int(ch.coupling.lower() != 'ac') * 4   # 0x354730
    ent = pc.vga_entry(ch.vdiv_uv)
    vert = 0 if ent is None else ((((ent[0] >> 16) * 0x8000) + (ent[0] & 0x7FFF)) & 0xFFFF)  # 0x354840
    # VOFF / VPWM (MsoCtl_SetProbeVoffVpwm 0x354940)
    full = float((1 << ADC_BITS) - 1)
    voff = ch.voff_code & 0xFFFF
    v = int(full - float(voff)) if is_inverted(cfg, idx) else voff
    dv = float(v)
    if pc.adc_trim == 0 or _n_analog(cfg) != 1:
        corr = 0.0
    else:
        corr = (dv - float(1 << (ADC_BITS - 1))) / _adc_trim_gain(pc.adc_trim)
    vpwm = _u64(((corr + dv) / full) * float(pc.pwm_scale))
    if ent is not None:
        vpwm += ent[1]
    if vpwm >= (1 << PWM_BITS):
        raise ValueError('vpwm %d out of range for channel %d' % (vpwm, idx))
    return {'base': base, 'vert': vert, 'voff': voff, 'vpwm': vpwm & 0xFFFF}


def trigger_memory(cfg: MsoConfig) -> List[Tuple[int, int]]:
    """MsoCtl_SetTrigMemory 0x353d00: W 0x09=0, W 0x3A=pulse mode, then (if enabled) TRIG_INDEX and
    the trigger memory program (W 0x09 addr / W 0x0A data pairs, closed by W 0x0B=0)."""
    t = cfg.trig_type
    u = (t - 3) & 0xFFFFFFFF
    pulse_mode = int(u < 2) + int((u & 0xFFFFFFFD) == 0) * 2
    out = [(TRIG_ADDR, 0), (0x3A, pulse_mode)]
    if not cfg.trig_enable:
        return out
    is_pulse = 3 <= t <= 6
    pulse_on = True              # cVar10: trigger enable, cleared on non-pulse paths
    analog = cfg.trig_channel_type == 1
    if analog:
        fold = analog_fold_log2(cfg)
        probes = [(i, a.enable) for i, a in enumerate(cfg.analog)]
    else:
        fold = digital_fold_log2(cfg)
        probes = list(enumerate(cfg.digital))
    n = len(probes)
    pos = -1
    mask = 0xFFFFFFFF
    data0 = 0xFFFFFFFF
    found = False
    if n > 0 and (n >> fold) != 0:
        lim = n >> fold
        cnt = 0
        for k, (pidx, en) in enumerate(probes):
            if en:
                if pidx == cfg.trig_channel:
                    found = True
                    pos = pidx if fold == 0 else cnt
                    idxval = cfg.trig_channel if fold == 0 else cnt
                    out.append((0x36, (idxval - 0x8000) & 0xFFFF))
                    if not analog:
                        pos += N_ANALOG
                    break
                cnt += 1
            if k == n - 1 or cnt >= lim:
                break
    if found:
        mask = ~(1 << pos) & 0xFFFFFFFF
        if not is_pulse:
            data0 = 0xFFFFFFFF if t == TRIG_ANY else mask
            pulse_on = False
        else:
            data0 = 0xFFFFFFFF
    else:
        out.append((0x36, 0))
        pos = -1
        if not is_pulse:
            data0 = 0xFFFFFFFF
            pulse_on = False
    out += [(TRIG_ADDR, 0x000), (TRIG_DATA, data0 & 0xFFFF),
            (TRIG_ADDR, 0x010), (TRIG_DATA, 0xFFFF)]
    rise = ((1 if t == TRIG_RISE else 0) << pos) if pos >= 0 else 0
    out += [(TRIG_ADDR, 0x100), (TRIG_DATA, rise & 0xFFFF),
            (TRIG_ADDR, 0x200), (TRIG_DATA, (~mask) & 0xFFFF)]
    pfold = fold
    pulse = cfg.trig_pulse_ps // TIME_UNIT_PS
    if pulse >> (32 - pfold):
        raise ValueError('pulse time too long for the fold')
    p = (pulse << pfold) & 0xFFFFFFFF if pulse_on else 0
    out += [(TRIG_ADDR, 0x400), (TRIG_DATA, p & 0xFFFF),
            (TRIG_ADDR, 0x401), (TRIG_DATA, p >> 16),
            (TRIG_SHFT, 0)]
    return out


def setup_program(cfg: MsoConfig, zero_info, include_cross: bool = True) -> List[Tuple[int, int]]:
    """Ordered (addr, value) register writes of one acquisition setup, W 0x01=0 .. W 0xF9=0
    (UsbBsp3x_SetupAcquire 0x34acf0 -> Mso_Setup 0x352720 -> MsoUsbController slots).

    include_cross: emit the 4 x 257 ANAn_CROSS writes.  DSWave only re-sends them when the enabled
    analog set (:func:`cross_mask`) changed since its previous run; replaying a block without them
    after a different channel set corrupts the analog levels, so the default is True."""
    cal = _as_cal(zero_info)
    g = geometry(cfg)
    w: List[Tuple[int, int]] = [(HW_BASE_CTRL, 0)]
    afold_reg = (g.afold_log2 + g.n_analog * 0x100) & 0xFFFF
    # per probe: SetAfold (+0x18), ClearAwr (+0x20), SetProbeVoffVpwm (+0x28)
    for i in range(N_ANALOG):
        r = analog_regs(cfg, cal, i)
        b = 0x40 + 0x10 * i
        w += [(b, r['base']), (0x28, afold_reg), (0x2B, 0),
              (b + 1, r['vert']), (0x2B, 0),
              (b + 2, r['voff']), (b + 3, r['vpwm'])]
    # SetAnalogFoldCross (+0x30)
    abrh = 0
    for i in range(N_ANALOG):
        src = _fold_source(cfg, i, g.afold_log2)
        if is_inverted(cfg, src):
            for bit in range(i * ABRH_BITS_PER_CH, (i + 1) * ABRH_BITS_PER_CH):
                abrh |= 1 << bit
    w += [(0x28, afold_reg), (0x29, ACROSS_TABLE[cross_mask(cfg)]), (0x2A, abrh & 0xFFFF)]
    # SetDigital (+0x38)
    den = sum(1 << i for i, d in enumerate(cfg.digital) if d) & 0xFFFF
    w += [(0x23, den), (0x20, (g.dfold_log2 + g.n_digital * 0x100) & 0xFFFF), (0x22, 0)]
    # SetRates (+0x78)
    for (rate, fold, lo) in ((g.analog_rate, g.afold_log2, 0x15), (g.digital_rate, g.dfold_log2, 0x17)):
        div = math.ceil(float((1 << fold) * ANALOG_BASE_RATE) / float(rate))
        m = min(div, ANALOG_DIV_M) & 0xFFFFFFFF
        d = math.ceil(float(div & 0xFFFFFFFF) / float(ANALOG_DIV_M))
        w += [(lo, d & 0xFFFF), (lo + 1, (((m - 1) << 8) + (d >> 16)) & 0xFFFF)]
    # SetHorizontal (+0x70)
    if g.blk_bytes >= BYTES_PER_FRAME:
        raise ValueError('block bytes exceed the frame')
    hb = (int(g.analog_primary) * 2 + int(g.small_pkt) * 8 + int(cfg.trig_channel_type == 1)
          + int(not ((g.n_digital != 0 and g.analog_primary) or g.n_analog == 0)) * 4)
    last = _u64(float(g.rd_bytes) * 0.25 - 1.0)
    w += [(0x19, (g.analog_mtxs - 1) & 0xFFFF), (0x1A, (g.digital_mtxs - 1) & 0xFFFF), (0x10, hb),
          (0x1B, (BYTES_PER_FRAME - g.blk_bytes) >> 2),
          (0x11, (g.hw_depth >> 6) & 0xFFFF), (0x12, (g.hw_depth >> 22) & 0xFFFF),
          (0x13, last & 0xFFFF), (0x14, (last >> 16) & 0xFFFF)]
    # SetTrigBase (+0x40)
    tb = int(cfg.auto_trig) * 2 + int(3 <= cfg.trig_type <= 6) * 4 + int(cfg.trig_enable)
    w.append((0x30, tb))
    # SetTrigMemory (+0x48)
    w += trigger_memory(cfg)
    # SetTrigPos (+0x50)
    prew = ((g.pri_blk_bytes + g.sec_blk_bytes) * g.pretrig_blocks) // PREW_DIV
    w += [(0x31, g.pretrig & 0xFFFF), (0x32, (g.pretrig >> 16) & 0xFFFF),
          (0x33, prew & 0xFFFF), (0x34, (prew >> 16) & 0xFFFF)]
    # SetTrigLevel (+0x58)
    if cfg.trig_channel_type == 1:
        w.append((0x35, cfg.analog[cfg.trig_channel].trig_level_code & 0xFFFF))
    # SetTrigMargin (+0x60), SetTrigHold (+0x68)
    hold = cfg.trig_hold_ps // TIME_UNIT_PS
    if hold >= 1 << 32:
        raise ValueError('hold time too long')
    w += [(0x37, cfg.trig_margin & 0xFFFF), (0x38, hold & 0xFFFF), (0x39, hold >> 16)]
    # CROSS tables (Slot17 +0x88), only when the enabled set changed in DSWave
    if include_cross:
        for i in range(N_ANALOG):
            reg = 0x44 + 0x10 * i
            w.append((reg, 0xFFFF))
            w += [(reg, v) for v in cross_table(cfg, cal, i)]
    # SetDThr (+0x80)
    thr = float(cfg.threshold_uv - THR_REF_MIN_UV) / float(THR_REF_MAX_UV - THR_REF_MIN_UV)
    w.append((0x21, int((float(DTHR_SCALE) * thr) * DTHR_FACTOR) & 0xFFFF))
    # UsbBsp3x_vt158: HW_BASE_MODE, then FLUSH_DATA
    w += [(HW_BASE_MODE, g.hw_mode), (FLUSH_DATA, 0)]
    return w


__all__ = ['AnalogChannel', 'MsoConfig', 'ProbeCal', 'Geometry', 'parse_zero_info', 'parse_probe_cal',
           'load_zero_info', 'rates_and_depths', 'geometry', 'setup_program', 'cross_mask', 'cross_table',
           'trigger_memory', 'analog_regs', 'analog_fold_log2', 'digital_fold_log2', 'hori_depth',
           'is_inverted', 'SAMPLERATES', 'VDIVS']
