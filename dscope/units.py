"""Parsing of SI-suffixed values and channel lists used by the CLI."""
import re

from .errors import DscopeError

_SI = {'G': 1e9, 'M': 1e6, 'k': 1e3, 'K': 1e3, '': 1.0, 'm': 1e-3, 'u': 1e-6, 'µ': 1e-6, 'n': 1e-9, 'p': 1e-12}
_RX = re.compile(r'^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([GMkKmuµnp]?)\s*([a-zA-Z%/]*)\s*$')


def si(text, unit=None, what='value'):
    """'500mV' -> 0.5, '10k' -> 10000, '200us' -> 2e-4, '50%' -> 50, '10x' -> 10."""
    if isinstance(text, (int, float)):
        return float(text)
    m = _RX.match(str(text))
    if not m:
        raise DscopeError('INVALID_ARG', 'cannot parse %s %r' % (what, text))
    num, pre, u = m.groups()
    if u and u not in ('%', 'x', 'X') and unit and u.lower() != unit.lower() \
            and not (unit == 'Hz' and u.lower() in ('sa/s', 'sps', 's/s')):
        raise DscopeError('INVALID_ARG', 'unexpected unit %r for %s (expected %s)' % (u, what, unit))
    return float(num) * _SI[pre]


def channel_list(text):
    """'A0-A3,D0-D7' / 'all' / 'analog' / 'digital' / 'A1,D0' -> (['A0',..], ['D0',..])"""
    if text is None:
        return None
    t = text.replace(' ', '')
    if t.lower() in ('all', '*'):
        t = 'A0-A3,D0-D7'
    elif t.lower() == 'analog':
        t = 'A0-A3'
    elif t.lower() == 'digital':
        t = 'D0-D7'
    elif t.lower() in ('none', ''):
        return [], []
    a, d = [], []
    for part in t.split(','):
        m = re.fullmatch(r'([AaDd])(\d)(?:-[AaDd]?(\d))?', part)
        if not m:
            raise DscopeError('INVALID_ARG', 'bad channel spec %r (use e.g. A0-A3,D0-D7)' % part)
        k, lo, hi = m.group(1).upper(), int(m.group(2)), int(m.group(3) or m.group(2))
        for i in range(lo, hi + 1):
            if (k == 'A' and i > 3) or (k == 'D' and i > 7):
                raise DscopeError('INVALID_ARG', 'no such channel %s%d' % (k, i))
            (a if k == 'A' else d).append('%s%d' % (k, i))
    return sorted(set(a)), sorted(set(d), key=lambda c: int(c[1:]))


def per_channel(text, unit, what, default=None, valid=('A0', 'A1', 'A2', 'A3')):
    """'1V' -> {all: 1.0}; 'A0=500mV,A1=2V' -> {'A0': .5, 'A1': 2}. Returns dict ch->value (missing = default)."""
    out = {c: default for c in valid}
    if text is None:
        return out
    for part in str(text).split(','):
        if '=' in part:
            k, v = part.split('=', 1)
            k = k.strip().upper()
            if k not in valid:
                raise DscopeError('INVALID_ARG', 'bad channel %r in %s' % (k, what))
            out[k] = si(v, unit, what) if unit is not None else v.strip()
        else:
            val = si(part, unit, what) if unit is not None else part.strip()
            for c in valid:
                out[c] = val
    return out


def window(text):
    """'0:2ms' / '-1ms:1ms' / ':500us' -> (t0, t1) seconds (None = open)."""
    if not text:
        return None
    a, _, b = text.partition(':')
    return (si(a, 's', 'window') if a else None, si(b, 's', 'window') if b else None)
