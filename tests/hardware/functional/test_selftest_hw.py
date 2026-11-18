"""The driver's own self-test on the real scope: the wiring-free checks the offline suite can only fake (`hw`)."""
import pytest

from openm454 import selftest

pytestmark = pytest.mark.hw


def test_the_wiring_free_checks_pass_on_the_device(scope):
    res = selftest.run(scope, quick=True, awg=False)
    by_id = {i['id']: i for i in res['items']}
    assert by_id['V01']['pass'] is True, by_id['V01']          # device info: fw, FPGA, security, SuperSpeed
    assert by_id['V12']['pass'] is True, by_id['V12']          # forced 5 ms capture of all 12 channels at 100 MSa/s
    assert res['failed'] == 0, [i for i in res['items'] if i['pass'] is False]


def test_the_signal_checks_skip_with_the_reason_they_need_a_fixture(scope):
    """This bench declares one link (AWG into A0), not the standard fixture: the signal checks must say so."""
    res = selftest.run(scope, quick=True, awg=False)
    skipped = [i for i in res['items'] if i.get('skipped')]
    assert skipped, 'the standard-fixture checks cannot pass on this wiring'
    reasons = ' '.join(i['measured'].get('skipped') or '' for i in skipped)
    assert 'fixture' in reasons.lower()
    assert 'fixture' in (res.get('note') or '').lower(), res.get('note')
    assert res['failed'] == 0, 'a wiring-free check failed'