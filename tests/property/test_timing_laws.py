"""Hypothesis laws for the timing profile: only shorten, floor, fixed keys."""
import pytest
from hypothesis import given, settings, strategies as st

from openm454 import timing
from openm454.errors import DscopeError


@settings(max_examples=60)
@given(key=st.sampled_from(sorted(timing.KEYS)), value=st.floats(0, 2))
def test_profile_floor_law(key, value):
    spec = '%s=%r' % (key, value)
    floor = timing.MIN[key]
    base = timing.baseline()[key]
    if value > base or value < floor:
        with pytest.raises(DscopeError) as e:
            timing.validate(spec)
        assert e.value.code == 'INVALID_ARG'
    else:
        assert timing.validate(spec) == {key: value}


@given(key=st.sampled_from(sorted(timing.FIXED)))
def test_fixed_keys_are_refused(key):
    with pytest.raises(DscopeError) as e:
        timing.validate('%s=0.001' % key)
    assert e.value.code == 'INVALID_ARG'
