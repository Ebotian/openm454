"""Host arrival stamps of a repeat run: one per frame, kept by the .npz, absent on a replay (no hardware)."""
import tempfile
import os

import numpy as np
import pytest
from hypothesis import given, settings, strategies as st

from openm454 import capture as capmod


def _cap(stamps):
    n = len(stamps)
    frames = [{'analog': {'A0': np.full(4, i % 256, np.uint8)}, 'digital': {}, 't0_analog': 0.0,
               't0_digital': 0.0, 'trig_index': None if i % 3 else i} for i in range(n)]
    for i, s in enumerate(stamps):
        if s is not None:
            frames[i]['host_s'] = s
    meta = {'channels': {'A0': {'zero_code': 128, 'volts_per_code': 0.034, 'probe': 1.0}},
            'analog_rate_hz': 1e6, 'digital_rate_hz': 1e6, 't0_analog_s': 0.0, 't0_digital_s': 0.0,
            'acq': {'repeat_frames': n, 'frames': n}, 'frames': n}
    cap = capmod.Capture(meta, frames[0]['analog'], {})
    cap.frames = frames
    return cap


def _roundtrip(cap):
    d = tempfile.mkdtemp()
    try:
        return capmod.load_capture(cap.save(os.path.join(d, 'r.npz')))
    finally:
        for n in os.listdir(d):
            os.remove(os.path.join(d, n))
        os.rmdir(d)


@given(stamps=st.lists(st.floats(min_value=0.0, max_value=60.0, allow_nan=False, allow_infinity=False),
                       min_size=2, max_size=8))
@settings(max_examples=25)
def test_every_stamp_comes_back_on_its_own_frame(stamps):
    """The stamps are an acquisition fact like the trigger index: one per frame, same order, after a save/load."""
    back = _roundtrip(_cap(stamps))
    assert back.meta['frame_host_s'] == pytest.approx(stamps, rel=1e-8, abs=0)
    assert [f['host_s'] for f in back.frames] == pytest.approx(stamps, rel=1e-8, abs=0)


@given(n=st.integers(min_value=2, max_value=8))
@settings(max_examples=10)
def test_a_replay_writes_no_stamp_key(n):
    """A replayed/golden run has no arrival time; the key must be absent, not a list of nulls."""
    back = _roundtrip(_cap([None] * n))
    assert 'frame_host_s' not in back.meta
    assert all('host_s' not in f for f in back.frames)