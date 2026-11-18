"""Fixtures for the hardware suite: the declared bench and one open scope (`hw`; excluded from CI by the marker)."""
import os

import pytest

from tests.support import bench as benchmod


def _restore_awg(scope, before):
    """Put the generator back the way the suite found it; unknown state means park it, never leave a signal on.

    The generator is write-only, so state/awg.json is the only record of what was running, and it distinguishes the
    five modes. A failure here is a bench-safety failure: the caller turns it into an error, not something to swallow.
    """
    if not before or not before.get('enabled'):
        scope.awg_off()
        return 'parked at 0 V'
    mode = before.get('mode')
    if mode == 'burst':
        scope.awg_burst(before['wave'], before['freq_hz'], before['high_v'], before['low_v'], before['cycles'],
                        before['period_s'], duty=before.get('duty_pct', 50.0), phase=before.get('phase_deg', 0.0))
    elif mode == 'sweep':
        scope.awg_sweep(before['start_hz'], before['end_hz'], before['time_s'],
                        sweep_type=before.get('sweep_type', 'linear'), wave=before['wave'],
                        high=before['high_v'], low=before['low_v'], duty=before.get('duty_pct', 50.0))
    elif mode == 'modulation':
        scope.awg_modulate(before['kind'], before['wave'], before['freq_hz'], before['high_v'], before['low_v'],
                           mod_wave=before.get('mod_wave', 'sine'), mod_freq=before.get('mod_freq_hz', 100.0),
                           depth=(before.get('am') or {}).get('depth', 50.0),
                           deviation=(before.get('fm') or {}).get('deviation_hz'),
                           hop=(before.get('fsk') or {}).get('hop_hz'), duty=before.get('duty_pct', 50.0))
    elif before.get('wave') == 'dc':
        scope.awg_dc(before['level_v'])
    else:
        scope.awg_set(before['wave'], before['freq_hz'], before['high_v'], before['low_v'],
                      before.get('duty_pct', 50.0), before.get('phase_deg', 0.0))
    return 'restored (%s)' % (mode or 'continuous')


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
            _restore_awg(s, before)
        except Exception as e:                 # noqa: BLE001 - reported below, after the device is closed
            failure = e
        s.close()
        if failure is not None:
            raise AssertionError('the AWG state could not be restored: %r' % (failure,))
