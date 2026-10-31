"""Acquisition engine: run one register-programmed capture and collect the raw stream.

Mirrors DSWave 1.2.1 (UsbDevice_TransferThread 0x33f950, UsbBsp3x Setup/Start/StopAcquire and the UsbTransfer
callbacks; evidence and RVAs in re/notes/acquisition_protocol.md):

  1. >= 100 ms after the previous run: setup program (W 0x01=0, 1 ms, MSO writes ..., W 0x00 MODE, W 0xF9=0)
  2. submit EP 0x86 reads: one 4096-byte head transfer + N data transfers (no timeout); wait 50 ms
  3. start: W SYNC=sync, W ACTION=0, W CTRL=1; 1 ms.  From here until the stop handshake is done every register
     access sits inside an EP 0x84 handshake (Transport.running / Transport.handshake)
  4. collect: data transfers are resubmitted from their callback for the WHOLE run, also after the frame is
     complete, because the device keeps streaming zero padding until it is stopped.  While the head has not
     arrived the progress register is polled like DSWave (handshake + R 0x02, every ~15 ms from 100 ms on)
  5. normal stop: handshake{W CTRL=0, W ACTION=8}; stop resubmitting; cancel every transfer and wait until all
     are back; W CTRL=0, 1 ms, W ACTION=4
     error stop (failed handshake or control transfer, bad head, transfer error, zero-length transfer,
     resubmit failure): nothing more is sent to the device; cancel, wait, mark the session failed and raise
     DEVICE_ERROR.  The next open resets the acquisition state (device.open: W CTRL=0, 1 ms, W ACTION=4).
"""
import collections
import struct
import threading
import time

import usb1

from . import regs
from .errors import DscopeError
from .transport import HINT_REOPEN

HEAD_BYTES = 4096
HEAD_MAGIC = b'\x55\x55\x55\x55'
EP_DATA = 0x86
GAP_BETWEEN_RUNS = 0.100       # FUN_14033f270: 100 ms after a stop before the next setup
SETUP_SETTLE = 0.001           # SetupAcquire: 1 ms after W CTRL=0
SUBMIT_TO_START = 0.050        # TransferThread: 50 ms between submitting and the start writes
POLL_AFTER = 0.100             # progress polling starts 100 ms after the start writes
POLL_EVERY = 0.015
DRAIN_TIMEOUT = 2.0            # cancelled transfers must be back within this time

_STATUS = {usb1.TRANSFER_COMPLETED: 'COMPLETED', usb1.TRANSFER_ERROR: 'ERROR', usb1.TRANSFER_TIMED_OUT: 'TIMED_OUT',
           usb1.TRANSFER_CANCELLED: 'CANCELLED', usb1.TRANSFER_STALL: 'STALL', usb1.TRANSFER_NO_DEVICE: 'NO_DEVICE',
           usb1.TRANSFER_OVERFLOW: 'OVERFLOW'}


class RawCapture:
    def __init__(self):
        self.head = None
        self.data = bytearray()
        self.frames = []          # repeat mode: one entry per data transfer (frame data + 4 KiB tail)
        self.t_start = None
        self.t_head = None
        self.t_done = None
        self.timed_out = False
        self.aborted = False
        self.ep84 = []            # every EP 0x84 handshake byte, in order (None = timeout)
        self.handshakes = []      # (ms after start, 'poll'|'stop', [bytes])
        self.progress = []        # raw R 0x02 values from the progress polls
        self.stop = None          # 'normal' | 'error'
        self.error = None

    @property
    def head_words(self):
        return struct.unpack('<8I', self.head[:32]) if self.head else None

    @property
    def triggered(self):
        w = self.head_words
        return None if w is None else not (w[2] & 0x80000000)


class Engine:
    def __init__(self, device):
        self.dev = device
        self.t = device.t
        self.sync = 0
        self.active = False
        self._t_end = 0.0
        self._abort = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._stale = []          # transfers that never came back after a cancel (kept alive, never freed)

    # ------------------------------------------------------------------ cross-thread control
    def abort(self):
        """Ask a running capture to stop (thread-safe). The run loop performs the normal stop handshake."""
        self._abort.set()

    def wait_idle(self, timeout):
        return self._idle.wait(timeout)

    # ------------------------------------------------------------------ transfers
    def _submit(self, n):
        tr = self.t.h.getTransfer()
        tr.setBulk(EP_DATA, n, callback=self._cb, timeout=0)
        self._trs.append(tr)
        tr.submit()
        return tr

    def _cb(self, tr):
        """Runs inside libusb event handling (handleEventsTimeout and the synchronous EP 0x84 reads). Data
        transfers are resubmitted here, like UsbTransfer_DataCallback, so EP 0x86 stays drained while the main
        loop is busy (handshakes, on_chunk)."""
        if self._stopping:
            return
        st = tr.getStatus()
        n = tr.getActualLength()
        if tr is self._head_tr:
            if st == usb1.TRANSFER_COMPLETED:
                self._events.append(('head', bytes(tr.getBuffer()[:n])))
            else:
                self._events.append(('error', 'head transfer ended with status %s' % _STATUS.get(st, st)))
            return
        if st != usb1.TRANSFER_COMPLETED:
            self._events.append(('error', 'data transfer ended with status %s' % _STATUS.get(st, st)))
            return
        if n == 0:
            self._events.append(('error', 'zero-length data transfer'))
            return
        if not self._full:
            self._events.append(('data', bytes(tr.getBuffer()[:n])))
        try:
            tr.submit()
        except usb1.USBError as e:
            self._events.append(('error', 'data transfer resubmit failed: %s' % e))

    def _cancel_and_wait(self):
        for tr in self._trs:
            try:
                if tr.isSubmitted():
                    tr.cancel()
            except usb1.USBError:
                pass
        end = time.time() + DRAIN_TIMEOUT
        while any(tr.isSubmitted() for tr in self._trs):
            if time.time() > end:
                self._stale.extend(tr for tr in self._trs if tr.isSubmitted())
                return False
            self.t.ctx.handleEventsTimeout(0.02)
        self._events.clear()
        for tr in self._trs:
            try:
                tr.close()
            except Exception:
                pass
        return True

    # ------------------------------------------------------------------ run
    def run(self, setup_writes, pkt_bytes, need_bytes, n_transfers=16, trigger_timeout=5.0,
            collect_timeout=10.0, repeat_frames=0, on_chunk=None, stop_flag=None, poll=True):
        """Execute one capture. need_bytes = data bytes (after the head) to collect.
        repeat_frames > 0: repeat mode; after the head every data transfer is one frame (data + 4 KiB tail);
        collect that many frames. on_chunk(bytes) is called for the head and for every data chunk as it arrives
        (live streaming). stop_flag(): optional callable; the capture stops when it returns True.
        Returns a RawCapture; cap.head is None when nothing arrived before trigger_timeout. Raises DEVICE_ERROR
        after an error stop (the session is then unusable until the device is reopened)."""
        t = self.t
        if t.failed:
            raise DscopeError('DEVICE_ERROR', 'device failed earlier in this session (%s)' % t.failed, HINT_REOPEN)
        gap = self._t_end + GAP_BETWEEN_RUNS - time.time()
        if gap > 0:
            time.sleep(gap)
        self._abort.clear()
        self._idle.clear()
        self.active = True
        try:
            return self._run(setup_writes, pkt_bytes, need_bytes, n_transfers, trigger_timeout, collect_timeout,
                             repeat_frames, on_chunk, stop_flag, poll)
        finally:
            self._t_end = time.time()
            self.active = False
            self._idle.set()

    def _run(self, setup_writes, pkt_bytes, need_bytes, n_transfers, trigger_timeout, collect_timeout,
             repeat_frames, on_chunk, stop_flag, poll):
        t = self.t
        cap = RawCapture()
        self._events = collections.deque()
        self._stopping = False
        self._full = False
        self._trs = []
        self._head_tr = None

        for i, (addr, val) in enumerate(setup_writes):
            t.write(addr, val)
            if i == 0 and addr == regs.CTRL:
                time.sleep(SETUP_SETTLE)
        try:
            self._head_tr = self._submit(HEAD_BYTES)
            for _ in range(n_transfers):
                self._submit(pkt_bytes)
        except usb1.USBError as e:
            self._stopping = True          # nothing was started: just take the reads back
            self._cancel_and_wait()
            raise DscopeError('USB_ERROR', 'cannot queue EP 0x86 reads: %s' % e, HINT_REOPEN)
        time.sleep(SUBMIT_TO_START)
        try:
            t.write(regs.SYNC, self.sync)
            t.write(regs.ACTION, 0x0000)
            t.write(regs.CTRL, 0x0001)
        except DscopeError as e:
            why = 'start write failed: %s' % e.message
            self._stop_error(cap, why)
            raise DscopeError('DEVICE_ERROR', why, HINT_REOPEN) from e
        t.running = True
        time.sleep(0.001)
        cap.t_start = time.time()

        try:
            error = self._collect(cap, need_bytes, trigger_timeout, collect_timeout, repeat_frames, on_chunk,
                                  stop_flag, poll)
        except DscopeError as e:           # a failed progress-poll handshake (the session is already failed)
            error = e.message
        except BaseException:              # KeyboardInterrupt, a bug in on_chunk: stop cleanly, then re-raise
            self._stop_normal(cap)
            cap.t_done = time.time()
            raise
        if error is None:
            error = self._stop_normal(cap)
        else:
            self._stop_error(cap, error)
        cap.t_done = time.time()
        if error:
            raise DscopeError('DEVICE_ERROR', error, HINT_REOPEN)
        return cap

    def _collect(self, cap, need_bytes, trigger_timeout, collect_timeout, repeat_frames, on_chunk, stop_flag,
                 poll):
        """Pump events until the record is complete, the caller stops, or a deadline passes. Returns None, or an
        error reason that requires the error stop."""
        t = self.t
        ctx = t.ctx
        deadline_head = cap.t_start + trigger_timeout
        deadline = None
        last_poll = 0.0
        polling = poll and not repeat_frames
        while True:
            ctx.handleEventsTimeout(0.005)
            while self._events:
                ev = self._events.popleft()
                kind, buf = ev
                if kind == 'error':
                    return buf
                if kind == 'head':
                    if len(buf) != HEAD_BYTES or buf[:4] != HEAD_MAGIC:
                        return 'bad head packet (%d bytes, check id %s)' % (len(buf), buf[:4].hex())
                    cap.head = buf
                    cap.t_head = time.time()
                    deadline = cap.t_head + collect_timeout
                elif self._full:
                    continue                # zero padding after the record: discarded
                elif repeat_frames:
                    cap.frames.append(buf)
                else:
                    cap.data += buf
                if on_chunk is not None:
                    on_chunk(buf)
                if self._enough(cap, need_bytes, repeat_frames):
                    self._full = True
            if self._full:
                return None
            if self._abort.is_set() or (stop_flag is not None and stop_flag()):
                cap.aborted = True
                return None
            now = time.time()
            if cap.head is None:
                if now > deadline_head:
                    cap.timed_out = True
                    return None
                if polling and now - cap.t_start >= POLL_AFTER and now - last_poll >= POLL_EVERY:
                    last_poll = now
                    rec = []
                    try:
                        with t.handshake(rec):
                            v = t.read16(regs.ACTION)
                    finally:
                        cap.ep84 += rec
                        cap.handshakes.append((round((now - cap.t_start) * 1000, 1), 'poll', rec))
                    cap.progress.append(v)
            elif now > deadline:
                cap.timed_out = True
                return None

    @staticmethod
    def _enough(cap, need_bytes, repeat_frames):
        if repeat_frames:
            return len(cap.frames) >= repeat_frames
        return cap.head is not None and need_bytes is not None and len(cap.data) >= need_bytes

    # ------------------------------------------------------------------ stop
    def _stop_normal(self, cap):
        """DSWave StopAcquire + Drain + ForceAcquire. Returns None, or the reason it had to fall back to the
        error stop."""
        t = self.t
        rec = []
        t_rel = round((time.time() - cap.t_start) * 1000, 1) if cap.t_start else None
        try:
            with t.handshake(rec):
                t.write(regs.CTRL, 0x0000)
                t.write(regs.ACTION, 0x0008)
        except DscopeError as e:
            cap.ep84 += rec
            cap.handshakes.append((t_rel, 'stop', rec))
            self._stop_error(cap, 'stop handshake failed: %s' % e.message)
            return cap.error
        cap.ep84 += rec
        cap.handshakes.append((t_rel, 'stop', rec))
        self._stopping = True
        t.running = False
        if not self._cancel_and_wait():
            why = 'EP 0x86 reads did not come back within %.0f s of the cancel' % DRAIN_TIMEOUT
            t.fail(why)
            cap.stop, cap.error = 'error', why
            return why
        try:
            t.write(regs.CTRL, 0x0000)
            time.sleep(0.001)
            t.write(regs.ACTION, 0x0004)
        except DscopeError as e:
            why = 'final stop write failed: %s' % e.message
            t.fail(why)
            cap.stop, cap.error = 'error', why
            return why
        cap.stop = 'normal'
        return None

    def _stop_error(self, cap, why):
        """DSWave error stop: send nothing; take the reads back; the session stays failed."""
        t = self.t
        t.fail(why)
        self._stopping = True
        t.running = False
        self._cancel_and_wait()
        cap.stop, cap.error = 'error', why
