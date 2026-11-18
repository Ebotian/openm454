"""The calibration path on the unit it belongs to (`hw`): bound table applied, foreign table refused."""
import os

import pytest

from openm454 import calib
from openm454.errors import DscopeError

pytestmark = pytest.mark.hw


def _save(doc):
    calib.save(doc)
    return calib.PATH


def test_a_table_bound_to_this_unit_is_applied(bench, scope):
    """A neutral table is an exact identity: --cal must run the read/check/apply path and leave the reading alone."""
    link = bench.link('awg_to_a0')
    doc = calib.placeholder()
    doc['unit_id'] = calib.unit_id(scope.dev.zero_info)
    path = _save(doc)
    try:
        st = link.settings(2e-3, 10e6)
        st.cal = True
        cap = scope.capture(st)
        applied = cap.meta.get('applied') or {}
        assert applied.get('cal') is not None, applied
        link.assert_shows_the_declared_signal(cap.measure(link.channel))
    finally:
        os.unlink(path)


def test_a_table_from_another_unit_is_refused(bench, scope):
    doc = calib.placeholder()
    doc['unit_id'] = 'deadbeefdeadbeef'
    path = _save(doc)
    try:
        st = bench.link('awg_to_a0').settings(1e-3, 10e6)
        st.cal = True
        with pytest.raises(DscopeError) as e:
            scope.capture(st)
        assert e.value.code == 'CAL_MISMATCH'
    finally:
        os.unlink(path)
    cap = scope.capture(bench.link('awg_to_a0').settings(1e-3, 10e6))
    assert cap.meta['acq']['complete'], 'the scope has to stay usable after a refused calibration'