"""PNG layout: the legend, title and lane labels never cover the plotted data (synthetic captures, Agg backend)."""
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pytest

from openm454 import plot as P
from openm454.capture import CODES_PER_DIV, Capture

ALL_D = tuple('D%d' % i for i in range(8))
TITLE = 'DSCope M454  A0,A1,A2,A3,D0,D1,D2,D3,D4,D5,D6,D7  5e+06 Sa/s  20000 samples  trig A1 rise 1.4  triggered'


def bench_cap(an=('A0', 'A1', 'A2', 'A3'), dg=ALL_D, rate=5e6, n=20000):
    """The standard fixture in miniature: 1 kHz 2.8 V square on A1..A3 / D0,D2,D5,D7, A0 parked, A2 overshooting."""
    t = -n / 2 / rate + np.arange(n) / rate
    ph = (t * 1000) % 1
    sq = ph < 0.5
    chans, analog = {}, {}
    for c in an:
        vdiv = 0.5 if c in ('A0', 'A3') else 1.0
        v = np.full(n, 0.005) if c == 'A0' else 2.8 * sq
        if c == 'A2':                                      # over-compensated 10x probe: decaying overshoot
            v = v + 0.5 * np.exp(-(ph % 0.5) / 0.1) * np.where(sq, 1, -1)
        vpc = vdiv / CODES_PER_DIV
        chans[c] = {'zero_code': 60.0, 'volts_per_code': vpc, 'probe': 1.0, 'vdiv_v': vdiv}
        analog[c] = np.clip(np.round(v / vpc + 60), 0, 255).astype(np.uint8)
    digital = {c: (sq if c in ('D0', 'D2', 'D5', 'D7') else np.zeros(n, bool)).astype(np.uint8) for c in dg}
    return Capture({'analog_rate_hz': rate if an else None, 't0_analog_s': float(t[0]),
                    'digital_rate_hz': rate if dg else None, 't0_digital_s': float(t[0]), 'channels': chans,
                    'trigger': {'source': 'A1', 'edge': 'rise', 'level_v': 1.4}}, analog=analog, digital=digital)


def _overlap(a, b, tol=0.5):
    """True when two display-space boxes share more than a hairline."""
    return min(a.x1, b.x1) - max(a.x0, b.x0) > tol and min(a.y1, b.y1) - max(a.y0, b.y0) > tol


def _inside(a, b, tol=0.5):
    return a.x0 >= b.x0 - tol and a.y0 >= b.y0 - tol and a.x1 <= b.x1 + tol and a.y1 <= b.y1 + tol


def check_layout(fig, n_an, title=None):
    fig.canvas.draw()
    r = fig.canvas.get_renderer()
    data = [ax.get_window_extent(r) for ax in fig.axes]           # each axes' data area (the patch)
    legends = [ax.get_legend() for ax in fig.axes if ax.get_legend() is not None]
    assert len(legends) == (1 if n_an else 0)
    for leg in legends:
        box = leg.get_window_extent(r)
        assert len(leg.get_texts()) == n_an
        assert not any(_overlap(box, d) for d in data), 'legend %s covers a data area %s' % (box, data)
        assert _inside(box, fig.bbox), 'legend %s is cut off by the figure %s' % (box, fig.bbox)
    if title:
        tt = [t for t in fig.findobj(matplotlib.text.Text) if t.get_text() == title and t.get_visible()]
        assert len(tt) == 1
        tb = tt[0].get_window_extent(r)
        assert not any(_overlap(tb, d) for d in data)
        assert not any(_overlap(tb, leg.get_window_extent(r)) for leg in legends)
        assert _inside(tb, fig.bbox)
    for ax in fig.axes:                                            # digital lane labels: right margin, lane centre
        for txt in ax.texts:
            tb = txt.get_window_extent(r)
            assert not any(_overlap(tb, d) for d in data)
            assert _inside(tb, fig.bbox)
            lane = int(ax.texts.index(txt))
            y0 = (len(ax.texts) - 1 - lane) * 1.4
            lo, hi = ax.transData.transform([(0, y0), (0, y0 + 1)])[:, 1]
            assert lo <= (tb.y0 + tb.y1) / 2 <= hi, '%s label is off its lane' % txt.get_text()


@pytest.mark.parametrize('an,dg', [
    (('A1',), ()),                                     # one analog channel: the first plot of the report
    (('A0', 'A1', 'A2', 'A3'), ()),
    (('A0', 'A1', 'A2', 'A3'), ALL_D),                 # all 12 channels
    (('A1', 'A2'), ('D0', 'D5')),
    ((), ('D0',)),
    ((), ALL_D),
])
def test_legend_and_labels_stay_off_the_data(an, dg):
    fig = P.render(bench_cap(an, dg), title=TITLE)
    try:
        check_layout(fig, len(an), TITLE)
    finally:
        plt.close(fig)


def test_zoom_window_and_no_title():
    cap = bench_cap()
    fig = P.render(cap, window=(-0.2e-3, 0.3e-3))
    try:
        check_layout(fig, 4)
    finally:
        plt.close(fig)


def test_long_labels_fall_back_to_two_legend_rows(monkeypatch):
    monkeypatch.setattr(P, '_si', lambda x, unit: '%s' % ('9' * 18 + unit))   # every number 18+ characters wide
    fig = P.render(bench_cap(dg=()), title=TITLE)
    try:
        check_layout(fig, 4, TITLE)
        r = fig.canvas.get_renderer()
        rows = {round(t.get_window_extent(r).y0) for t in fig.axes[0].get_legend().get_texts()}
        assert len(rows) == 2
    finally:
        plt.close(fig)


def test_plot_writes_png_and_tolerates_missing_vdiv(tmp_path):
    cap = bench_cap(('A1',), ('D0',))
    del cap.meta['channels']['A1']['vdiv_v']               # older files: V/div derived from volts_per_code
    out = P.plot(cap, str(tmp_path / 'x.png'), title='x')
    with open(out, 'rb') as fh:
        assert fh.read(8) == b'\x89PNG\r\n\x1a\n'
    fig = P.render(cap)
    try:
        assert fig.axes[0].get_legend().get_texts()[0].get_text().startswith('A1  1V/div')
    finally:
        plt.close(fig)
