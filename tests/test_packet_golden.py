"""Golden tests for dscope.packet against the recorded DSWave runs (no hardware).

Recordings: re/traces/S01_warm/scen/*.json + bulk_in.bin (local only, not in git); the trace-based
tests are skipped when they are absent.  The synthetic round-trip tests always run.

Bench fixture during every recording:
  A0 = scope AWG output (flat 0 V unless an awg_* run), A1/A3 = 1 kHz ~2.83 Vpp square (1x probe),
  A2 = same square through a 10x probe (~0.28 V), D0/D2/D5/D7 = same square, D1/D3/D4/D6 floating (0).
"""
import functools
import math
import os
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "re", "tools"))

from dscope import packet as P  # noqa: E402

try:
    import scen_load  # noqa: E402
    HAVE_TRACES = os.path.exists(os.path.join(scen_load.SESSION, "bulk_in.bin"))
except ImportError:  # pragma: no cover
    scen_load, HAVE_TRACES = None, False

needs_traces = pytest.mark.skipif(not HAVE_TRACES, reason="recorded traces not present")

CODES_PER_V = P.CODES_PER_DIV          # at 1 V/div
SWING_1X = 84.0                        # A1/A3 square swing at 1 V/div (codes)
SWING_10X = 8.4                        # A2 (10x probe)
SQUARE_CH = (0, 2, 5, 7)               # digital inputs wired to the square
FLOAT_CH = (1, 3, 4, 6)


# ------------------------------------------------------------------------------------ signal helpers
def levels(x):
    """Low/high level of a two-level signal (medians of the two halves of the 1..99 % range)."""
    x = np.asarray(x, float)
    lo_p, hi_p = np.percentile(x, [1, 99])
    mid = (lo_p + hi_p) / 2
    lo = float(np.median(x[x < mid])) if (x < mid).any() else float(lo_p)
    hi = float(np.median(x[x >= mid]))
    return lo, hi


def schmitt(x, lo, hi):
    """Rising/falling edge indices with hysteresis (index of the sample that completes the edge).
    A window that starts between the thresholds takes its initial state from the mid level."""
    x = np.asarray(x)
    c = np.full(len(x), -1, np.int8)
    c[x >= hi] = 1
    c[x <= lo] = 0
    if len(x) and c[0] < 0:
        c[0] = 1 if x[0] >= (lo + hi) / 2 else 0
    idx = np.nonzero(c >= 0)[0]
    st = c[idx]
    ch = np.nonzero(np.diff(st))[0] + 1
    return idx[ch][st[ch] == 1], idx[ch][st[ch] == 0]


def refine(x, i, thr, up):
    """Walk back from an edge to the first sample of its final crossing of thr."""
    j = int(i)
    if up:
        while j > 0 and x[j - 1] >= thr:
            j -= 1
    else:
        while j > 0 and x[j - 1] <= thr:
            j -= 1
    return j


def analog_edges(x, thr, hyst):
    r, f = schmitt(x, thr - hyst, thr + hyst)
    return (np.array([refine(x, i, thr, True) for i in r], int),
            np.array([refine(x, i, thr, False) for i in f], int))


def digital_edges(d, gap):
    """Deglitched edges: transitions closer than gap samples form one edge (its first transition)."""
    t = np.nonzero(np.diff(d.astype(np.int8)))[0] + 1
    if len(t) == 0:
        return np.array([], int), np.array([], int)
    groups = np.split(t, np.nonzero(np.diff(t) > gap)[0] + 1)
    r, f = [], []
    for g in groups:
        if d[g[0] - 1] == d[g[-1]]:
            continue
        (r if d[g[-1]] else f).append(g[0])
    return np.array(r, int), np.array(f, int)


def square_period(x, rate):
    """(period_s, duty, tolerance) of a two-level analog signal; see period_from_edges."""
    lo, hi = levels(x)
    r, f = schmitt(x, lo + 0.4 * (hi - lo), lo + 0.6 * (hi - lo))
    return period_from_edges(r, f, rate)


def digital_period(d, rate, stale=0):
    r, f = digital_edges(d[stale:], max(4, int(60e-9 * rate)))
    return period_from_edges(r, f, rate)


def period_from_edges(r, f, rate):
    """Tolerance 1 % with >= 2 rising edges, 1.5 % when the window holds a single period
    (period = 2 x mean half period; the fixture duty is 49.7 %, duty then unknown)."""
    if len(r) >= 2:
        T = (r[-1] - r[0]) / (len(r) - 1)
        highs = [f[f > a][0] - a for a in r[:-1] if (f > a).any()]
        return T / rate, (np.mean(highs) / T if highs else None), 0.01
    e = np.sort(np.r_[r, f])
    if len(e) < 2:
        return None, None, None
    return 2 * np.mean(np.diff(e)) / rate, None, 0.015


def sine_fit_freq(x, rate, f0, span=0.05, steps=401):
    """Least-squares sine fit; scan f in f0*(1 +- span) and refine. Returns best frequency."""
    x = np.asarray(x, float)
    step = max(1, len(x) // 20000)
    x = x[::step] - x.mean()
    t = np.arange(len(x)) * step / rate

    def resid(f):
        w = 2 * np.pi * f * t
        M = np.stack([np.sin(w), np.cos(w), np.ones_like(t)], 1)
        c, res, *_ = np.linalg.lstsq(M, x, rcond=None)
        return float(np.sum((M @ c - x) ** 2))

    fs = np.linspace(f0 * (1 - span), f0 * (1 + span), steps)
    rs = [resid(f) for f in fs]
    k = int(np.argmin(rs))
    lo, hi = fs[max(k - 1, 0)], fs[min(k + 1, len(fs) - 1)]
    for _ in range(40):                      # golden-section refine
        m1, m2 = lo + 0.382 * (hi - lo), lo + 0.618 * (hi - lo)
        if resid(m1) < resid(m2):
            hi = m2
        else:
            lo = m1
    return (lo + hi) / 2


# ------------------------------------------------------------------------------------- run loading
RUNS = scen_load.run_names() if HAVE_TRACES else []
CAPTURE_RUNS = []
if HAVE_TRACES:
    import json
    for _n in RUNS:
        _r = json.load(open(os.path.join(scen_load.SESSION, "scen", _n + ".json")))
        if any(b["pipe"] == 0x86 and b["actual"] for b in _r.get("bulk_in", [])):
            CAPTURE_RUNS.append(_n)
AWG_RUNS = [n for n in CAPTURE_RUNS if n.startswith("awg_")]
REPEAT_FRAMES = 8


@functools.lru_cache(maxsize=None)
def get(name):
    """(run, layout, decoded) -- repeat mode: first REPEAT_FRAMES frames loaded, first decoded."""
    L0 = P.layout_from_regs(scen_load.run_regs(name))
    if L0.hw_mode == P.MODE_REPEAT:
        run = scen_load.load_run(name, max_bytes=L0.frame_bytes * REPEAT_FRAMES)
    else:
        need = (L0.acq_bytes or 0) + P.TAIL_BYTES
        run = scen_load.load_run(name, max_bytes=need)
    if L0.hw_mode == P.MODE_REPEAT:
        dec = P.decode_frames(run["stream"], L0)[0]
    else:
        dec = P.decode(run["stream"], L0)
    return run, L0, dec


def cfg(run, ch):
    vdiv = scen_load.scpi_value(run, ":ANALOG:CHANNEL:Vdiv", ch) / 1e6
    voff = scen_load.scpi_value(run, ":ANALOG:CHANNEL:Offset", ch)
    inv = scen_load.scpi_value(run, ":ANALOG:CHANNEL:Invert", ch)
    ac = scen_load.scpi_value(run, ":ANALOG:CHANNEL:Coupling", ch)
    return vdiv, voff, bool(inv), bool(ac)


def awg_cfg(name):
    import json
    for f in ("corpus3.json", "corpus1.json", "corpus2.json", "corpus4.json"):
        p = os.path.join(ROOT, "re", "scenarios", f)
        if os.path.exists(p):
            for it in json.load(open(p)):
                if it["name"] == name and "awg" in it:
                    return it["awg"]
    return None


# =================================================================================== structural tests
@needs_traces
@pytest.mark.parametrize("name", CAPTURE_RUNS)
def test_layout_matches_hal(name):
    """Rates equal the HAL's SCPI answers; block size and primary block samples reproduce the HAL's
    own 'send samples' arithmetic for the first data transfer; tail sits at acq_bytes."""
    run, L, dec = get(name)
    q = run["query"]
    assert L.analog_rate == q[":ANALOG:SAMPLE:Rate?"]
    assert L.digital_rate == q[":DIGITAL:SAMPLE:Rate?"]
    # block multipliers from registers == gcd rule (Mso_ComputeGeometry)
    assert (L.amtxs, L.dmtxs) == P._mtxs(L.analog_rate, L.digital_rate, L.analog, L.digital)
    first = run["transfers"][0]
    if L.hw_mode == P.MODE_REPEAT:
        first -= P.TAIL_BYTES
    exp_send = min(first, L.acq_bytes) // L.block_bytes * L.primary_block_samples
    assert run["send_samples"][0] == exp_send
    assert run["transfers"][0] == P.hal_transfer_bytes(L)
    assert dec["tail_offset"] == L.acq_bytes
    t = dec["tail"]
    assert t["id"] == P.TAIL_ID
    assert L.acq_words - 16 < t["valid_words"] <= L.acq_words
    assert t["valid_words"] % 16 == 0
    assert dec["nblocks"] == t["valid_words"] * 4 // L.block_bytes
    na = len(L.analog) and dec["nblocks"] * L.analog_block_samples
    nd = len(L.digital) and dec["nblocks"] * L.digital_block_samples
    assert all(len(v) == na for v in dec["analog"].values())
    assert all(len(v) == nd for v in dec["digital"].values())
    # analog and digital cover the same time span
    if L.analog and L.digital:
        assert math.isclose(na / L.analog_rate, nd / L.digital_rate)


@needs_traces
@pytest.mark.parametrize("name", CAPTURE_RUNS)
def test_head_tail_trigger_consistent(name):
    run, L, dec = get(name)
    h = P.parse_head(run["head"])
    ti = P.trigger_index(h, L)
    hal = run["trigger_samples"][0]
    if ti is None:
        assert hal == 0
    else:
        assert ti == hal
    if L.hw_mode != P.MODE_REPEAT:
        assert dec["tail"]["trig_word"] == h["trig_word"]
    assert L.pretrig == P.pretrigger_offset(scen_load.scpi_value(run, ":TRIGger:SAMPLEindex"), L)


@needs_traces
@pytest.mark.parametrize("name", CAPTURE_RUNS)
def test_tail_minmax_matches_data(name):
    """The FPGA's own per-channel min/max (tail records, indexed by physical channel) equal the
    min/max of the decoded valid samples -- an fixture-independent check of the analog de-interleave
    and of the valid-length rule; disabled channels report min=255, max=0."""
    run, L, dec = get(name)
    for k, rec in enumerate(dec["tail"]["channels"]):
        if k in dec["analog"]:
            a = dec["analog"][k]
            assert (rec["min"], rec["max"]) == (int(a.min()), int(a.max())), (k, rec)
        else:
            assert (rec["min"], rec["max"]) == (255, 0), (k, rec)


@needs_traces
def test_hal_nsamples_view():
    """decode(nsamples=HAL count) returns exactly the HAL's sample count (filler included)."""
    run, L, _ = get("base")
    dec = P.decode(run["stream"], L, nsamples=run["send_samples"][0])
    assert dec["analog_samples"] == run["send_samples"][0] == 100736
    run, L, _ = get("ach_a1")
    dec = P.decode(run["stream"], L, nsamples=run["send_samples"][0])
    assert dec["analog_samples"] == 402944 and dec["digital_samples"] == 402944 // 4


# ====================================================================================== analog truth
def _expected_levels(run, ch):
    vdiv, voff, inv, ac = cfg(run, ch)
    sw = (SWING_10X if ch == 2 else SWING_1X) / vdiv
    lo, hi = (voff - sw, voff) if inv else (voff, voff + sw)
    return lo, hi, sw, ac


@needs_traces
@pytest.mark.parametrize("name", CAPTURE_RUNS)
def test_analog_channel_identity_and_swing(name):
    """A0 flat at the offset (unless AWG), A1/A3 ~84 codes x (1 V/div / vdiv), A2 ~1/10 of that;
    low/high at the offset (clipped to 0..255); overdriven channels (> ~1.25x full scale) skipped."""
    run, L, dec = get(name)
    checked = 0
    for ch, a in dec["analog"].items():
        vdiv, voff, inv, ac = cfg(run, ch)
        if ch == 0:
            if name.startswith("awg_"):
                continue
            if vdiv >= 0.5:
                lo, hi = np.percentile(a, [1, 99])
                assert hi - lo <= 3 and abs(np.median(a) - min(max(voff, 0), 255)) <= 2.5, (lo, hi)
                checked += 1
            continue
        elo, ehi, sw, ac = _expected_levels(run, ch)
        if elo < -40 or ehi > 300:
            continue                                     # front end overdriven -> not a decoder check
        lo, hi = levels(a)
        tol = max(3.0, 0.08 * sw)
        if ac:                                           # AC coupling: only the swing is defined
            assert abs((hi - lo) - sw) <= tol, (ch, lo, hi, sw)
        else:
            assert abs(lo - np.clip(elo, 0, 255)) <= tol, (ch, lo, elo)
            assert abs(hi - np.clip(ehi, 0, 255)) <= tol, (ch, hi, ehi)
        checked += 1
    if L.analog and any(cfg(run, c)[0] >= 0.5 for c in L.analog):
        assert checked > 0


@needs_traces
@pytest.mark.parametrize("name", CAPTURE_RUNS)
def test_square_frequency_and_duty(name):
    """A1/A3 (and A2 when resolvable) are the 1 kHz square: period x 1/rate = 1 ms, duty ~50 %."""
    run, L, dec = get(name)
    n = 0
    for ch in (1, 3, 2):
        if ch not in dec["analog"]:
            continue
        lo, hi, sw, ac = _expected_levels(run, ch)
        if sw < 15 or lo < -40 or hi > 300 or min(hi, 255) - max(lo, 0) < 15:
            continue                                      # too small / overdriven / clipped away
        T, duty, tol = square_period(dec["analog"][ch], L.analog_rate)
        assert T is not None, ch
        assert abs(T - 1e-3) <= tol * 1e-3, (ch, T)
        if duty is not None:
            assert abs(duty - 0.5) <= 0.02, (ch, duty)
        n += 1
    if n == 0:
        pytest.skip("no analog square channel resolvable in this configuration")


# ===================================================================================== digital truth
@needs_traces
@pytest.mark.parametrize("name", CAPTURE_RUNS)
def test_digital_channels(name):
    """D1/D3/D4/D6 constant 0; D0/D2/D5/D7 toggle at 1 kHz, edges in step with A1 (or A3, or D0)."""
    run, L, dec = get(name)
    if not dec["digital"]:
        pytest.skip("no digital channels")
    rate = L.digital_rate
    st = dec["digital_stale"]                       # leading samples hold the previous level
    assert st == P.DIGITAL_STALE[L.dfold]
    thr_v = scen_load.scpi_value(run, ":DIGITAL:Threshold") / 1e6
    if thr_v < 0.2:
        pytest.skip("threshold %.2f V: inputs at 0 V chatter" % thr_v)
    for ch in FLOAT_CH:
        if ch in dec["digital"]:
            assert not dec["digital"][ch][st:].any(), "D%d should read 0" % ch
    if thr_v > 2.6:                                 # above the square's high level (~2.83 V)
        for ch in SQUARE_CH:
            if ch in dec["digital"]:
                assert not dec["digital"][ch][st:].any(), "D%d should stay 0 at %.1f V" % (ch, thr_v)
        return
    gap = max(4, int(60e-9 * rate))
    ref = None
    for ach in (1, 3):
        if ach in dec["analog"]:
            vdiv, voff, inv, ac = cfg(run, ach)
            if vdiv == 1.0 and not inv and not ac and voff + SWING_1X < 255:
                a = dec["analog"][ach].astype(float)
                thr = voff + thr_v * CODES_PER_V           # the digital threshold, on the analog trace
                ref = ("A%d" % ach, analog_edges(a, thr, 4), L.analog_rate, len(a))
                break
    if ref is None and 0 in dec["digital"]:
        r0, f0 = digital_edges(dec["digital"][0][st:], gap)
        ref = ("D0", (r0 + st, f0 + st), rate, len(dec["digital"][0]))
    tol = max(300e-9, 2.0 / rate, 2.0 / (ref[2] if ref else rate))
    for ch in SQUARE_CH:
        if ch not in dec["digital"]:
            continue
        d = dec["digital"][ch]
        r, f = digital_edges(d[st:], gap)
        r, f = r + st, f + st
        assert len(r) + len(f) >= 1, "D%d does not toggle" % ch
        T, duty, ptol = period_from_edges(r, f, rate)
        if T is not None:
            assert abs(T - 1e-3) <= ptol * 1e-3, (ch, T)
        if ref is None:
            continue
        rname, (ra, fa), arate, alen = ref
        span = alen / arate
        for ea, ed in ((ra, r), (fa, f)):
            ta, td = ea / arate, ed / rate
            # every interior reference edge has a digital edge of the same polarity close by
            for x in ta[(ta > 20e-6) & (ta < span - 20e-6)]:
                assert len(td) and np.min(np.abs(td - x)) <= tol, ("D%d vs %s" % (ch, rname), x)


# ======================================================================================== triggering
TRIGGERED = (["te_a1_normal", "repeat_mode"] + [n for n in AWG_RUNS if n != "awg_noise"]) if HAVE_TRACES else []


def _level_crossing_near(x, t, lvl, tol=2):
    for i in range(max(1, t - tol), min(len(x), t + tol + 1)):
        if x[i - 1] < lvl <= x[i]:
            return True
    return False


@needs_traces
@pytest.mark.parametrize("name", TRIGGERED)
def test_trigger_edge_position(name):
    """The trigger source rises through TRIG_LEVEL at trigger_index(head) +- 2 samples, and the
    trigger window [trig - SAMPLEindex, + depth) has the edge at SAMPLEindex."""
    run, L, dec = get(name)
    h = P.parse_head(run["head"])
    assert h["triggered"]
    ti = P.trigger_index(h, L)
    ch, src = scen_load.scpi_args(run, ":TRIGger:Channel")
    assert src == 1 and L.trig_domain == "analog"        # analog trigger in all these runs
    lvl = run["regs"][0x35]
    x = dec["analog"][ch].astype(int)
    assert _level_crossing_near(x, ti, lvl), (ti, x[ti - 4:ti + 4])
    sidx = scen_load.scpi_value(run, ":TRIGger:SAMPLEindex")
    depth = run["query"][":ANALOG:SAMPLE:Depth?"]
    w = P.trigger_window(dec, L, ti, sidx, depth)
    xa = w["analog"][ch].astype(int)
    assert len(xa) == depth
    assert _level_crossing_near(xa, sidx, lvl)
    for dch, dv in w["digital"].items():
        assert len(dv) == depth * L.digital_rate / L.analog_rate


@needs_traces
def test_repeat_mode_frames():
    """HW_BASE_MODE 1: frames of acq_bytes + tail back to back; each frame's tail trigger puts the
    A1 rising edge (level 169) at its index; the indices match the HAL log frame by frame."""
    run, L, _ = get("repeat_mode")
    assert L.hw_mode == P.MODE_REPEAT and L.frame_bytes == 507904
    assert all(s == L.frame_bytes for s in run["transfers"][:REPEAT_FRAMES])
    frames = P.decode_frames(run["stream"], L)
    assert len(frames) == REPEAT_FRAMES
    lvl = run["regs"][0x35]
    hal = run["trigger_samples"]
    for k, fr in enumerate(frames):
        ti = fr["trig_index"]
        assert ti is not None and ti == hal[k]
        x = fr["analog"][1].astype(int)
        assert _level_crossing_near(x, ti, lvl), (k, ti)
        assert fr["nblocks"] == 1565
    assert frames[0]["trig_index"] == P.trigger_index(P.parse_head(run["head"]), L)


# ===================================================================================== fold / channels
FOLD_RUNS = ["ach_a0", "ach_a1", "ach_a3", "ach_a01", "ach_a13", "ach_a012", "ach_anone", "dch_d0", "dch_d01",
             "dch_d02", "dch_d0123", "dch_d5", "dch_dnone", "only_a1_d0", "only_d0", "te_only_a1_fold4",
             "te_only_a1_d0_fold", "te_only_d0", "te_d2_rise_nod1"]
EXPECT = {  # name: (analog chans, digital chans, arate, drate, amtxs, dmtxs, block bytes)
    "ach_a0": ((0,), tuple(range(8)), 400e6, 100e6, 4, 1, 320),
    "ach_a1": ((1,), tuple(range(8)), 400e6, 100e6, 4, 1, 320),
    "ach_a3": ((3,), tuple(range(8)), 400e6, 100e6, 4, 1, 320),
    "ach_a01": ((0, 1), tuple(range(8)), 200e6, 100e6, 2, 1, 320),
    "ach_a13": ((1, 3), tuple(range(8)), 200e6, 100e6, 2, 1, 320),
    "ach_a012": ((0, 1, 2), tuple(range(8)), 100e6, 100e6, 1, 1, 256),
    "ach_anone": ((), tuple(range(8)), 100e6, 100e6, 1, 1, 64),
    "dch_d0": ((0, 1, 2, 3), (0,), 100e6, 400e6, 1, 4, 288),
    "dch_d01": ((0, 1, 2, 3), (0, 1), 100e6, 400e6, 1, 4, 320),
    "dch_d02": ((0, 1, 2, 3), (0, 2), 100e6, 400e6, 1, 4, 320),
    "dch_d0123": ((0, 1, 2, 3), (0, 1, 2, 3), 100e6, 200e6, 1, 2, 320),
    "dch_d5": ((0, 1, 2, 3), (5,), 100e6, 400e6, 1, 4, 288),
    "dch_dnone": ((0, 1, 2, 3), (), 100e6, 100e6, 1, 1, 256),
    "only_a1_d0": ((1,), (0,), 400e6, 400e6, 1, 1, 72),
    "only_d0": ((), (0,), 100e6, 400e6, 1, 1, 8),
    "te_only_a1_fold4": ((1,), tuple(range(8)), 400e6, 100e6, 4, 1, 320),
    "te_only_a1_d0_fold": ((1,), (0,), 400e6, 400e6, 1, 1, 72),
    "te_only_d0": ((), (0,), 100e6, 400e6, 1, 1, 8),
    "te_d2_rise_nod1": ((0, 1, 2, 3), (0, 2, 5, 7), 100e6, 200e6, 1, 2, 320),
}


@needs_traces
@pytest.mark.parametrize("name", FOLD_RUNS)
def test_fold_layouts(name):
    """Single-channel / folded runs: right channels, rates and block geometry; every enabled
    square channel shows a 1 ms period at its own sample rate; A0 stays flat."""
    run, L, dec = get(name)
    an, dg, ar, dr, am, dm, bb = EXPECT[name]
    assert (L.analog, L.digital, L.analog_rate, L.digital_rate, L.amtxs, L.dmtxs, L.block_bytes) == \
        (an, dg, ar, dr, am, dm, bb)
    assert L.afold * 100e6 == ar or not an
    for ch, a in dec["analog"].items():
        if ch == 0:
            assert np.ptp(np.percentile(a, [1, 99])) <= 3
            continue
        T, duty, tol = square_period(a, L.analog_rate)
        assert abs(T - 1e-3) <= tol * 1e-3, (ch, T)
    for ch, d in dec["digital"].items():
        if ch in SQUARE_CH:
            T, duty, tol = digital_period(d, L.digital_rate, dec["digital_stale"])
            assert abs(T - 1e-3) <= tol * 1e-3, (ch, T)


# ============================================================================================== AWG
@needs_traces
@pytest.mark.parametrize("name", AWG_RUNS)
def test_awg_loopback(name):
    """A0 reproduces the programmed AWG wave: frequency +-0.5 %, high/low +-10 % of the span."""
    run, L, dec = get(name)
    p = awg_cfg(name)
    assert p is not None
    a = dec["analog"][0].astype(float)
    vdiv, voff, inv, ac = cfg(run, 0)
    v = (a - voff) * vdiv / CODES_PER_V
    span = p["high"] - p["low"]
    if p["wave"] == 3:                                    # noise: stays inside the programmed range
        assert np.percentile(v, 1) >= p["low"] - 0.1 * span and np.percentile(v, 99) <= p["high"] + 0.1 * span
        return
    lo, hi = np.percentile(v, [0.5, 99.5])
    assert abs(hi - p["high"]) <= 0.1 * span, (hi, p["high"])
    assert abs(lo - p["low"]) <= 0.1 * span, (lo, p["low"])
    rate = L.analog_rate
    n_per = len(a) / rate * p["freq"]
    if n_per >= 2.5:
        mid_lo, mid_hi = lo + 0.3 * (hi - lo), lo + 0.7 * (hi - lo)
        r, f = schmitt(v, mid_lo, mid_hi)
        f_meas = rate * (len(r) - 1) / (r[-1] - r[0])
    else:
        assert p["wave"] == 0
        f_meas = sine_fit_freq(v, rate, p["freq"])
    assert abs(f_meas / p["freq"] - 1) <= 0.005, (f_meas, p["freq"])
    if p["wave"] == 2 and n_per >= 2.5:                   # square: duty
        highs = [f[f > x][0] - x for x in r[:-1] if (f > x).any()]
        duty = np.mean(highs) / ((r[-1] - r[0]) / (len(r) - 1))
        assert abs(duty * 100 - p["duty"]) <= 2.0, duty


# ==================================================================================== stream assembly
ASSEMBLY_RUNS = ["base", "dch_d0", "only_a1_d0", "roll_mode", "awg_sine_1hz_levels", "time_20000000000",
                 "te_a1_time100ms", "only_d0", "ach_a1"]


def _transfers(run):
    out, off = [run["head"]], 0
    for n in run["transfers"]:
        out.append(run["stream"][off:off + n])
        off += n
    return out


def _collect(events, layout):
    an = {c: [] for c in layout.analog}
    dg = {c: [] for c in layout.digital}
    frames, blk = [], 0
    for e in events:
        if e["kind"] == "samples":
            assert e["block"] == blk
            for c, v in e["analog"].items():
                an[c].append(v)
            for c, v in e["digital"].items():
                dg[c].append(v)
            blk += e["nblocks"]
        elif e["kind"] == "frame":
            assert e["nblocks"] == blk
            frames.append((e, {c: np.concatenate(v) if v else np.zeros(0, np.uint8) for c, v in an.items()},
                           {c: np.concatenate(v) if v else np.zeros(0, np.uint8) for c, v in dg.items()}))
            an = {c: [] for c in layout.analog}
            dg = {c: [] for c in layout.digital}
            blk = 0
    return frames


@needs_traces
@pytest.mark.parametrize("name", ASSEMBLY_RUNS)
def test_stream_assembler_matches_decode(name):
    """Feeding the recorded USB transfers one by one gives exactly decode()'s samples."""
    run, L, dec = get(name)
    asm = P.StreamAssembler(L)
    ev = []
    for t in _transfers(run):
        ev += asm.feed(t)
    assert ev[0]["kind"] == "head" and ev[-1]["kind"] == "end"
    frames = _collect(ev, L)
    assert len(frames) == 1
    e, an, dg = frames[0]
    assert e["nblocks"] == dec["nblocks"]
    for c in L.analog:
        assert np.array_equal(an[c], dec["analog"][c])
    for c in L.digital:
        assert np.array_equal(dg[c], dec["digital"][c])


@needs_traces
def test_stream_assembler_small_chunks_and_repeat():
    run, L, dec = get("roll_mode")
    blob = b"".join(_transfers(run))
    asm = P.StreamAssembler(L)
    ev = []
    for i in range(0, len(blob), 777):                    # odd chunking, head split too
        ev += asm.feed(blob[i:i + 777])
    frames = _collect(ev, L)
    assert len(frames) == 1 and np.array_equal(frames[0][1][1], dec["analog"][1])
    run, L, _ = get("repeat_mode")
    asm = P.StreamAssembler(L, max_frames=3)
    ev = []
    for t in _transfers(run):
        ev += asm.feed(t)
    frames = _collect(ev, L)
    ref = P.decode_frames(run["stream"], L)[:3]
    assert len(frames) == 3 and ev[-1]["kind"] == "end"
    for (e, an, dg), r in zip(frames, ref):
        assert e["trig_index"] == r["trig_index"]
        assert np.array_equal(an[1], r["analog"][1]) and np.array_equal(dg[7], r["digital"][7])


# ================================================================================ synthetic (no traces)
def encode(layout, analog, digital, nblk):
    """Inverse of decode for tests: build nblk blocks from per-channel arrays."""
    out = np.zeros((nblk, layout.block_bytes), np.uint8)
    na, nd = len(layout.analog), len(layout.digital)
    if na:
        A = np.stack([analog[c][:nblk * layout.analog_block_samples] for c in layout.analog])
        A = A.reshape(na, nblk, layout.amtxs * 8, 8).transpose(1, 2, 0, 3).reshape(nblk, -1)
        out[:, :layout.analog_block_bytes] = A
    if nd:
        D = np.stack([digital[c][:nblk * layout.digital_block_samples] for c in layout.digital])
        D = D.reshape(nd, nblk, layout.dmtxs, 64).transpose(1, 2, 0, 3)
        D = np.packbits(D, axis=3, bitorder="little").reshape(nblk, -1)
        out[:, layout.analog_block_bytes:] = D
    return out.tobytes()


SYNTH = [
    dict(analog=(0, 1, 2, 3), digital=tuple(range(8)), analog_rate=100e6, digital_rate=100e6),
    dict(analog=(2,), digital=tuple(range(8)), analog_rate=400e6, digital_rate=100e6, afold=4),
    dict(analog=(0, 3), digital=(1, 4, 6), analog_rate=200e6, digital_rate=200e6, afold=2, dfold=2),
    dict(analog=(0, 1, 2), digital=(5,), analog_rate=100e6, digital_rate=400e6, dfold=4),
    dict(analog=(), digital=(3, 4), analog_rate=100e6, digital_rate=400e6, dfold=4),
    dict(analog=(1,), digital=(), analog_rate=400e6, digital_rate=100e6, afold=4),
    dict(analog=(0, 1, 2, 3), digital=(0, 1, 2), analog_rate=100e6, digital_rate=200e6, dfold=2),
]


@pytest.mark.parametrize("kw", SYNTH)
def test_synthetic_roundtrip(kw):
    rng = np.random.default_rng(1)
    nblk = 37
    L0 = P.make_layout(**kw)
    words = nblk * L0.block_bytes // 4 + 3
    L = P.make_layout(acq_words=words, pretrig=1000, **kw)
    an = {c: rng.integers(0, 256, nblk * L.analog_block_samples, dtype=np.uint8) for c in L.analog}
    dg = {c: rng.integers(0, 2, nblk * L.digital_block_samples, dtype=np.uint8) for c in L.digital}
    body = encode(L, an, dg, nblk)
    body += b"\xee" * (L.acq_bytes - len(body))
    tail = bytearray(P.TAIL_BYTES)
    tail[0:4] = b"\x00\xa5\x5a\x00"
    tail[0x800:0x804] = (nblk * L.block_bytes // 4).to_bytes(4, "little")
    tail[0xC00:0xC04] = (17).to_bytes(4, "little")
    stream = body + bytes(tail) + bytes(5000)
    dec = P.decode(stream, L)
    assert dec["nblocks"] == nblk and dec["tail_offset"] == L.acq_bytes
    for c in L.analog:
        assert np.array_equal(dec["analog"][c], an[c])
    for c in L.digital:
        assert np.array_equal(dec["digital"][c], dg[c])
    assert P.trigger_index(dec["tail"], L) == 1017
    # assembler, 1000-byte chunks
    head = bytearray(P.HEAD_BYTES)
    head[0:4] = (P.HEAD_MAGIC).to_bytes(4, "little")
    head[8:12] = (0x80000000).to_bytes(4, "little")
    asm = P.StreamAssembler(L)
    blob = bytes(head) + stream
    ev = []
    for i in range(0, len(blob), 1000):
        ev += asm.feed(blob[i:i + 1000])
    assert ev[0]["head"]["triggered"] is False and ev[-1]["kind"] == "end"
    frames = _collect(ev, L)
    for c in L.analog:
        assert np.array_equal(frames[0][1][c], an[c])
    for c in L.digital:
        assert np.array_equal(frames[0][2][c], dg[c])
    # aborted acquisition: flush() releases the complete blocks received so far
    asm = P.StreamAssembler(L)
    cut = P.HEAD_BYTES + 10 * L.block_bytes + 5
    ev = asm.feed(blob[:cut]) + asm.flush()
    got = [e for e in ev if e["kind"] == "samples"]
    assert sum(e["nblocks"] for e in got) == 10
    for c in L.analog:
        assert np.array_equal(np.concatenate([e["analog"][c] for e in got]), an[c][:10 * L.analog_block_samples])
    # fill_stale only touches the leading digital samples
    dec2 = P.decode(stream, L, fill_stale=True)
    for c in L.digital:
        st = dec2["digital_stale"]
        assert (dec2["digital"][c][:st] == dg[c][st]).all() and np.array_equal(dec2["digital"][c][st:], dg[c][st:])


def test_layout_from_regs_basic():
    regs = {0x40: 5, 0x50: 4, 0x60: 5, 0x70: 4, 0x28: 0x201, 0x23: 0x0F, 0x20: 0x401, 0x15: 1, 0x17: 1,
            0x19: 0, 0x1A: 0, 0x13: 0xE90F, 0x14: 1, 0x31: 50048, 0x10: 3, 0x00: 0}
    L = P.layout_from_regs(regs)
    assert L.analog == (0, 2) and L.digital == (0, 1, 2, 3)
    assert (L.analog_rate, L.digital_rate) == (200e6, 200e6)
    assert L.block_bytes == 2 * 64 + 4 * 8 and L.acq_bytes == 503808 and L.trig_domain == "analog"
    with pytest.raises(P.PacketError):
        P.layout_from_regs({**regs, 0x28: 0x102})
    with pytest.raises(P.PacketError):
        P.parse_head(b"\x00" * 4096)
    h = P.parse_head((0x55555555).to_bytes(4, "little") + bytes(4) + (0x2E).to_bytes(4, "little") + bytes(4084))
    assert h["triggered"] and h["trig_pos_raw"] == 0x2E
