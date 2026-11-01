"""Built-in arbitrary waveform generator (MCX output, 0..3 V, sine/square up to 1 MHz, triangle up to 400 kHz).

Register access is indirect: W 0x08 = AWG register address, then W 0x07 = value (DSWave's log labels these
the other way round). Formulas fitted to DSWave 1.2.1's own register programs (tests/fixtures/scenarios/corpus3.json) and
verified by loopback on A0:
  FCW (4/5)      = floor(f_Hz * 40)                       (32-bit, lo/hi)
  RPAR/FPAR      = int(3846153.2 / f_Hz + 0.5)           (14/15, 16/17; edge parameter, same for both)
  PHASE (6/7)    = phase_deg / 360 * 4e9
  HIGH/LOW (8/9) = trunc(uV / 6e6 * 0.91525 * 1023)  (~156.05 codes per volt)
  DUTY (10/11)   = duty_fraction * 4e9
  RSLOPE/FSLOPE (12/13) = floor(68.75 / duty), floor(68.75 / (1 - duty))
  WAVE (0): 0 sine, 1 triangle, 2 square, 3 noise;  MODE (1): 0 basic
  SWITCH (2) = 1 ... 0 brackets mode/wave/frequency updates; ENABLE (3): 2 = armed, 3 = output running
Stopped (ENABLE 2), the output holds the LOW level; awg_off also writes LOW = 0 so "off" means 0 V.
Noise (measured 2026-10-31): Gaussian-like around (low+high)/2 (+30..160 mV), sigma ~2.8 % of (high-low), peaks
about +-0.2 x (high-low), flat to ~2 MHz; the frequency setting has no effect.
"""
import math

from .errors import DscopeError

A_WAVE, A_MODE, A_SWITCH, A_ENABLE = 0, 1, 2, 3
A_FCW0, A_FCW1, A_PH0, A_PH1, A_HIGH, A_LOW = 4, 5, 6, 7, 8, 9
A_DUTY0, A_DUTY1, A_RSLOPE, A_FSLOPE, A_RPAR0, A_RPAR1, A_FPAR0, A_FPAR1 = 10, 11, 12, 13, 14, 15, 16, 17
REG_DATA, REG_ADDR = 0x07, 0x08
WAVES = {'sine': 0, 'triangle': 1, 'square': 2, 'noise': 3}
F_MAX = {'sine': 1e6, 'square': 1e6, 'triangle': 400e3, 'noise': 1e6}
V_MAX = 3.0
MIN_PULSE_S = 380e-9


def program(wave='sine', freq=1000.0, high=3.0, low=0.0, duty=50.0, phase=0.0):
    """Return the ordered list of (awg_addr, value) writes that configure (but do not start) the AWG."""
    if wave not in WAVES:
        raise DscopeError('INVALID_ARG', 'wave must be one of %s' % ', '.join(WAVES))
    if not 0.1 <= freq <= F_MAX[wave]:
        raise DscopeError('INVALID_ARG', '%s frequency must be 0.1 Hz .. %g Hz' % (wave, F_MAX[wave]))
    if not 0.0 <= low < high <= V_MAX + 1e-9:
        raise DscopeError('INVALID_ARG', 'levels must satisfy 0 <= low < high <= 3.0 V')
    if not 0.1 <= duty <= 99.9:
        raise DscopeError('INVALID_ARG', 'duty must be 0.1 .. 99.9 %')
    if wave == 'square':
        period = 1.0 / freq
        if min(duty, 100 - duty) / 100 * period < MIN_PULSE_S * (1 - 1e-9):     # 380 ns itself is allowed
            raise DscopeError('INVALID_ARG', 'pulse narrower than 380 ns at this frequency/duty')
    # DSWave's exact conversions (dscope/awg_modes.py replays its AWG handler write for write)
    from .awg_modes import edge_param, level_code, micro, slopes
    fcw = int((micro(freq) / 1e6) * 40.0)
    rpar = edge_param(freq)
    ph = int(((micro(phase % 360) // 1000000) / 360.0) * 4e9) & 0xFFFFFFFF      # whole degrees, truncated
    dw, rs, fs = slopes(duty)
    dw &= 0xFFFFFFFF
    hi, lo = level_code(high), level_code(low)
    return [
        (A_ENABLE, 0x0002),
        (A_SWITCH, 1), (A_MODE, 0), (A_SWITCH, 0),
        (A_SWITCH, 1), (A_WAVE, WAVES[wave]), (A_SWITCH, 0),
        (A_SWITCH, 1), (A_FCW0, fcw & 0xFFFF), (A_FCW1, fcw >> 16), (A_RPAR0, rpar & 0xFFFF), (A_RPAR1, rpar >> 16),
        (A_FPAR0, rpar & 0xFFFF), (A_FPAR1, rpar >> 16), (A_SWITCH, 0),
        (A_PH0, ph & 0xFFFF), (A_PH1, ph >> 16),
        (A_HIGH, hi), (A_LOW, lo),
        (A_DUTY0, dw & 0xFFFF), (A_DUTY1, dw >> 16), (A_RSLOPE, rs & 0xFFFF), (A_FSLOPE, fs & 0xFFFF),
    ]


START = [(A_SWITCH, 1), (A_ENABLE, 0x0003), (A_SWITCH, 0)]
STOP = [(A_ENABLE, 0x0002)]
#: after STOP the output holds the programmed LOW level (measured 2026-10-31); LOW = 0 parks it at 0 V. The next
#: program() rewrites LOW.
PARK = [(A_LOW, 0)]


def dc(level):
    """Writes for a steady DC output: stopped, the generator holds LOW (not a DSWave feature; measured)."""
    if not 0.0 <= level <= V_MAX + 1e-9:
        raise DscopeError('INVALID_ARG', 'DC level must be 0 .. 3.0 V')
    from .awg_modes import level_code
    return STOP + [(A_LOW, level_code(level))]


def apply(transport, writes):
    for addr, val in writes:
        transport.write(REG_ADDR, addr)
        transport.write(REG_DATA, val)


CODES_PER_V = 1023 * 0.91525 / 6.0       # HIGH/LOW code per volt (DSWave: trunc(uV / 6e6 * 0.91525 * 1023))


def describe(wave, freq, high, low, duty, phase):
    from .awg_modes import level_code
    fcw = int(math.floor(freq * 40))
    if wave == 'noise':
        return {'wave': wave, 'high_v': high, 'low_v': low, 'note': 'Gaussian-like noise around (low+high)/2 with sigma '
                'about 2.8 % of (high-low), peaks about +-0.2 x (high-low), flat to ~2 MHz; frequency is ignored'}
    return {'wave': wave, 'freq_hz': freq, 'freq_actual_hz': fcw / 40.0, 'high_v': round(level_code(high) / CODES_PER_V, 4),
            'low_v': round(level_code(low) / CODES_PER_V, 4), 'duty_pct': duty, 'phase_deg': phase}
