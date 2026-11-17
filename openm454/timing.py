"""Capture spacing: the waits the driver keeps around every run, and how a session may shorten them.

The spacing constants in acquire and transport reproduce the spacing recorded from DSWave 1.2.1 (docs/PROTOCOL.md
section 7). A profile keeps them as the baseline and shortens selected ones for one session:

    OPENM454_TIMING=gap=0.005,submit=0.005,setup=0.002,start=0.002,read=0.002,quiet=0.005

That is the floor profile (every key at its floor): about 26 ms per capture on the owner's Windows bench and 28.6 ms
on a second bench, against about 193 ms with the traced spacing.

Keys (seconds): gap, quiet, submit, setup, start, final, read. A profile may only shorten a wait, and only down to its
floor (MIN). Refused with INVALID_ARG before anything reaches the device: an entry that is not key=value, an unknown
or repeated key, a value that is not a finite non-negative number, above the traced value or below the floor.

Not part of a profile:
  * The hold after the stop of a run that ended while the device could still be waiting for a trigger (no head:
    a normal/auto timeout or a stop before the trigger; or a repeat run stopped before its last frame) is always the
    traced 100 ms (acquire.QUIET_AFTER_UNTRIGGERED_STOP): register writes sent within ~10 ms of such a stop were
    dropped on hardware (docs/PROTOCOL.md section 7, item 7). `quiet` shortens the hold only after a run that had a
    head (forced or triggered), the only kind of stop the floor evidence covers.
  * The analog front-end settling (scope.ANALOG_WAKE_SETTLE, scope.OVERDRIVE_SETTLE) is measured device physics, not
    spacing: the inputs read 8-14 codes high for ~80 ms after a digital-only capture and need ~215 ms after
    overdrive, so a shorter wait would shift the measured levels. `wake=` and `overdrive=` are INVALID_ARG.

The active profile is reported by `openm454 info` (`timing`), in every capture's metadata (`acq.timing`), in the log
result document and every saved log file (`acq.timing`), in the first line and the summary of `watch`, in the
selftest result (`timing`), and in the MCP summaries (capture/record `acq.timing`, status `timing`).
"""
import math
import os

from .errors import DscopeError

ENV = 'OPENM454_TIMING'
#: profile key -> (module, module-level name)
KEYS = {'gap': ('acquire', 'GAP_BETWEEN_RUNS'), 'quiet': ('acquire', 'QUIET_AFTER_STOP'),
        'submit': ('acquire', 'SUBMIT_TO_START'), 'setup': ('acquire', 'SETUP_SETTLE'),
        'start': ('acquire', 'START_SETTLE'), 'final': ('acquire', 'FINAL_GAP'),
        'read': ('transport', 'READ_DELAY')}
#: keys that are deliberately not adjustable, and why (an unknown key is refused; these get the reason)
FIXED = {'wake': 'the analog front-end settling (150 ms) is measured device physics, not spacing: the inputs read '
                 '8-14 codes high for ~80 ms after a digital-only capture, so a shorter wait shifts the measured levels',
         'overdrive': 'the recovery after a saturated input (300 ms) is measured device physics, not spacing: 1500 div '
                      'of overdrive needed ~215 ms to settle, so a shorter wait shifts the measured levels'}
#: The floors: the floor profile, the only set that has run without a failure on both benches that tested it
#: (300 forced and 300 normal-triggered captures on the owner's Windows bench; 10,000 captures on Linux and 30 on
#: Windows on a second bench). Below it the scope has wedged: DEVICE_ERROR in the EP 0x84 handshake, then no register
#: answer until a replug. `final` (W CTRL=0 -> W ACTION=4) is the stop handshake itself and is not shortened. Every
#: run behind these numbers was a forced or triggered capture; profiles, counts and results: docs/PROTOCOL.md 7.1.
MIN = {'gap': 0.005, 'quiet': 0.005, 'submit': 0.005, 'setup': 0.002, 'start': 0.002, 'read': 0.002, 'final': 0.001}
#: kept for callers that only ask about the post-stop quiet time
MIN_QUIET_AFTER_STOP = MIN['quiet']

_ACTIVE = {}
_FROM_ENV = True           # the profile in force came from the environment: every activate() reads it again
_BASELINE = {}


def profile(spec=None):
    """'gap=0.005,read=0.003' -> {'gap': 0.005, 'read': 0.003} (spec None: the environment); a malformed entry, an
    unknown, fixed or repeated key, or a value that is not a finite non-negative number raises INVALID_ARG."""
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
        if key in FIXED:
            raise DscopeError('INVALID_ARG', '%s: %r is not adjustable (keys: %s)' % (ENV, key, ', '.join(KEYS)),
                              FIXED[key] + '; remove it from the profile')
        if key not in KEYS:
            raise DscopeError('INVALID_ARG', '%s: unknown key %r (keys: %s)'
                              % (ENV, key, ', '.join(KEYS)),
                              'a misspelled key would otherwise be ignored silently')
        if key in out:
            raise DscopeError('INVALID_ARG', '%s: %r is given twice' % (ENV, key),
                              'give every key once: otherwise one of the two values would be ignored silently')
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
    from . import acquire, transport
    return {'acquire': acquire, 'transport': transport}


def baseline():
    """The traced values a profile may shorten (read once, before the first apply)."""
    if not _BASELINE:
        mods = _modules()
        for key, (mod, name) in KEYS.items():
            _BASELINE[key] = getattr(mods[mod], name)
    return dict(_BASELINE)


def current():
    """The value of every key in force right now (the traced value where the profile leaves it alone)."""
    mods = _modules()
    return {key: getattr(mods[mod], name) for key, (mod, name) in KEYS.items()}


def validate(spec=None):
    """-> the profile (spec None: the environment), checked against the traced values and the floors; nothing is
    applied. Raises INVALID_ARG."""
    prof = profile(spec)
    base = baseline()
    for key, v in prof.items():
        if v > base[key]:
            raise DscopeError('INVALID_ARG', '%s: %s=%g is above the traced %g (a profile only shortens)'
                              % (ENV, key, v, base[key]), 'omit the key to keep the traced value')
        if v < MIN[key]:
            raise DscopeError('INVALID_ARG', '%s: %s=%g is below the %.4g s floor' % (ENV, key, v, MIN[key]),
                              'below the floors the scope has wedged and needed a replug; the floor is the value '
                              'with hardware evidence (docs/PROTOCOL.md 7.1)')
    return prof


def activate(spec=None):
    """Validate and apply a profile; -> the active {'key': seconds}.

    spec None (every Scope() does this): read OPENM454_TIMING again, so a changed value is applied and an invalid one
    refused. A profile given here explicitly stays in force until the next activate() with a spec, or until the
    variable is set. Every key is set: the profile's value, or the traced one, so active() always names exactly what
    is applied. Raises INVALID_ARG before anything changes (and before anything reaches the device).
    """
    global _ACTIVE, _FROM_ENV
    if spec is None and not _FROM_ENV and ENV not in os.environ:
        return dict(_ACTIVE)
    prof = validate(spec)                       # everything is checked before the first constant is touched
    base = baseline()
    mods = _modules()
    for key, (mod, name) in KEYS.items():
        setattr(mods[mod], name, prof.get(key, base[key]))
    _ACTIVE = prof
    _FROM_ENV = spec is None
    return dict(_ACTIVE)


def active():
    """The profile in force for this process ({} when the spacing is the traced baseline)."""
    return dict(_ACTIVE)


def reset():
    """Undo activate(): restore the traced values (tests only)."""
    global _ACTIVE, _FROM_ENV
    mods = _modules()
    for key, v in baseline().items():
        mod, name = KEYS[key]
        setattr(mods[mod], name, v)
    _ACTIVE = {}
    _FROM_ENV = True
