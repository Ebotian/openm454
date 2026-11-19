"""Fixtures for the hardware suite: the declared bench and one open scope (`hw`; excluded from CI by the marker)."""
import os

import pytest

from tests.support import bench as benchmod


@pytest.fixture(scope='session')
def bench():
    """tests/hardware/bench.json: which output drives which input, and what the capture must see."""
    return benchmod.Bench(benchmod.load())


@pytest.fixture(scope='session')
def scope():
    """The real M454. Fails when the device cannot be opened (an explicit hardware run must not turn into a green
    run of skips), and hands the generator back the way it found it: restored by mode, parked when the state is
    unknown or was off."""
    from openm454 import scope as scopemod
    if os.environ.get('PYTEST_XDIST_WORKER'):
        pytest.fail('the hardware suite drives one device: run it with -n 0, not under xdist')
    if os.environ.get('OPENM454_NO_USB'):
        pytest.skip('OPENM454_NO_USB is set: the hardware suite needs a real scope')
    s = scopemod.Scope()
    try:
        s.open()
    except Exception as e:                     # no device, another process holds it, no driver
        s.close()
        pytest.fail('the hardware suite could not open the M454: %s (set OPENM454_NO_USB=1 to skip instead)' % e)
    before = s.awg_status()
    try:
        yield s
    finally:
        failure = None
        try:
            benchmod.restore_awg(s, before)
        except Exception as e:                 # noqa: BLE001 - reported below, after the output is parked
            failure = e
            try:                               # never leave a test waveform on the wire
                s.awg_off()
            except Exception:                  # noqa: BLE001 - the restore error is the one worth reporting
                pass
        s.close()
        if failure is not None:
            raise AssertionError('the AWG state could not be restored (output parked at 0 V): %r' % (failure,))
