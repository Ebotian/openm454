import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


@pytest.fixture(autouse=True)
def _no_unit_calibration(tmp_path, monkeypatch):
    """Offline tests never read the checkout's state/calibration.json (a per-unit file, opt-in with --cal):
    calib.PATH points at a file that does not exist unless a test writes one."""
    from openm454 import calib
    monkeypatch.setattr(calib, 'PATH', str(tmp_path / 'state' / 'calibration.json'))
    calib._cache.clear()
    yield
    calib._cache.clear()
