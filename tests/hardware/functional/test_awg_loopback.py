"""The AWG into an analog input, wired as tests/hardware/bench.json declares (`hw`; never in CI).

The declaration is the oracle for the wiring: what the capture is configured for (probe, volts/div, offset) and
what it must then show (frequency, levels, duty) both come from bench.json, so a bench wired differently fails here
and not as a mysterious number somewhere else.
"""
import statistics
import time

import pytest

pytestmark = pytest.mark.hw

RATE = 10e6          # 10 MSa/s: 100 samples per period of the declared 1 kHz square
DURATION = 2e-3      # two periods per frame, so every frame carries both levels
FRAMES = 64


def test_the_connected_scope_is_the_one_the_bench_file_declares(bench, scope):
    want = bench.d['scope']['model']
    got = scope.info()['device']['model']
    assert got == want, 'bench.json declares %r, the connected scope reports %r' % (want, got)


def test_the_declared_link_shows_the_signal_it_declares(bench, scope):
    link = bench.link('awg_to_a0')
    link.program(scope)
    cap = scope.capture(link.settings(DURATION, RATE))
    link.assert_shows_the_declared_signal(cap.measure(link.channel))


def test_a_repeat_run_keeps_one_arrival_time_per_frame(bench, scope, tmp_path):
    """The host stamps are the point of the feature: N frames, monotone, inside the run, and never read faster
    than they can be recorded. They also have to survive the .npz, which is where a later analysis reads them."""
    link = bench.link('awg_to_a0')
    link.program(scope)
    st = link.settings(DURATION, RATE)
    st.repeat = FRAMES
    t0 = time.time()
    cap = scope.capture(st)
    span = time.time() - t0
    stamps = [f.get('host_s') for f in cap.frames]
    assert all(s is not None for s in stamps), 'a repeat run has to record when every frame arrived'
    assert len(stamps) == len(cap.frames) == FRAMES
    assert stamps == sorted(stamps), 'arrival times have to be monotone'
    assert 0.0 <= stamps[0] <= stamps[-1] <= span, 'every stamp has to lie inside the run'
    record = cap.meta['acq']['duration_s']
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert statistics.median(gaps) >= record, 'the median frame is read faster than it is recorded'
    from openm454.capture import load_capture
    back = load_capture(cap.save(str(tmp_path / 'repeat.npz')))
    assert back.meta['frame_host_s'] == pytest.approx(stamps, rel=1e-8, abs=0)


def test_every_frame_carries_the_declared_signal(bench, scope):
    """The frames of a repeat run are separate acquisitions, so each one must show the signal, not a stale copy."""
    link = bench.link('awg_to_a0')
    link.program(scope)
    st = link.settings(DURATION, RATE)
    st.repeat = FRAMES
    cap = scope.capture(st)
    ch, sig, tol = link.channel, link['signal'], link['tolerance']
    sc = cap.scale(ch)
    code_v = sc['volts_per_code'] * sc.get('probe', 1.0)
    swings = [int(f['analog'][ch].max()) - int(f['analog'][ch].min()) for f in cap.frames]
    median = statistics.median(swings)
    # the absolute level is the capture test's job (it owns the declared tolerance); here the question is whether
    # every frame carries the same signal or some are stale/dead copies of it
    assert median * code_v >= (sig['high_v'] - sig['low_v']) - tol['level_v'], \
        'the frames swing %.3g V, bench.json declares %.3g V: check the declared levels' % (
            median * code_v, sig['high_v'] - sig['low_v'])
    for i, swing in enumerate(swings):
        assert abs(swing - median) <= 0.2 * median, 'frame %d: swing %d codes, median over the run %d' % (
            i, swing, median)