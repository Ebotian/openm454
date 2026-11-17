"""Repeat mode against DSWave's own recording (corpus4 `repeat_mode`): the triggered repeat program the driver builds
from user-level Settings (Scope.capture -> hal_config -> mso_adapter -> mso) and the per-frame decoding of the
recorded frames (mso_adapter.decode_frames). Skips when the git-ignored traces are not present.

The recorded run: A0-A3 at 1 V/div DC, D0-D7 at 1.4 V, 1 ms window, 100,000 samples, trigger A1 rise at code 169,
normal mode, 50 % pre-trigger, :REPEATModeEnable 1. DSWave's HAL logs the trigger position of every frame
("trigger samples: N"), which each frame's own tail has to reproduce.
"""
import json
import os

import numpy as np
import pytest

from openm454 import scope as scopemod
from openm454.acquire import RawCapture

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SESS = os.path.join(ROOT, 're', 'traces', 'S01_warm')
SCEN = os.path.join(SESS, 'scen', 'repeat_mode.json')
ZERO = os.path.join(ROOT, 'state', 'cache', 'zero_info_from_trace.bin')
HAVE = all(os.path.exists(p) for p in (SCEN, os.path.join(SESS, 'bulk_in.bin'), ZERO))
pytestmark = pytest.mark.skipif(not HAVE, reason='recorded traces not present')

N = 8                                    # frames replayed (the recording holds ~1000)
LEVEL_CODE = 169                         # the recorded :ANALOG:CHANNEL:TRIGGERlevel
KEY_REGS = (0x0E, 0x0F)                  # never compared or printed


def _recorded():
    r = json.load(open(SCEN))
    pk = sorted([b for b in r['bulk_in'] if b['pipe'] == 0x86 and b['actual']], key=lambda b: b['seq'])
    with open(os.path.join(SESS, 'bulk_in.bin'), 'rb') as f:
        chunks = []
        for b in pk[:1 + N]:
            f.seek(b['bin_off'])
            chunks.append(f.read(b['actual']))
    prog = []
    for kind, name, addr, val in r['run_writes']:          # the setup block: up to W 0xF9
        prog.append((addr, val))
        if addr == 0xF9:
            break
    trig = [int(line.split('trigger samples: ')[1]) for line in r.get('log', []) if 'trigger samples:' in line]
    return chunks[0], chunks[1:], prog, trig


class _Engine:
    def __init__(self, head, frames):
        self.head, self.frames, self.calls = head, frames, []

    def run(self, prog, **kw):
        self.calls.append((list(prog), kw))
        raw = RawCapture()
        raw.head = self.head
        raw.frames = list(self.frames[:kw.get('repeat_frames', 0)])
        raw.stop = 'normal'
        return raw


class _Dev:
    info = {}

    def __init__(self):
        self.zero_info = open(ZERO, 'rb').read()


def _settings():
    ch = ('A0', 'A1', 'A2', 'A3')
    return scopemod.Settings(analog=list(ch), digital=['D%d' % i for i in range(8)], vdiv={c: 1.0 for c in ch},
                             offset={c: 0.0 for c in ch}, coupling={c: 'dc' for c in ch}, probe={c: 1.0 for c in ch},
                             threshold=1.4, duration=1e-3, samples=100_000, trig_source='A1', trig_edge='rise',
                             trig_level=(LEVEL_CODE - 128) / 29.375, trig_mode='normal', trig_pos=50.0, repeat=N)


class _Replay(tuple):
    """(capture, engine calls, recorded program, HAL trigger samples, recorded frames), with a short repr so a
    failing test never prints register programs or raw bytes."""
    def __repr__(self):
        return '<repeat_mode replay: %d frames>' % len(self[4])


@pytest.fixture(scope='module')
def replay():
    head, frames, rec_prog, trig = _recorded()
    s = scopemod.Scope()
    s.dev, s.engine = _Dev(), _Engine(head, frames)
    s._check_link = lambda: None
    cap = s.capture(_settings())
    return _Replay((cap, s.engine.calls, rec_prog, trig, frames))


def _no_key(prog):
    return [(a, v) for a, v in prog if a not in scopemod.CROSS_REGS and a not in KEY_REGS]


def test_the_triggered_repeat_program_is_the_recorded_one(replay):
    cap, calls, rec_prog, _, frames = replay
    assert len(calls) == 1
    prog, kw = calls[0]
    mine, rec = _no_key(prog), _no_key(rec_prog)
    assert len(mine) == len(rec)
    diff = [i for i, (m, r) in enumerate(zip(mine, rec)) if m != r]
    assert not diff, 'writes differ at %s' % diff[:10]            # write by write, order and values
    assert [v for a, v in prog if a == 0x00] == [1]               # HW_BASE_MODE 1: repeat, no stream
    assert kw['repeat_frames'] == N and kw['n_transfers'] == 2
    assert kw['pkt_bytes'] == len(frames[0])                      # one recorded transfer = one frame
    hal = scopemod.hal_config(_settings(), repeat=N)[0]
    assert hal['repeat'] == 1 and hal['analog'][1]['trig_level_code'] == LEVEL_CODE


def test_the_plan_compiles_the_same_program(replay):
    """Scope.plan (the offline path the CLI checks before opening the device) gives the same repeat geometry."""
    _, calls, _, _, frames = replay
    s = scopemod.Scope()
    s.dev = _Dev()
    pl = s.plan(_settings())
    assert pl['geometry']['hw_mode'] == 1 and pl['geometry']['roll'] is False
    assert pl['geometry']['pkt_bytes'] == len(frames[0]) and pl['writes'] == len(calls[0][0])


def test_every_recorded_frame_is_decoded_with_its_own_trigger(replay):
    cap, _, _, trig, frames = replay
    assert cap.meta['acq']['repeat_frames'] == N and cap.meta['acq']['frames'] == N and len(cap.frames) == N
    got = [f['trig_index'] for f in cap.frames]
    assert got == trig[:N]                                        # DSWave's HAL log, frame by frame
    assert len(set(got)) > 1                                      # separate acquisitions, not one copied
    rate = cap.meta['analog_rate_hz']
    for i, f in enumerate(cap.frames):
        a1 = np.asarray(f['analog']['A1'], dtype=float)
        assert len(a1) == cap.meta['acq']['samples']
        assert f['t0_analog'] == pytest.approx(-0.5e-3, rel=1e-3)          # 50 % pre-trigger
        k = int(round(-f['t0_analog'] * rate))                    # sample of t = 0
        before, after = np.median(a1[k - 300:k - 100]), np.median(a1[k + 100:k + 300])
        assert before < LEVEL_CODE < after, i                     # A1 rises through the level at t = 0
        assert abs(a1[k] - LEVEL_CODE) <= 2, i
        assert len(f['digital']['D0']) == len(cap.frames[0]['digital']['D0'])
    assert not any(np.array_equal(cap.frames[0]['analog']['A1'], f['analog']['A1']) for f in cap.frames[1:])
    assert np.array_equal(cap.analog['A1'], cap.frames[0]['analog']['A1'])


def test_the_recorded_frames_survive_a_save(replay, tmp_path):
    from openm454.capture import load_capture
    cap = replay[0]
    back = load_capture(cap.save(str(tmp_path / 'repeat.npz')))
    assert len(back.frames) == N
    assert [f['trig_index'] for f in back.frames] == [f['trig_index'] for f in cap.frames]
    n = back.meta['frames_samples']['D0']
    assert all(len(f['digital']['D0']) == n for f in back.frames)
    assert np.array_equal(back.frames[3]['digital']['D0'], cap.frames[3]['digital']['D0'][:n])
