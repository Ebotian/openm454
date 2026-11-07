"""openm454.awg reproduces DSWave 1.2.1's AWG register programs (recorded in tests/fixtures/awg_programs.json)."""
import json, os
import pytest
from openm454 import awg

FIX = json.load(open(os.path.join(os.path.dirname(__file__), 'fixtures', 'awg_programs.json')))


def _final(seq):
    d = {}
    for a, v in seq:
        d[a] = v
    return d


@pytest.mark.parametrize('case', FIX, ids=[c['name'] for c in FIX])
def test_program_matches_dswave(case):
    a = case['awg']
    wave = ['sine', 'triangle', 'square', 'noise'][a['wave']]
    mine = awg.program(wave, a['freq'], a['high'], a['low'], a['duty'], a['phase'] / 1e6)
    rec = case['writes'][:-3]                       # last three = START
    assert case['writes'][-3:] == [list(x) for x in awg.START]
    assert _final(mine[1:]) == _final(rec[1:])


def test_rejects_out_of_range():
    for kw in (dict(high=3.5), dict(low=-0.1), dict(freq=2e6), dict(wave='triangle', freq=500e3)):
        args = dict(wave='sine', freq=1000, high=3.0, low=0.0); args.update(kw)
        with pytest.raises(Exception):
            awg.program(**args)
