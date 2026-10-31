"""Acquisition engine against a simulated M454 (no hardware).

The simulator enforces the rules recovered from DSWave (re/notes/acquisition_protocol.md):
  * while the device runs, control transfers are only legal between EP 0x84 bytes 02 and 03
  * the device streams until it is stopped; it must never find EP 0x86 without a pending read
  * after a failed handshake the host must not send anything
"""
import threading
import time

import pytest
import usb1

from dscope import regs
from dscope.acquire import Engine, HEAD_BYTES
from dscope.errors import DscopeError
from dscope.transport import Transport, REQ_WRITE, REQ_READ_SETUP, REQ_READ

PKT = 8192


class Sim:
    def __init__(self, head_after=0.0, trigger=True, ep84='ok', zlp_at=None, fail_hs_at=None):
        self.regs = {}
        self.log = []              # ('W'|'R'|'EP84'|'cancel', ...)
        self.violations = []
        self.pending = []          # submitted transfers, in order
        self.running = False       # CTRL=1 written, not yet stopped by the handshake
        self.streaming = False
        self.hs = 0                # 0 idle, 1 after byte 01, 2 inside (after byte 02)
        self.hs_count = 0
        self.head_after = head_after
        self.trigger = trigger
        self.ep84_mode = ep84
        self.fail_hs_at = fail_hs_at
        self.zlp_at = zlp_at
        self.t_run = None
        self.head_sent = False
        self.data_sent = 0
        self.starved = False
        self.after_fail = []       # control transfers seen after a handshake failure
        self.failed = False

    # ---------------------------------------------------------------- device behaviour
    def ctrl(self, kind, addr, val=None):
        if self.failed:
            self.after_fail.append((kind, addr, val))
        if self.running and self.hs != 2:
            self.violations.append('%s 0x%02X outside handshake' % (kind, addr))
        self.log.append((kind, addr, val, self.hs == 2))
        if kind == 'W':
            self.regs[addr] = val
            if addr == regs.CTRL and val == 1:
                self.running = self.streaming = True
                self.t_run = time.time()
            if addr == regs.ACTION and val == 8 and self.hs == 2:
                self.streaming = False

    def ep84(self):
        self.hs_count += 1
        if self.ep84_mode == 'timeout' or (self.fail_hs_at is not None and self.hs_count >= self.fail_hs_at):
            self.failed = True
            raise usb1.USBErrorTimeout()
        self.log.append(('EP84', self.hs))
        if self.hs == 0:
            self.hs = 1
            return 1
        if self.hs == 1:
            self.hs = 2
            return 2
        self.hs = 0
        if not self.streaming:
            self.running = False
        return 3

    def pump(self):
        done = []
        for tr in list(self.pending):
            if tr._cancel:
                tr._finish(usb1.TRANSFER_CANCELLED, 0)
                done.append(tr)
        for tr in done:
            self.pending.remove(tr)
            tr._fire()
        if not self.streaming:
            return
        if not self.head_sent:
            if not self.trigger or time.time() - self.t_run < self.head_after:
                return
            head = [tr for tr in self.pending if tr._n == HEAD_BYTES]
            if not head:
                self.starved = True
                return
            tr = head[0]
            tr._buf[:4] = b'\x55\x55\x55\x55'
            tr._finish(usb1.TRANSFER_COMPLETED, HEAD_BYTES)
            self.pending.remove(tr)
            self.head_sent = True
            tr._fire()
            return
        data = [tr for tr in self.pending if tr._n != HEAD_BYTES]
        if not data:
            self.starved = True
            return
        for tr in data[:4]:
            self.data_sent += 1
            n = 0 if self.zlp_at is not None and self.data_sent >= self.zlp_at else tr._n
            tr._buf[:n] = bytes([self.data_sent & 0xFF]) * n
            tr._finish(usb1.TRANSFER_COMPLETED, n)
            self.pending.remove(tr)
            tr._fire()


class FakeTransfer:
    def __init__(self, sim):
        self.sim = sim
        self._submitted = False
        self._cancel = False

    def setBulk(self, ep, n, callback=None, timeout=0):
        assert ep == 0x86 and timeout == 0
        self._n, self._cb, self._buf = n, callback, bytearray(n)

    def submit(self):
        assert not self._submitted
        self._submitted, self._cancel = True, False
        self.sim.pending.append(self)

    def isSubmitted(self):
        return self._submitted

    def cancel(self):
        self.sim.log.append(('cancel',))
        self._cancel = True

    def _finish(self, status, n):
        self._status, self._len = status, n

    def _fire(self):
        self._submitted = False
        self._cb(self)

    def getStatus(self):
        return self._status

    def getActualLength(self):
        return self._len

    def getBuffer(self):
        return self._buf

    def close(self):
        assert not self._submitted


class FakeHandle:
    def __init__(self, sim):
        self.sim = sim
        self._read = None

    def getTransfer(self):
        return FakeTransfer(self.sim)

    def controlWrite(self, rt, req, v, i, data, timeout=0):
        self.sim.pump()
        if req == REQ_WRITE:
            self.sim.ctrl('W', data[0], data[1] | data[2] << 8)
        elif req == REQ_READ_SETUP:
            self._read = data[0]
        return len(data)

    def controlRead(self, rt, req, v, i, n, timeout=0):
        assert req == REQ_READ
        self.sim.pump()
        self.sim.ctrl('R', self._read)
        return (0x0006).to_bytes(2, 'little')[:n]

    def bulkRead(self, ep, n, timeout=0):
        assert ep == 0x84 and n == 1
        self.sim.pump()
        return bytes([self.sim.ep84()])


class FakeCtx:
    def __init__(self, sim):
        self.sim = sim

    def handleEventsTimeout(self, tv=0):
        self.sim.pump()
        time.sleep(0.001)


class FakeDevice:
    def __init__(self, sim):
        self.t = Transport()
        self.t.h, self.t.ctx = FakeHandle(sim), FakeCtx(sim)


SETUP = [(regs.CTRL, 0), (0x10, 0x1234), (regs.MODE, 0), (regs.FLUSH_DATA, 0)]


def engine(**kw):
    sim = Sim(**kw)
    return sim, Engine(FakeDevice(sim))


def writes(sim):
    return [(e[1], e[2]) for e in sim.log if e[0] == 'W']


def test_triggered_capture_keeps_reads_queued_until_stop():
    sim, eng = engine()
    cap = eng.run(SETUP, pkt_bytes=PKT, need_bytes=5 * PKT, n_transfers=4, trigger_timeout=1.0)
    assert cap.stop == 'normal' and cap.head is not None and len(cap.data) >= 5 * PKT
    assert sim.violations == [] and not sim.starved
    assert cap.ep84 == [1, 2, 3]                        # one handshake: the stop
    w = writes(sim)
    assert w[-4:] == [(regs.CTRL, 0), (regs.ACTION, 8), (regs.CTRL, 0), (regs.ACTION, 4)]
    # the CTRL=0/ACTION=8 pair is inside the handshake, the final pair after every read was cancelled and back
    inside = [e for e in sim.log if e[0] == 'W' and e[3]]
    assert [(e[1], e[2]) for e in inside] == [(regs.CTRL, 0), (regs.ACTION, 8)]
    last_cancel = max(i for i, e in enumerate(sim.log) if e[0] == 'cancel')
    assert sim.log[-2][:3] == ('W', regs.CTRL, 0) and last_cancel < len(sim.log) - 2
    assert not any(tr.isSubmitted() for tr in eng._trs)
    assert not eng.t.running and not eng.t.failed


def test_start_order_and_setup():
    sim, eng = engine()
    eng.run(SETUP, pkt_bytes=PKT, need_bytes=PKT, n_transfers=2, trigger_timeout=1.0)
    w = writes(sim)
    assert w[:4] == SETUP and w[4:7] == [(regs.SYNC, 0), (regs.ACTION, 0), (regs.CTRL, 1)]


def test_no_trigger_polls_progress_then_stops_normally():
    sim, eng = engine(trigger=False)
    cap = eng.run(SETUP, pkt_bytes=PKT, need_bytes=PKT, n_transfers=2, trigger_timeout=0.4)
    assert cap.head is None and cap.timed_out and cap.stop == 'normal'
    assert len(cap.progress) >= 5 and set(cap.progress) == {6}
    assert sim.violations == []
    kinds = [h[1] for h in cap.handshakes]
    assert kinds[-1] == 'stop' and set(kinds[:-1]) == {'poll'}
    assert cap.handshakes[0][0] >= 100                  # polling starts 100 ms after the start writes
    assert cap.ep84 == [1, 2, 3] * len(cap.handshakes)
    reads = [e for e in sim.log if e[0] == 'R']
    assert reads and all(e[1] == regs.ACTION and e[3] for e in reads)


def test_failed_stop_handshake_sends_nothing_more():
    sim, eng = engine(ep84='timeout')
    with pytest.raises(DscopeError) as ei:
        eng.run(SETUP, pkt_bytes=PKT, need_bytes=2 * PKT, n_transfers=2, trigger_timeout=1.0)
    assert ei.value.code == 'DEVICE_ERROR'
    assert sim.after_fail == []                         # no control transfer after the failed EP 0x84 read
    assert eng.t.failed and not eng.t.running
    assert not any(tr.isSubmitted() for tr in eng._trs)
    with pytest.raises(DscopeError) as ei:
        eng.t.write(regs.CTRL, 0)
    assert ei.value.code == 'DEVICE_ERROR'
    with pytest.raises(DscopeError):
        eng.run(SETUP, pkt_bytes=PKT, need_bytes=PKT)


def test_failed_poll_handshake_goes_to_error_stop():
    sim, eng = engine(trigger=False, fail_hs_at=4)      # 01 02 03 then the next poll times out
    with pytest.raises(DscopeError) as ei:
        eng.run(SETUP, pkt_bytes=PKT, need_bytes=PKT, n_transfers=2, trigger_timeout=1.0)
    assert ei.value.code == 'DEVICE_ERROR' and 'EP 0x84' in ei.value.message
    assert sim.after_fail == [] and sim.violations == []


def test_zero_length_data_is_an_error_stop():
    sim, eng = engine(zlp_at=3)
    with pytest.raises(DscopeError) as ei:
        eng.run(SETUP, pkt_bytes=PKT, need_bytes=100 * PKT, n_transfers=4, trigger_timeout=1.0)
    assert 'zero-length' in ei.value.message
    assert eng.t.failed and sim.violations == []
    assert writes(sim)[-1] == (regs.CTRL, 1)            # nothing written after the start


def test_register_access_refused_while_running():
    sim, eng = engine()
    seen = []

    def on_chunk(buf):
        if len(buf) == PKT and not seen:
            with pytest.raises(DscopeError) as ei:
                eng.t.write(regs.CTRL, 0)
            seen.append(ei.value.code)
    cap = eng.run(SETUP, pkt_bytes=PKT, need_bytes=3 * PKT, n_transfers=4, trigger_timeout=1.0, on_chunk=on_chunk)
    assert seen == ['UNSAFE_OPERATION'] and cap.stop == 'normal' and sim.violations == []


def test_abort_from_another_thread_stops_normally():
    sim, eng = engine(trigger=False)
    threading.Timer(0.25, eng.abort).start()
    t0 = time.time()
    cap = eng.run(SETUP, pkt_bytes=PKT, need_bytes=PKT, n_transfers=2, trigger_timeout=10.0)
    assert time.time() - t0 < 2.0
    assert cap.aborted and cap.stop == 'normal' and sim.violations == []
    assert eng.wait_idle(0.1)


def test_exception_in_on_chunk_still_stops_cleanly():
    sim, eng = engine()

    def boom(buf):
        if len(buf) == PKT:
            raise ValueError('decoder bug')
    with pytest.raises(ValueError):
        eng.run(SETUP, pkt_bytes=PKT, need_bytes=4 * PKT, n_transfers=4, trigger_timeout=1.0, on_chunk=boom)
    assert sim.violations == [] and not eng.t.running and not eng.t.failed
    assert writes(sim)[-2:] == [(regs.CTRL, 0), (regs.ACTION, 4)]


def test_repeat_frames_mode_does_not_poll():
    sim, eng = engine(head_after=0.3)
    cap = eng.run(SETUP, pkt_bytes=PKT, need_bytes=None, n_transfers=2, trigger_timeout=1.0, repeat_frames=3)
    assert len(cap.frames) == 3 and cap.progress == [] and cap.stop == 'normal'


def test_runs_are_spaced_100ms():
    sim, eng = engine()
    eng.run(SETUP, pkt_bytes=PKT, need_bytes=PKT, n_transfers=2, trigger_timeout=1.0)
    t_end = eng._t_end
    sim.head_sent, sim.streaming = False, False
    eng.run(SETUP, pkt_bytes=PKT, need_bytes=PKT, n_transfers=2, trigger_timeout=1.0)
    assert sim.t_run - t_end >= 0.1


def test_device_close_sends_nothing_after_failure():
    from dscope.device import Device
    sim = Sim()
    d = Device()
    d.t.h, d.t.ctx = FakeHandle(sim), FakeCtx(sim)
    d.opened = True
    d.t.fail('test')
    n = len(sim.log)
    d.t.close = lambda: None
    d.close()
    assert len(sim.log) == n
