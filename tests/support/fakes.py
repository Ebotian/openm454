"""Fake devices and engines shared by the offline tests (no hardware, no USB).

FakeDevice replaces Scope.dev: the zero info and the USB speed the scope would report, plus the transport state
Scope polls. OneShotEngine returns one prepared RawCapture and records the run kwargs; ScriptEngine walks a script
of (frame, trigger wait) pairs and builds one RawCapture per run, so a test can script misfires, retries and
no-trigger runs.
"""
import types

from openm454 import scope as scopemod
from openm454.errors import DscopeError
from openm454.acquire import RawCapture

HEAD = bytes(b'\x55\x55\x55\x55' + bytes(4092))       # triggered head


class FakeDevice:
    def __init__(self, speed='super'):
        self.zero_info = scopemod._cached_zero()
        self.info = {'usb_speed': speed} if speed else {}
        self.t = types.SimpleNamespace(running=False, stale=False)


class OneShotEngine:
    """One prepared RawCapture per test; calls keeps the _run kwargs (need_bytes, repeat_frames, ...)."""

    def __init__(self, raw):
        self.raw, self.calls = raw, []

    def run(self, prog, **kw):
        self.calls.append(kw)
        return self.raw


class ScriptEngine:
    """Each run takes the next (frame, trigger wait s) of the script; frame None = no trigger ('abort': aborted)."""

    def __init__(self, script, record_s):
        self.script, self.record_s, self.calls = list(script), record_s, []

    def run(self, prog, **kw):
        self.calls.append(kw)
        fr, waited = self.script.pop(0)
        raw = RawCapture()
        raw.t_start = 1000.0
        if fr is None or fr == 'abort':
            raw.aborted = fr == 'abort'
            return raw
        raw.head, raw.data = HEAD, bytearray(kw['need_bytes'])
        raw.t_head = raw.t_start + self.record_s + waited
        raw.frame = fr
        return raw


class FakeCap:
    def __init__(self, st, short=False):
        an, dg = list(st.analog), list(st.digital)
        rate = st.rate or 100e6
        self.n = int(rate * st.duration * (0.5 if short else 1.0)) + 32
        self.meta = {'acq': {'analog_rate_hz': rate if an else None, 'digital_rate_hz': rate if dg else None,
                             'stop': 'normal', 'complete': True, 'triggered': st.trig_mode != 'force'}}

    def measure(self, ch, edges_n=0):
        return {'samples': self.n, 'state': 'low', 'vpp_v': 0.0}


class SelftestScope:
    def __init__(self, short=False, fail_on=None, awg_enabled=None, fail_while_driven=False):
        self.calls, self.short, self.fail_on = [], short, fail_on
        self.awg_state = None if awg_enabled is None else {'enabled': awg_enabled}
        self.driven, self.fail_while_driven = False, fail_while_driven

    def info(self):
        return {'device': {'fpga_done': True, 'security_pass': True, 'fw_version': '0x3110', 'usb_speed': 'super',
                           'fpga_bin_version': '0x3100', 'panel_version': '0x3100'}}

    def capture(self, st):
        self.calls.append(('capture', st.trig_mode, tuple(st.analog), tuple(st.digital), st.rate))
        if self.fail_on and len(self.calls) == self.fail_on:
            raise DscopeError('USB_ERROR', 'transfer failed')
        if self.fail_while_driven and self.driven:
            raise DscopeError('TIMEOUT_NO_TRIGGER', 'no trigger')
        return FakeCap(st, self.short)

    def awg_status(self):                    # host-side record of the last setting: reading it touches nothing
        return self.awg_state

    def awg_set(self, *a, **k):
        self.calls.append(('awg_set',))
        self.driven = True

    def awg_off(self):
        self.calls.append(('awg_off',))
        self.driven = False
