"""Live processing hook for `log`: your own Python code sees every sample of a gap-free recording as it arrives and
can react at once (a threshold alarm, a counter, a trigger for other equipment, an early stop).

    python -m openm454 log -c A1,D0 --rate 1M --duration forever --hook myhook.py

myhook.py defines

    def setup(info):         # optional, once before the first sample: info = {'rate_hz', 'analog', 'digital',
        ...                  #   'scale': {ch: {'volts_per_code', 'zero_code', 'probe'}}, 'every_s', 'duration_s', ...}
    def process(block):      # required: every piece of the recording, in order, without gaps (about 10 ms of signal
        ...                  #   each, more when the USB transfers are longer); return None, or a dict = an event
    def finish(summary):     # optional, once at the end; a returned dict goes into the final result
        ...

A dict returned by process() is printed at once as one JSON line {"event": {...}, "k": n, "t_signal_s": end of the
block, "lag_s": how far behind live the hook was (seconds of newer signal the scope had already taken), "t_wall_s":
...}; {"stop": true} in it also ends the recording cleanly (stop_reason "hook"). block offers
t0_s / t1_s (signal seconds since the start), rate_hz, n (samples per channel), analog / digital (channel lists),
codes(ch) (uint8 ADC codes), volts(ch) (float32 at the probe tip), bits(ch) (uint8 0/1), packed(ch) (bits packed
little-endian, 8 per byte), times() (float64 seconds). The arrays belong to the recording: do not modify them.

The hook runs in the thread that decodes the data, so it must keep up: its time is reported (hook_ms per live line,
the final 'hook' block). If it falls behind, the queue grows and the run stops with HOST_TOO_SLOW before data could be
lost. An exception in the hook stops the run with HOOK_ERROR (the data so far is kept). The scope is busy recording:
the hook must not talk to it (no openm454 commands); anything else (files, sockets, serial ports) is fine.
"""
import builtins
import importlib.util
import json
import os
import sys
import time
import traceback

import numpy as np

from .errors import DscopeError

PIECE_S = 0.01           # the recording hands the hook pieces of about this much signal (at least one 64-sample block)


def _stderr_print(*args, **kw):
    """print() inside a hook file: to stderr unless a file is given, so the JSON lines on stdout stay intact."""
    kw.setdefault('file', sys.stderr)
    builtins.print(*args, **kw)


def plain(x):
    """An event made printable: numpy scalars and arrays become Python numbers and lists (block.codes(ch).min() is a
    numpy uint8); NaN/inf become None. Anything else that JSON cannot hold raises TypeError."""
    if isinstance(x, dict):
        return {str(k): plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [plain(v) for v in x]
    if isinstance(x, np.ndarray):
        return plain(x.tolist())
    if isinstance(x, np.generic):
        x = x.item()
    if isinstance(x, float) and (x != x or x in (float('inf'), float('-inf'))):
        return None
    if x is None or isinstance(x, (bool, int, float, str)):
        return x
    raise TypeError('%s is not JSON (use numbers, strings, lists, dicts)' % type(x).__name__)


def _plain_dict(r, where):
    try:
        r = plain(r)
        json.dumps(r)
    except (TypeError, ValueError, RecursionError) as e:
        raise HookError(where, TypeError('the returned dict cannot be printed as JSON: %s' % e)) from None
    return r


class HookError(Exception):
    """The user's hook raised: carries a short traceback for the error report."""

    def __init__(self, where, exc):
        tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
        super().__init__('%s() raised %s: %s' % (where, type(exc).__name__, exc))
        self.trace = [l.rstrip() for l in ''.join(tb[-4:]).splitlines()][-6:]


class Block:
    """One piece of the recording: samples t0_s .. t1_s of every channel (see the module doc)."""

    def __init__(self, arrays, t0_s, n, rate_hz, scale, analog, digital):
        self._a, self._scale = arrays, scale
        self.t0_s, self.n, self.rate_hz = t0_s, n, rate_hz
        self.t1_s = t0_s + n / rate_hz
        self.analog, self.digital = list(analog), list(digital)
        self._cache = {}

    def codes(self, ch):
        """Raw ADC codes (uint8) of an analog channel."""
        return self._a[ch]

    def volts(self, ch):
        """Volts at the probe tip (float32) of an analog channel."""
        k = ('v', ch)
        if k not in self._cache:
            s = self._scale[ch]
            self._cache[k] = ((self._a[ch].astype(np.float32) - np.float32(s['zero_code']))
                              * np.float32(s['volts_per_code'] * s['probe']))
        return self._cache[k]

    def packed(self, ch):
        """Bits of a digital channel packed 8 per byte, little-endian (sample 0 = bit 0 of byte 0)."""
        return self._a[ch]

    def bits(self, ch):
        """One uint8 0/1 per sample of a digital channel."""
        k = ('b', ch)
        if k not in self._cache:
            self._cache[k] = np.unpackbits(self._a[ch], bitorder='little')[:self.n]
        return self._cache[k]

    def times(self):
        """Signal time of every sample, seconds since the start of the recording."""
        if 't' not in self._cache:
            self._cache['t'] = self.t0_s + np.arange(self.n) / self.rate_hz
        return self._cache['t']


class Hook:
    """A loaded hook file: setup / process / finish with timing."""

    def __init__(self, path):
        self.path = os.path.abspath(path)
        if not os.path.isfile(self.path):
            raise DscopeError('INVALID_ARG', 'hook file not found: %s' % path, 'give the path of a .py file that '
                              'defines process(block)')
        name = '_openm454_hook_%d' % (abs(hash(self.path)) % 10 ** 8)
        try:
            spec = importlib.util.spec_from_file_location(name, self.path)
            mod = importlib.util.module_from_spec(spec)
            mod.print = _stderr_print                # the hook's own print() never lands between the JSON lines
            sys.modules[name] = mod                  # dataclasses, pickle, typing need the module registered
            folder = os.path.dirname(self.path)
            if folder not in sys.path:
                sys.path.append(folder)              # helpers next to the hook file import (appended: no shadowing)
            spec.loader.exec_module(mod)
        except BaseException as e:                   # noqa: BLE001 - syntax errors, imports, sys.exit(): before the run
            sys.modules.pop(name, None)
            raise DscopeError('INVALID_ARG', 'the hook file %s cannot be loaded: %s: %s'
                              % (os.path.basename(path), type(e).__name__, e), 'fix the file; '
                              '"python %s" shows the full error' % path) from None
        self.process = getattr(mod, 'process', None)
        if not callable(self.process):
            raise DscopeError('INVALID_ARG', 'the hook file %s defines no process(block) function'
                              % os.path.basename(path), 'see "Live processing hook" in the README')
        self._setup = getattr(mod, 'setup', None)
        self._finish = getattr(mod, 'finish', None)
        self.calls = self.events = 0
        self.seconds = 0.0
        self.line_seconds = 0.0          # since the last live line
        self.last_event = None
        self.stop = False
        self.delay_last = self.delay_max = None   # lag: s of newer signal the scope had taken when a call ended

    def setup(self, info):
        if callable(self._setup):
            try:
                self._setup(info)
            except BaseException as e:               # noqa: BLE001 - user code, sys.exit() included
                raise HookError('setup', e) from None

    def call(self, block):
        """-> the event dict or None. Raises HookError."""
        t = time.perf_counter()
        try:
            r = self.process(block)
        except BaseException as e:                   # noqa: BLE001 - user code: sys.exit() must not kill the writer
            raise HookError('process', e) from None
        finally:
            dt = time.perf_counter() - t
            self.calls += 1
            self.seconds += dt
            self.line_seconds += dt
        if r is None:
            return None
        if not isinstance(r, dict):
            raise HookError('process', TypeError('process() must return None or a dict, not %s' % type(r).__name__))
        r = _plain_dict(r, 'process')
        self.events += 1
        self.last_event = r
        if r.get('stop'):
            self.stop = True
        return r

    def note_delay(self, d):
        self.delay_last = d
        self.delay_max = d if self.delay_max is None else max(self.delay_max, d)

    def finish(self, summary):
        if not callable(self._finish):
            return None
        try:
            r = self._finish(summary)
        except BaseException as e:                   # noqa: BLE001
            raise HookError('finish', e) from None
        return _plain_dict(r, 'finish') if isinstance(r, dict) else None

    def report(self, signal_s):
        return {'file': self.path.replace('\\', '/'), 'calls': self.calls, 'events': self.events,
                'seconds': round(self.seconds, 3), 'load': round(self.seconds / signal_s, 4) if signal_s else None,
                'lag_max_s': None if self.delay_max is None else round(self.delay_max, 3),
                'stopped_run': self.stop, 'last_event': self.last_event}
