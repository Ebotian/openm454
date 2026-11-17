"""CLI helpers shared by the CLI tests: JSON lines, argument parsing, and the watch fake clock/scope."""
import json

import numpy as np

from openm454 import cli
from openm454.capture import Capture

RATE = 1e6
VPC = 1 / 29.375                       # volts per code at 1 V/div


def json_lines(capsys):
    """Every JSON line the CLI printed since the last read."""
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]


def last_json(capsys):
    """The last JSON line the CLI printed (errors are reported as a JSON object, not a traceback)."""
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def args_of(*argv):
    return cli.build_parser().parse_args(list(argv))


def square(n=10_000, period=1000, high=250, jitter=None):
    """0/1 square: rising edges every `period` samples (or at the given list of periods), high for `high` samples."""
    b = np.zeros(n, np.uint8)
    t = 100
    for p in (jitter or []) + [period] * (n // period + 1):
        if t >= n:
            break
        b[t:t + high] = 1
        t += p
    return b


def make_cap(bits=None):
    bits = square() if bits is None else bits
    meta = {'channels': {'A1': {'zero_code': 128, 'vdiv_v': 1.0, 'volts_per_code': VPC, 'probe': 1.0}},
            'analog_rate_hz': RATE, 'digital_rate_hz': RATE, 't0_analog_s': -0.005, 't0_digital_s': -0.005,
            'acq': {'triggered': True, 'samples': bits.size}, 'applied': {'channels': ['A1', 'D0'], 'trigger': {}},
            'hints': [], 'warnings': []}
    codes = (128 + np.round(bits * 2.8 / VPC)).astype(np.uint8)          # 0 V / 2.8 V square
    return Capture(meta, {'A1': codes}, {'D0': bits.copy()})


class Clock:
    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def sleep(self, dt):
        assert dt > 0
        self.t += dt


class FakeScope:
    """capture() takes durations[k] seconds of fake time (the first is slow, like a real session's first run)."""
    def __init__(self, clock, durations, on_capture=None):
        self.clock, self.durations, self.starts, self.on_capture = clock, list(durations), [], on_capture

    def capture(self, st):
        self.starts.append(round(self.clock.t - 1000.0, 6))
        k = len(self.starts) - 1
        self.clock.t += self.durations[min(k, len(self.durations) - 1)]
        if self.on_capture:
            self.on_capture(k)
        return make_cap()


def lines_of(capsys):
    return [json.loads(x) for x in capsys.readouterr().out.splitlines()]
