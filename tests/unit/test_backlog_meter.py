"""The loop-mode backlog estimate (logger.BacklogMeter): what the scope has acquired but the host has not read."""
import pytest

from openm454 import logger

MB = 1e6


def _feed(m, seconds, rate, step=0.01, t0=0.0, received=None):
    """Advance the meter for some seconds at rate MB/s of production and received MB/s of reading."""
    t, got = t0, (0.0 if received is None else received * t0)
    while t < t0 + seconds:
        t += step
        got += received * step if received is not None else rate * step
        m.update(t, got)
    return t, got


def test_the_backlog_grows_at_the_difference_and_the_peak_is_bounded():
    """The first DRIFT_BAND is absorbed by the clock-drift baseline (that is its job); above it the slope is D - R."""
    m = logger.BacklogMeter(400 * MB, 0.0)
    t1, got1 = _feed(m, 1.0, 400 * MB, received=287 * MB)
    b1 = m.backlog
    t2, got2 = _feed(m, 1.0, 400 * MB, t0=t1, received=287 * MB)
    assert b1 > 16 * 2 ** 20, 'one second at D - R is already above the drift band'
    assert (m.backlog - b1) / (t2 - t1) == pytest.approx(113 * MB, rel=0.01)
    assert m.max == m.backlog


def test_nothing_is_added_after_the_stop():
    """After the stop the device produces nothing more: the reads that still arrive are the drain, so the model must
    freeze its production term instead of growing the peak by (D - R) x drain."""
    m = logger.BacklogMeter(400 * MB, 0.0)
    t, got = _feed(m, 1.2, 400 * MB, received=287 * MB)
    peak = m.max
    m.stop(t)
    for step in range(1, 51):                      # the host keeps reading for another 0.5 s
        m.update(t + step * 0.01, got + 287 * MB * step * 0.01)
    assert m.max == peak, 'the peak must not grow while draining'
    assert m.backlog < peak, 'the backlog drains'


def test_a_reading_host_stays_at_zero():
    m = logger.BacklogMeter(150 * MB, 0.0)
    _feed(m, 1.0, 150 * MB, received=150 * MB)
    assert m.max == 0