"""The bench wiring the self-test may assume (state/fixture.json; write it whenever the wiring changes).

{"analog": "standard", "digital": "standard"} is the self-test fixture (openm454/selftest.py). Any other value names
what is connected (e.g. a {channel: source} map); the self-test then skips the checks that need the calibration
square there.
A missing file means the standard fixture.
"""
import json
import os
import time

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'state', 'fixture.json')
STANDARD = {'analog': 'standard', 'digital': 'standard'}


def load(path=PATH):
    try:
        with open(path, encoding='utf-8') as f:
            d = json.load(f)
    except (OSError, ValueError):
        return dict(STANDARD)
    return {**STANDARD, **d}


def save(analog='standard', digital='standard', note=None, path=PATH):
    d = {'analog': analog, 'digital': digital, 'since': time.strftime('%Y-%m-%d %H:%M')}
    if note:
        d['note'] = note
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(d, f, indent=1)
    return d
