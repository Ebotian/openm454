"""Capture container: raw codes + scaling + timing, measurements, npz save/load.

Time base: t = 0 at the trigger point (or at the window centre for untriggered captures);
sample k of a channel sampled at rate r is at t = t0 + k / r.
Analog volts = (code - zero_code) * volts_per_code * probe, volts_per_code = vdiv * 8 / 235.
"""
import json
import os
import time

import numpy as np

from . import measure

CODES_PER_DIV = 235.0 / 8.0          # 29.375
CODE_MIN, CODE_MAX = 10, 245          # usable ADC range (profile ref_min/ref_max)


class Capture:
    def __init__(self, meta, analog=None, digital=None):
        self.meta = meta                   # dict (JSON-serialisable)
        self.analog = analog or {}         # 'A1' -> uint8 codes
        self.digital = digital or {}       # 'D0' -> uint8 0/1
        self._measured = {}                # (ch, edges_n, window) -> result (the data never changes)

    # ---------------------------------------------------------------- scaling / time
    def scale(self, ch):
        return self.meta['channels'][ch]

    def volts(self, ch):
        s = self.scale(ch)
        return (self.analog[ch].astype(np.float32) - s['zero_code']) * s['volts_per_code'] * s.get('probe', 1.0)

    def rate(self, ch):
        return self.meta['analog_rate_hz'] if ch.startswith('A') else self.meta['digital_rate_hz']

    def t0(self, ch):
        return self.meta['t0_analog_s'] if ch.startswith('A') else self.meta['t0_digital_s']

    def time(self, ch):
        n = len(self.analog[ch]) if ch.startswith('A') else len(self.digital[ch])
        return self.t0(ch) + np.arange(n) / self.rate(ch)

    # ---------------------------------------------------------------- measurements
    def measure(self, ch, edges_n=0, window=None):
        key = (ch, edges_n, tuple(window) if window else None)
        if key not in self._measured:
            self._measured[key] = self._measure(ch, edges_n, window)
        return dict(self._measured[key])

    def _measure(self, ch, edges_n=0, window=None):
        if ch.startswith('A'):
            v = self.volts(ch)
            s = self.scale(ch)
            k = s.get('probe', 1.0) * s['volts_per_code']
            if window:
                v = self._win(ch, v, window)
            return measure.analog(v, self.rate(ch), clip_lo=(CODE_MIN + 1 - s['zero_code']) * k,
                                  clip_hi=(CODE_MAX - 1 - s['zero_code']) * k, edges_n=edges_n, lsb=k)
        b = self.digital[ch]
        if window:
            b = self._win(ch, b, window)
        return measure.digital(b, self.rate(ch), edges_n=edges_n)

    def _win(self, ch, arr, window):
        t0, t1 = window
        r, s0 = self.rate(ch), self.t0(ch)
        a = max(0, int((t0 - s0) * r)) if t0 is not None else 0
        b = min(len(arr), int((t1 - s0) * r)) if t1 is not None else len(arr)
        return arr[a:b]

    def channels(self):
        return sorted(self.analog) + sorted(self.digital, key=lambda c: int(c[1:]))

    def summary(self, edges_n=0, window=None):
        out = {'analog': {}, 'digital': {}}
        for ch in sorted(self.analog):
            out['analog'][ch] = self.measure(ch, edges_n, window)
        for ch in sorted(self.digital, key=lambda c: int(c[1:])):
            out['digital'][ch] = self.measure(ch, edges_n, window)
        return out

    def dump(self, ch, points=200, window=None):
        """Decimated min/max/mean series for numeric inspection without a picture."""
        arr = self.volts(ch) if ch.startswith('A') else self.digital[ch].astype(np.float32)
        t = self.time(ch)
        if window:
            arr = self._win(ch, arr, window); t = self._win(ch, t, window)
        n = len(arr)
        if n == 0:
            return {'t_s': [], 'min': [], 'max': [], 'mean': []}
        k = max(1, int(np.ceil(n / points)))
        m = (n // k) * k
        a = arr[:m].reshape(-1, k)
        tt = t[:m].reshape(-1, k)[:, 0]
        r = lambda x: [float('%.5g' % y) for y in x]
        return {'t_s': r(tt), 'min': r(a.min(1)), 'max': r(a.max(1)), 'mean': r(a.mean(1)), 'samples_per_point': k}

    # ---------------------------------------------------------------- persistence
    def save(self, path):
        arrays = {}
        for ch, a in self.analog.items():
            arrays['analog_' + ch] = a
        for ch, a in self.digital.items():
            arrays['digital_' + ch] = np.packbits(a, bitorder='little')
            self.meta.setdefault('digital_lengths', {})[ch] = int(len(a))
        meta = json.dumps(self.meta)
        n = sum(len(a) for a in self.analog.values())
        (np.savez_compressed if n < 20_000_000 else np.savez)(path, meta=np.frombuffer(meta.encode(), dtype=np.uint8), **arrays)
        return path

    def to_volts_dict(self):
        return {ch: self.volts(ch) for ch in self.analog}


def load_capture(path):
    z = np.load(path)
    meta = json.loads(bytes(z['meta']).decode())
    analog, digital = {}, {}
    for k in z.files:
        if k.startswith('analog_'):
            analog[k[7:]] = z[k]
        elif k.startswith('digital_'):
            ch = k[8:]
            digital[ch] = np.unpackbits(z[k], bitorder='little')[:meta['digital_lengths'][ch]]
    return Capture(meta, analog, digital)


def new_path(out_dir, tag='cap'):
    os.makedirs(out_dir, exist_ok=True)
    base = time.strftime('%Y%m%d-%H%M%S') + ('-%03d' % (int(time.time() * 1000) % 1000))
    return os.path.join(out_dir, '%s_%s' % (base, tag))


def prune(out_dir, keep=200):
    try:
        fs = sorted((os.path.join(out_dir, f) for f in os.listdir(out_dir)), key=os.path.getmtime)
    except OSError:
        return
    stems = []
    for f in fs:
        st = os.path.splitext(f)[0]
        if st not in stems:
            stems.append(st)
    for st in stems[:-keep]:
        for ext in ('.npz', '.png', '.json'):
            try:
                os.remove(st + ext)
            except OSError:
                pass
