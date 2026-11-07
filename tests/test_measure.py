import numpy as np
from openm454 import measure


def square(f=1000.0, rate=1e6, n=20000, hi=3.0, lo=0.0, duty=0.5, noise=0.01, seed=0):
    t = np.arange(n) / rate
    v = np.where((t * f) % 1.0 < duty, hi, lo).astype(float)
    return v + np.random.default_rng(seed).normal(0, noise, n)


def test_square_levels_freq_duty():
    m = measure.analog(square(duty=0.25), 1e6)
    assert abs(m['freq_hz'] - 1000) < 1
    assert abs(m['duty_pct'] - 25) < 1
    assert abs(m['vtop_v'] - 3.0) < 0.05 and abs(m['vbase_v']) < 0.05


def test_sine_fft_fallback_and_flat():
    t = np.arange(4096) / 1e6
    m = measure.analog(np.sin(2 * np.pi * 1234 * t), 1e6)
    assert abs(m['freq_hz'] - 1234) < 5
    flat = measure.analog(np.zeros(5000) + 0.01 * np.random.default_rng(1).normal(size=5000), 1e6, lsb=0.034)
    assert flat['freq_hz'] is None


def test_digital_with_glitch():
    b = (square(noise=0) > 1.5).astype(np.uint8)
    b[5100] ^= 1
    m = measure.digital(b, 1e6)
    assert m['state'] == 'toggling' and abs(m['freq_hz'] - 1000) < 1 and m.get('runts', 0) >= 1
    assert measure.digital(np.zeros(100, np.uint8), 1e6)['state'] == 'low'


def test_digital_edge_chatter_is_one_edge():
    """Slow edge through the comparator at a high rate: 3-5 transitions within a few samples (hardware, 400 MSa/s)."""
    b = (square(f=1000, rate=4e6, n=20000, noise=0) > 1.5).astype(np.uint8)
    rises = np.flatnonzero(np.diff(b.astype(np.int8)) > 0) + 1
    for r in rises:
        b[r:r + 6] = [1, 0, 1, 0, 0, 1]                 # chatter right after each rising edge
    m = measure.digital(b, 4e6)
    assert abs(m['freq_hz'] - 1000) < 0.5 and abs(m['duty_pct'] - 50) < 0.5
    assert m['glitch_edges'] == 4 * len(rises) and 'runts' not in m


def test_digital_isolated_pulse_and_fast_clock_are_kept():
    b = np.zeros(2_000_000, np.uint8)
    b[1_000_000:1_000_040] = 1                          # a 100 ns strobe at 400 MSa/s
    m = measure.digital(b, 400e6)
    assert m['state'] == 'toggling' and m['edges'] == 2 and 'glitch_edges' not in m
    clk = np.tile(np.array([0, 0, 1, 1], np.uint8), 10000)     # 100 MHz at 400 MSa/s
    m = measure.digital(clk, 400e6)
    assert abs(m['freq_hz'] - 100e6) < 1 and 'glitch_edges' not in m


def test_noise_has_no_frequency():
    lsb = 8 / 235
    rng = np.random.default_rng(1)
    for sig in (0.5, 1, 2, 4):
        v = np.round(rng.normal(0, sig, 100000)) * lsb + 1.0
        assert measure.analog(v, 1e6, lsb=lsb)['freq_hz'] is None


def test_fast_sine_uses_the_fft():
    """10 samples per period at an exact ratio: the histogram levels miss the peaks (hardware, 2026-10-31)."""
    lsb = 8 / 235
    t = np.arange(5056) / 100e3
    v = np.round((1 + np.sin(2 * np.pi * 10e3 * t)) / lsb) * lsb
    m = measure.analog(v, 100e3, lsb=lsb)
    assert abs(m['freq_hz'] - 10e3) < 5 and m['freq_method'] == 'fft'


def test_narrow_pwm_keeps_its_frequency():
    t = np.arange(200000) / 1e6
    m = measure.analog((np.mod(t * 1000, 1) < 0.01) * 2.8, 1e6, lsb=8 / 235)
    assert abs(m['freq_hz'] - 1000) < 0.5 and abs(m['duty_pct'] - 1.0) < 0.2 and not m.get('noise_like')
