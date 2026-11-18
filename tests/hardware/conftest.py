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
    """The real M454, left as it was found: the AWG setting in force when the suite started is put back afterwards
    (the generator is write-only, so state/awg.json is the only record of what was running)."""
    from openm454 import scope as scopemod
    if os.environ.get('OPENM454_NO_USB'):
        pytest.skip('OPENM454_NO_USB is set: the hardware suite needs a real scope')
    s = scopemod.Scope()
    try:
        s.open()
    except Exception as e:                     # no device, another process holds it, no driver
        pytest.skip('no M454 on this machine: %s' % e)
    before = s.awg_status()
    try:
        yield s
    finally:
        try:
            if before and before.get('enabled'):
                s.awg_set(before['wave'], before['freq_hz'], before['high_v'], before['low_v'],
                          before.get('duty_pct', 50.0))
        except Exception:
            pass
        s.close()