"""Digital edge-trigger check: did a rise/fall trigger on a digital line fire on the requested edge?

The digital inputs are plain comparators without hysteresis. A slow edge chatters at the threshold (measured
2026-11-07 on the ~8 us RC rise of the 1 kHz calibration square: ~5 cycles within ~50 ns), and the hardware edge
trigger fires on any 1->0 (0->1) step of that chatter: a falling-edge trigger then lands inside a rising edge and the
head still reports a valid trigger (D5 fall: 13/40 at 100 MSa/s, 20/40 at 400 MSa/s; rising edges and analog
triggers never misfired). edge_check() compares the settled levels just before and just after t = 0 with the
requested edge: runs of identical samples shorter than the settle time are chatter and are ignored.
"""
import math

import numpy as np

SETTLE_S = 200e-9      # a settled level lasts at least this long ...
MIN_RUN = 3            # ... and at least this many samples
TOL_S = 20e-9          # trigger point uncertainty around t = 0 (at least TOL_MIN samples)
TOL_MIN = 2
WINDOW_S = 10e-6       # searched on each side of t = 0 (at least WINDOW_RUNS settled runs, at most WINDOW_MAX samples)
WINDOW_RUNS = 16
WINDOW_MAX = 1 << 16
CHATTER_MAX_S = 200e-9  # the unsettled stretch between the two settled levels counts as chatter only up to this long
                        # (at least 2 samples). Measured 2026-11-07 on misfires (calibration square): stretches up to
                        # 120 ns at 50-400 MSa/s. Individual runs are not limited: chatter near the sample rate aliases
                        # into runs of any length (62.5 ns at 80 MSa/s). Real data puts at least two bit periods there
                        # (250 ns for 8 MHz SPI); data faster than ~10 Mbit/s right at the trigger can look like chatter

HINT = ('the digital inputs have no hysteresis: a slow edge chatters at the threshold and can fire a trigger on the '
        'opposite edge; trigger on the other edge, on an analog channel (its trigger has a noise margin), or give the '
        'line a faster edge')


def checked_edge(edge):
    """'rise' / 'fall' for the edge triggers this check applies to; None for 'any' and the pulse triggers."""
    e = (edge or '').lower()
    return 'rise' if e in ('rise', 'rising') else 'fall' if e in ('fall', 'falling') else None


def settled_levels(bits, idx, rate, settle_s=SETTLE_S):
    """(level before, level after) the trigger at sample idx: the last run of identical samples lasting at least
    max(MIN_RUN samples, settle_s) that starts before idx - tol, and the first one that ends after idx + tol, within
    a bounded window around idx. Either is None when the window has no such run (too few samples, a fast signal,
    chatter all over the window)."""
    n = len(bits)
    if not rate or rate <= 0 or n < 2:
        return None, None
    run = max(MIN_RUN, int(math.ceil(settle_s * rate - 1e-9)))
    tol = max(TOL_MIN, int(round(TOL_S * rate)))
    w = min(max(WINDOW_RUNS * run, int(math.ceil(WINDOW_S * rate))), WINDOW_MAX)
    lo, hi = max(0, idx - w), min(n, idx + w)
    if hi - lo < 2:
        return None, None
    seg = np.asarray(bits[lo:hi]) != 0
    cut = np.flatnonzero(seg[1:] != seg[:-1]) + 1
    starts = np.concatenate(([0], cut))
    ends = np.concatenate((cut, [len(seg)]))
    vals = seg[starts]
    starts, ends = starts + lo, ends + lo
    long_ = (ends - starts) >= run
    b = np.flatnonzero(long_ & (starts < idx - tol))
    a = np.flatnonzero(long_ & (ends > idx + tol))
    if b.size and a.size and a[0] > b[-1] + 1:
        # what lies between the two settled runs must be as short as comparator chatter; a longer stretch is real
        # data (bits), and the edge cannot be judged this way
        if starts[a[0]] - ends[b[-1]] > max(2, CHATTER_MAX_S * rate):
            return None, None
    return (int(vals[b[-1]]) if b.size else None), (int(vals[a[0]]) if a.size else None)


def edge_check(bits, idx, rate, edge, settle_s=SETTLE_S):
    """'ok' (settled levels show the requested edge), 'mismatch' (they show the opposite edge: a misfire),
    'inconclusive' (a settled level is missing on one side, or both are equal: a pulse shorter than the settle
    time) or 'unchecked' (edge 'any' or a pulse trigger)."""
    want = checked_edge(edge)
    if want is None:
        return 'unchecked'
    if bits is None:
        return 'inconclusive'
    before, after = settled_levels(bits, int(idx), rate, settle_s)
    if before is None or after is None or before == after:
        return 'inconclusive'
    return 'ok' if ('rise' if after > before else 'fall') == want else 'mismatch'
