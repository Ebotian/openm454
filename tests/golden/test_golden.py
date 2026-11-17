"""Table-driven golden cases for the AWG and the old repeat snapshot.

The new remote's repeat trace golden stays in test_repeat_golden.py; the old repeat snapshot and the new
repeat golden coexist under golden/.
"""
import hashlib
import json
from pathlib import Path

import pytest

from openm454 import awg

FIX = json.loads((Path(__file__).resolve().parents[1] / 'fixtures' / 'awg_programs.json').read_text())


def _final(seq):
    d = {}
    for a, v in seq:
        d[a] = v
    return d


@pytest.mark.parametrize('case', FIX, ids=[c['name'] for c in FIX])
def test_awg_program_matches_dswave(case):
    a = case['awg']
    wave = ['sine', 'triangle', 'square', 'noise'][a['wave']]
    mine = awg.program(wave, a['freq'], a['high'], a['low'], a['duty'], a['phase'] / 1e6)
    rec = case['writes'][:-3]
    assert case['writes'][-3:] == [list(x) for x in awg.START]
    assert _final(mine[1:]) == _final(rec[1:])


def test_awg_rejects_out_of_range():
    for kw in (dict(high=3.5), dict(low=-0.1), dict(freq=2e6), dict(wave='triangle', freq=500e3)):
        args = dict(wave='sine', freq=1000, high=3.0, low=0.0)
        args.update(kw)
        with pytest.raises(Exception):
            awg.program(**args)


REPEAT_FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'dswave' / 'triggered_repeat_setup.json'
REPEAT_NORMALIZED_SHA256 = '925b8f21b53c02b9d06998793bc8c8e14167c6140d5d3d17ad1025e705bbf49b'


def test_the_repeat_recording_is_pinned():
    """The old real DSWave repeat-mode snapshot; the new remote trace golden is separate and coexists."""
    recorded = json.loads(REPEAT_FIXTURE.read_text())

    assert len(recorded['program']) == 75
    assert recorded['program'][0] == '01=0000'
    assert recorded['program'][-2:] == ['00=0001', 'F9=0000']
    assert '30=0003' in recorded['program']
    assert set(recorded['wildcards']) == {'43', '53', '63', '73'}

    normalized = []
    for entry in recorded['program']:
        addr = entry.partition('=')[0]
        normalized.append(addr + '=*' if addr in recorded['wildcards'] else entry)
    assert hashlib.sha256(json.dumps(normalized).encode()).hexdigest() == REPEAT_NORMALIZED_SHA256
