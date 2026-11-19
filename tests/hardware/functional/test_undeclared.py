"""A link the bench file does not declare is skipped with its name, never guessed (`hw`)."""
import pytest

pytestmark = pytest.mark.hw

PROBE = 'cal_to_d0'          # named but not declared: the skip path itself is what this test checks


def test_an_undeclared_link_skips_and_names_what_is_declared(bench):
    """Digital inputs, the calibration output and external sources are not wired on this bench: the test that would
    need them has to say so instead of passing on a wrong pin."""
    with pytest.raises(pytest.skip.Exception) as e:
        bench.link(PROBE)
    assert 'awg_to_a0' in str(e.value)