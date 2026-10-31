"""Waveform measurements (analog volts arrays and digital 0/1 arrays). Pure numpy, no device access.

Definitions (documented for the agent reading the JSON):
  vtop/vbase  modes of the histogram of the upper / lower half of the range (robust high/low levels)
  vamp        vtop - vbase
  edges       crossings of the mid level (vtop+vbase)/2 with hysteresis max(20 % vamp, 4 noise, 2.5 LSB), interpolated
  freq        (n_rise - 1) / (t_last_rise - t_first_rise); FFT peak if fewer than 2 rising edges
  duty        high time / period averaged over whole periods between the first and last rising edge
  rise/fall   10-90 % transition times, median over edges
"""
import numpy as np


def _r(x, n=5):
    if x is None:
        return None
    x = float(x)
    if not np.isfinite(x):
        return None
    if x == 0:
        return 0.0
    return float('%.*g' % (n, x))


def _mode_level(v, lo, hi):
    sel = v[(v >= lo) & (v <= hi)]
    if sel.size == 0:
        return None
    nb = 64
    h, e = np.histogram(sel, bins=nb, range=(lo, hi) if hi > lo else None)
    k = int(np.argmax(h))
    # mean of samples inside the winning bin (sub-bin resolution)
    inb = sel[(sel >= e[k]) & (sel <= e[k + 1])]
    return float(inb.mean()) if inb.size else float((e[k] + e[k + 1]) / 2)


def crossings(v, lo_thr, hi_thr):
    """Schmitt-trigger crossings. Returns (rise_idx_float, fall_idx_float) using linear interpolation
    at the midpoint threshold."""
    mid = (lo_thr + hi_thr) / 2
    state = None
    rises, falls = [], []
    above = v >= hi_thr
    below = v <= lo_thr
    # vectorised state machine via forward-fill of the last decisive sample
    dec = np.where(above, 1, np.where(below, -1, 0))
    idx = np.nonzero(dec)[0]
    if idx.size < 2:
        return np.array([]), np.array([])
    d = dec[idx]
    ch = np.nonzero(np.diff(d))[0]
    for c in ch:
        a, b = idx[c], idx[c + 1]          # last decisive sample before, first after the transition
        seg = v[a:b + 1]
        if d[c + 1] > 0:
            k = np.nonzero(seg >= mid)[0]
        else:
            k = np.nonzero(seg <= mid)[0]
        j = a + (k[0] if k.size else b - a)
        if j > 0 and v[j] != v[j - 1]:
            frac = (mid - v[j - 1]) / (v[j] - v[j - 1])
            pos = j - 1 + min(max(frac, 0.0), 1.0)
        else:
            pos = float(j)
        (rises if d[c + 1] > 0 else falls).append(pos)
    return np.array(rises), np.array(falls)


def _transition_time(v, edges, lo, hi, rate, rising):
    """Median 10-90 % time around each edge (samples searched within +-half period)."""
    if len(edges) == 0:
        return None
    p10, p90 = lo + 0.1 * (hi - lo), lo + 0.9 * (hi - lo)
    out = []
    win = int(max(4, min(len(v) // max(len(edges) * 2, 1), 100000)))
    for e in edges[:200]:
        e = int(e)
        a, b = max(0, e - win), min(len(v), e + win)
        seg = v[a:b]
        c = e - a
        if rising:
            pre = np.nonzero(seg[:c + 1] <= p10)[0]
            post = np.nonzero(seg[c:] >= p90)[0]
        else:
            pre = np.nonzero(seg[:c + 1] >= p90)[0]
            post = np.nonzero(seg[c:] <= p10)[0]
        if pre.size and post.size:
            out.append((c + post[0]) - pre[-1])
    return float(np.median(out)) / rate if out else None


def analog(v, rate, clip_lo=None, clip_hi=None, edges_n=0, lsb=0.0):
    v = np.asarray(v, dtype=np.float64)
    n = v.size
    res = {'samples': int(n)}
    if n == 0:
        return res
    vmin, vmax = float(v.min()), float(v.max())
    res.update(vmin_v=_r(vmin), vmax_v=_r(vmax), vpp_v=_r(vmax - vmin), vmean_v=_r(v.mean()),
               vrms_v=_r(np.sqrt(np.mean(v * v))), vac_rms_v=_r(v.std()))
    midr = (vmin + vmax) / 2
    vtop = _mode_level(v, midr, vmax)
    vbase = _mode_level(v, vmin, midr)
    if vtop is None or vbase is None or vtop - vbase < 1e-9:
        vtop, vbase = vmax, vmin
    vamp = vtop - vbase
    res.update(vtop_v=_r(vtop), vbase_v=_r(vbase), vamp_v=_r(vamp))
    noise = float(np.std(np.diff(v[: min(n, 200000)]))) / np.sqrt(2) if n > 2 else 0.0
    hyst = max(0.2 * vamp, 4 * noise, 2.5 * lsb)
    freq = period = duty = None
    rises = falls = np.array([])
    if vamp > max(4 * noise, 3 * lsb) and vamp > 0:
        mid = (vtop + vbase) / 2
        rises, falls = crossings(v, mid - hyst / 2, mid + hyst / 2)
        if len(rises) >= 2:
            iv = np.diff(rises)
            med = float(np.median(iv))
            if len(iv) >= 3 and np.max(np.abs(iv - med)) > 0.1 * med:      # glitches: use the median period
                period_s = med / rate
                res['period_irregular'] = True
            else:
                period_s = (rises[-1] - rises[0]) / (len(rises) - 1) / rate
            freq, period = 1.0 / period_s, period_s
            hi = 0.0
            # high time per period: from each rise to the next fall
            fi = 0
            highs = []
            for k in range(len(rises) - 1):
                fs = falls[(falls > rises[k]) & (falls < rises[k + 1])]
                if fs.size:
                    highs.append(fs[0] - rises[k])
            if highs:
                duty = float(np.mean(highs)) / (period_s * rate) * 100
    if freq is None and n >= 64 and (vmax - vmin) > max(4 * lsb, 8 * noise):
        x = v - v.mean()
        sp = np.abs(np.fft.rfft(x * np.hanning(n)))
        k = int(np.argmax(sp[1:]) + 1)
        if sp[k] > 10 * (np.median(sp[1:]) + 1e-12) and 3 <= k < len(sp) - 1:     # >= 3 cycles in the window
            a, b, c = np.log(sp[k - 1] + 1e-30), np.log(sp[k] + 1e-30), np.log(sp[k + 1] + 1e-30)
            dk = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) != 0 else 0
            freq = (k + dk) * rate / n
            period = 1 / freq
            res['freq_method'] = 'fft'
    res.update(freq_hz=_r(freq, 7), period_s=_r(period, 7), duty_pct=_r(duty, 4),
               edges_rise=int(len(rises)), edges_fall=int(len(falls)),
               periods_in_window=_r(n / rate / period, 3) if period else None)
    if duty is not None and period:
        res.update(pwidth_s=_r(duty / 100 * period), nwidth_s=_r((1 - duty / 100) * period))
    res['rise_s'] = _r(_transition_time(v, rises, vbase, vtop, rate, True))
    res['fall_s'] = _r(_transition_time(v, falls, vbase, vtop, rate, False))
    if vamp > 0:
        res['overshoot_pct'] = _r((vmax - vtop) / vamp * 100, 3)
        res['undershoot_pct'] = _r((vbase - vmin) / vamp * 100, 3)
    if clip_lo is not None:
        res['clip_lo'] = bool(np.mean(v <= clip_lo) > 0.001)
    if clip_hi is not None:
        res['clip_hi'] = bool(np.mean(v >= clip_hi) > 0.001)
    if edges_n:
        res['edges_rise_s'] = [_r(x / rate, 7) for x in rises[:edges_n]]
        res['edges_fall_s'] = [_r(x / rate, 7) for x in falls[:edges_n]]
    return res


def digital(bits, rate, edges_n=0):
    b = np.asarray(bits, dtype=np.int8)
    n = b.size
    res = {'samples': int(n)}
    if n == 0:
        return res
    d = np.diff(b)
    rises = np.nonzero(d > 0)[0] + 1
    falls = np.nonzero(d < 0)[0] + 1
    hf = float(b.mean())
    if len(rises) + len(falls) == 0:
        res['state'] = 'high' if b[0] else 'low'
    else:
        res['state'] = 'toggling'
    res['high_frac'] = _r(hf, 4)
    res['edges'] = int(len(rises) + len(falls))
    if len(rises) >= 2:
        iv = np.diff(rises)
        med = float(np.median(iv))
        if len(iv) >= 3 and np.max(np.abs(iv - med)) > 0.1 * med:
            p = med
            widths = np.concatenate([np.diff(np.sort(np.concatenate([rises, falls])))])
            res['runts'] = int(np.sum(widths < 0.1 * med / 2))
        else:
            p = (rises[-1] - rises[0]) / (len(rises) - 1)
        res['freq_hz'] = _r(rate / p, 7)
        res['period_s'] = _r(p / rate, 7)
        seg = b[rises[0]:rises[-1]]
        res['duty_pct'] = _r(seg.mean() * 100, 4)
    if edges_n:
        res['edges_rise_s'] = [_r(x / rate, 7) for x in rises[:edges_n]]
        res['edges_fall_s'] = [_r(x / rate, 7) for x in falls[:edges_n]]
    return res
