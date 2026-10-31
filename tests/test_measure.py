import numpy as np
from dscope import measure


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
