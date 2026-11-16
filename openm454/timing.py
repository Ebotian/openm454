"""Capture spacing: the waits that reproduce DSWave's recordings, and how a session may shorten them.

The spacing constants in acquire/transport/scope come from DSWave 1.2.1 recordings: reproducing the numbers is
how the driver stays faithful without measuring anything.  A profile keeps those constants as the baseline and
shortens selected ones, for a session whose fidelity is judged by the signal that comes back instead.

    OPENM454_TIMING=gap=0.005,submit=0.02,setup=0.005,start=0.005,read=0.003,quiet=0.005

Keys (seconds): gap, submit, setup, start, final, read, wake, overdrive, quiet.  A profile may only shorten a
wait, and only down to MIN: below those values the scope has wedged (DEVICE_ERROR in the handshake, then no answer
until it is replugged) on both the maintainer's bench and this one.  MIN is the conservative profile that ran
300/300 captures there and 100+/100+ here; every other key may be zeroed.  See docs/PROTOCOL.md, section 7.

The active profile is reported by `openm454 info`, in every capture's metadata (`acq.timing`) and in the log's
final line, so a measurement taken with a shortened profile cannot be mistaken for a baseline one.
"""
import math
import os

from .errors import DscopeError

ENV = 'OPENM454_TIMING'
#: profile key -> (module, module-level name)
KEYS = {'gap': ('acquire', 'GAP_BETWEEN_RUNS'), 'submit': ('acquire', 'SUBMIT_TO_START'),
        'setup': ('acquire', 'SETUP_SETTLE'), 'start': ('acquire', 'START_SETTLE'),
        'final': ('acquire', 'FINAL_GAP'), 'read': ('transport', 'READ_DELAY'),
        'wake': ('scope', 'ANALOG_WAKE_SETTLE'), 'overdrive': ('scope', 'OVERDRIVE_SETTLE'),
        'quiet': ('acquire', 'QUIET_AFTER_STOP')}
#: The lowest value of each key with hardware evidence: the conservative profile, which ran 300 captures in each
#: trigger mode on the maintainer's Windows bench (2026-11-13) and 100+ captures per run here (2026-11-16).  The
#: fully zeroed profile wedges the scope after 8-15 captures (DEVICE_ERROR: "EP 0x84 handshake: expected 02, got
#: timeout", then no register answer until a replug), and adding any single key back does not make it safe: `quiet`
#: alone survived 30 in one session and died at 15 in another.  `final` is the stop handshake itself (W CTRL=0,
#: 1 ms, W ACTION=4) and is not shortened at all.
MIN = {'gap': 0.005, 'submit': 0.005, 'setup': 0.002, 'start': 0.002, 'read': 0.002, 'quiet': 0.005, 'final': 0.001}
#: kept for callers that only ask about the post-stop quiet time
MIN_QUIET_AFTER_STOP = MIN['quiet']

_ACTIVE = {}
_BASELINE = {}


def profile(spec=None):
    """'gap=0.005,read=0.003' -> {'gap': 0.005, 'read': 0.003}; a bad key or value raises INVALID_ARG."""
    spec = os.environ.get(ENV) if spec is None else spec
    out = {}
    for item in (spec or '').replace(' ', '').split(','):
        if not item:
            continue
        key, eq, value = item.partition('=')
        if not eq:
            raise DscopeError('INVALID_ARG', '%s: "%s" is not key=value (keys: %s)'
                              % (ENV, item, ', '.join(KEYS)),
                              'pass seconds, e.g. %s=gap=0.005,read=0.003' % ENV)
        if key not in KEYS:
            raise DscopeError('INVALID_ARG', '%s: unknown key %r (keys: %s)'
                              % (ENV, key, ', '.join(KEYS)),
                              'a misspelled key would otherwise be ignored silently')
        try:
            v = float(value)
        except ValueError:
            raise DscopeError('INVALID_ARG', '%s: %s=%s is not a number' % (ENV, key, value),
                              'pass seconds, e.g. %s=%s=0.005' % (ENV, key))
        if not math.isfinite(v) or v < 0:
            raise DscopeError('INVALID_ARG', '%s: %s=%s is not a finite, non-negative time' % (ENV, key, value),
                              'pass seconds >= 0')
        out[key] = v
    return out


def _modules():
    from . import acquire, scope, transport
    return {'acquire': acquire, 'scope': scope, 'transport': transport}


def baseline():
    """The traced values a profile may shorten (read once, before the first apply)."""
    if not _BASELINE:
        mods = _modules()
        for key, (mod, name) in KEYS.items():
            _BASELINE[key] = getattr(mods[mod], name)
    return dict(_BASELINE)


def activate(spec=None):
    """Validate and apply the profile (once, when read from the environment); -> the active {'key': seconds}.

    Raises INVALID_ARG before anything reaches the device: a profile is a host-side decision only.
    """
    global _ACTIVE
    if _ACTIVE and spec is None:
        return dict(_ACTIVE)
    prof = profile(spec)
    base = baseline()
    for key, v in prof.items():                 # validate everything before the first constant is touched
        if v > base[key]:
            raise DscopeError('INVALID_ARG', '%s: %s=%g is above the traced %g (a profile only shortens)'
                              % (ENV, key, v, base[key]), 'omit the key to keep the traced value')
        if key in MIN and v < MIN[key]:
            raise DscopeError('INVALID_ARG', '%s: %s=%g is below the %.4g s floor' % (ENV, key, v, MIN[key]),
                              'with the spacing fully zeroed the scope wedges after a handful of captures and needs '
                              'a replug; the floor is the value with hardware evidence (docs/PROTOCOL.md 7.1)')
    mods = _modules()
    for key, v in prof.items():
        mod, name = KEYS[key]
        setattr(mods[mod], name, v)
    _ACTIVE = prof
    return dict(_ACTIVE)


def active():
    """The profile in force for this process ({} when the spacing is the traced baseline)."""
    return dict(_ACTIVE)


def reset():
    """Undo activate(): restore the traced values (tests only)."""
    global _ACTIVE
    mods = _modules()
    for key, v in baseline().items():
        mod, name = KEYS[key]
        setattr(mods[mod], name, v)
    _ACTIVE = {}
