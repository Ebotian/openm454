"""USB transport + device simulator shared by the engine and scope tests (no hardware).

Sim models the M454's USB3 endpoint behavior the acquisition engine depends on: queued reads, short packets,
stalls, the stop handshake and the usbfs memory budget. FakeHandle/FakeCtx/FakeDevice plug it into the real
Engine through a Transport-shaped object, so the tests exercise the production code path and not a mock.
"""
import threading
import time

import usb1

from openm454 import regs
from openm454.acquire import HEAD_BYTES
from openm454.transport import Transport, REQ_WRITE, REQ_READ_SETUP, REQ_READ, usbfs_memory_mb


PKT = 8192


class Sim:
    def __init__(self, head_after=0.0, trigger=True, ep84='ok', zlp_at=None, fail_hs_at=None, submit_error=None,
                 no_mem_at=()):
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
        self.submit_error = submit_error   # raised by submit(): a host-side failure, nothing reaches the device
        self.no_mem_at = set(no_mem_at)    # submit() calls (1 = the head) that libusb refuses with NO_MEM
        self.submits = 0
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
        self.sim.submits += 1
        if self.sim.submit_error is not None:    # as libusb: the transfer is refused, never becomes pending
            raise self.sim.submit_error
        if self.sim.submits in self.sim.no_mem_at:
            raise usb1.USBError(usb1.ERROR_NO_MEM)
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
