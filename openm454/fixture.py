"""The bench wiring the self-test may assume (state/fixture.json; write it whenever the wiring changes).

{"analog": "standard", "digital": "standard"} is the self-test fixture (openm454/selftest.py). Any other value names
what is connected (e.g. a {channel: source} map); the self-test then skips the checks that need the calibration
square there. The scope has no internal loopback, so those checks always need this wiring (its own 1 kHz
calibration output and signal-generator output cabled to its inputs).
A missing file (e.g. a fresh pip install) means the wiring is unknown: only the wiring-free checks run.
"""
import json
import os
import time

from .paths import ROOT

PATH = os.path.join(ROOT, 'state', 'fixture.json')
STANDARD = {'analog': 'standard', 'digital': 'standard'}
UNKNOWN = {'analog': 'unknown', 'digital': 'unknown'}


def load(path=PATH):
    """-> {'analog': .., 'digital': .., ...}; unknown wiring when the file is missing, and also when it cannot be read
    (then with an 'error' saying why). A UTF-8 byte-order mark (PowerShell's Out-File) is accepted."""
    try:
        with open(path, encoding='utf-8-sig') as f:
            d = json.load(f)
    except FileNotFoundError:
        return dict(UNKNOWN)
    except (OSError, ValueError) as e:
        return dict(UNKNOWN, error='%s could not be read (%s): wiring treated as unknown' % (os.path.basename(path), e))
    if not isinstance(d, dict):
        return dict(UNKNOWN, error='%s is not a JSON object: wiring treated as unknown' % os.path.basename(path))
    return {**UNKNOWN, **d}


def save(analog='standard', digital='standard', note=None, path=PATH):
    d = {'analog': analog, 'digital': digital, 'since': time.strftime('%Y-%m-%d %H:%M')}
    if note:
        d['note'] = note
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(d, f, indent=1)
    return d
