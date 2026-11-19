"""A real capture through the offline file paths: save, load, analyze, plot (`hw`)."""
import json

import numpy as np
import pytest

from openm454 import cli
from openm454.capture import load_capture

pytestmark = pytest.mark.hw


def test_a_real_capture_survives_save_load_analyze_plot(bench, scope, tmp_path, capsys):
    link = bench.link('awg_to_a0')
    link.program(scope)
    cap = scope.capture(link.settings(2e-3, 10e6))
    npz = cap.save(str(tmp_path / 'cap.npz'))
    back = load_capture(npz)
    assert np.array_equal(back.analog[link.channel], cap.analog[link.channel])
    assert back.meta['acq']['samples'] == cap.meta['acq']['samples']

    assert cli.main(['analyze', npz]) == 0
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    mine = cap.measure(link.channel)
    assert line['analog'][link.channel]['freq_hz'] == pytest.approx(mine['freq_hz'], rel=1e-9)
    assert line['analog'][link.channel]['vpp_v'] == pytest.approx(mine['vpp_v'], rel=1e-6)

    png = str(tmp_path / 'cap.png')
    assert cli.main(['plot', npz, '--out', png]) == 0
    assert (tmp_path / 'cap.png').stat().st_size > 1000, 'the plot has to be a real picture, not an empty file'