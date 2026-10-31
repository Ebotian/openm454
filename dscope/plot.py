"""Render a Capture to PNG (matplotlib Agg). Designed to be read by an agent: one shared volts axis for
analog channels (fixed categorical colours A0..A3), digital channels as labelled lanes, trigger marker at t=0,
legend carries each channel's key numbers. Min/max decimation keeps edges and glitches visible."""
import numpy as np

SERIES = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100']        # A0..A3 (dataviz reference slots 1-4)
DIGITAL = '#6250d6'
SURFACE, INK, INK2, GRID = '#fcfcfb', '#0b0b0b', '#52514e', '#e4e3df'
COLUMNS = 3000


def _decimate(t, y, cols=COLUMNS):
    n = len(y)
    if n <= 2 * cols:
        return t, y
    k = n // cols
    m = k * cols
    yy = y[:m].reshape(cols, k)
    tt = t[:m].reshape(cols, k)[:, 0]
    lo, hi = yy.min(1), yy.max(1)
    t2 = np.repeat(tt, 2)
    y2 = np.empty(2 * cols, dtype=np.float64)
    y2[0::2], y2[1::2] = lo, hi
    return t2, y2


def _si(x, unit):
    if x is None:
        return '-'
    a = abs(x)
    for f, p in ((1e9, 'G'), (1e6, 'M'), (1e3, 'k'), (1, ''), (1e-3, 'm'), (1e-6, 'u'), (1e-9, 'n')):
        if a >= f or f == 1e-9:
            return '%.4g%s%s' % (x / f, p, unit)
    return '%g%s' % (x, unit)


def _time_unit(span):
    for f, u in ((1, 's'), (1e-3, 'ms'), (1e-6, 'us'), (1e-9, 'ns')):
        if span >= f * 2:
            return f, u
    return 1e-9, 'ns'


def plot(cap, path, window=None, title=None, summary=None, channels=None):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    chans = channels or cap.channels()
    an = [c for c in chans if c.startswith('A') and c in cap.analog]
    dg = [c for c in chans if c.startswith('D') and c in cap.digital]
    summary = summary or cap.summary(window=window)
    spans = [cap.time(c) for c in an + dg]
    t_lo = min(s[0] for s in spans) if spans else 0
    t_hi = max(s[-1] for s in spans) if spans else 1
    if window:
        t_lo = window[0] if window[0] is not None else t_lo
        t_hi = window[1] if window[1] is not None else t_hi
    f, tu = _time_unit(t_hi - t_lo)
    hr = [3] if an else []
    if dg:
        hr.append(max(1.0, 0.35 * len(dg)))
    fig, axes = plt.subplots(len(hr), 1, figsize=(14, 4.2 + 0.45 * len(dg) if an else 1.5 + 0.45 * len(dg)),
                             dpi=100, sharex=True, gridspec_kw={'height_ratios': hr}, squeeze=False)
    axes = axes[:, 0]
    fig.patch.set_facecolor(SURFACE)
    for ax in axes:
        ax.set_facecolor(SURFACE)
        for s in ax.spines.values():
            s.set_color(GRID)
        ax.tick_params(colors=INK2, labelsize=9)
        ax.grid(True, color=GRID, lw=0.6)
        ax.axvline(0, color=INK2, lw=0.8, ls='--')
    if an:
        ax = axes[0]
        for c in an:
            t = cap.time(c); v = cap.volts(c)
            if window:
                sel = (t >= t_lo) & (t <= t_hi); t, v = t[sel], v[sel]
            tt, vv = _decimate(t, v)
            m = summary['analog'].get(c, {})
            lab = '%s  %s/div  Vpp %s  f %s' % (c, _si(cap.scale(c)['vdiv_v'] * cap.scale(c).get('probe', 1), 'V'),
                                               _si(m.get('vpp_v'), 'V'), _si(m.get('freq_hz'), 'Hz'))
            ax.plot(tt / f, vv, color=SERIES[int(c[1])], lw=1.0, label=lab)
        trig = cap.meta.get('trigger', {})
        if trig.get('source', '').startswith('A') and trig.get('level_v') is not None:
            ax.axhline(trig['level_v'], color=INK2, lw=0.7, ls=':')
        ax.set_ylabel('V', color=INK2)
        ax.legend(loc='upper right', fontsize=8.5, frameon=True, facecolor=SURFACE, edgecolor=GRID, labelcolor=INK)
    if dg:
        ax = axes[-1]
        for i, c in enumerate(dg):
            t = cap.time(c); b = cap.digital[c].astype(np.float32)
            if window:
                sel = (t >= t_lo) & (t <= t_hi); t, b = t[sel], b[sel]
            tt, bb = _decimate(t, b)
            y0 = (len(dg) - 1 - i) * 1.4
            ax.fill_between(tt / f, y0, y0 + bb, step='post', color=DIGITAL, alpha=0.18, lw=0)
            ax.step(tt / f, y0 + bb, where='post', color=DIGITAL, lw=0.9)
            m = summary['digital'].get(c, {})
            ax.text(1.002, (y0 + 0.5) / (len(dg) * 1.4), '%s %s %s' % (c, m.get('state', ''), _si(m.get('freq_hz'), 'Hz') if m.get('freq_hz') else ''),
                    transform=ax.transAxes, fontsize=8.5, color=INK, va='center')
        ax.set_yticks([])
        ax.set_ylim(-0.3, len(dg) * 1.4)
    axes[-1].set_xlabel('time (%s), 0 = trigger' % tu, color=INK2)
    axes[-1].set_xlim(t_lo / f, t_hi / f)
    if title:
        axes[0].set_title(title, fontsize=10, color=INK, loc='left')
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
    return path
