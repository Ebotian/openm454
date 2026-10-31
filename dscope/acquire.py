"""Acquisition engine: run one register-programmed capture and collect the raw stream.

Order (mirrors DSWave 1.2.1, re/traces/S01_warm):
  1. setup program (W 0x01=0 ... W 0x00 MODE, W 0xF9 FLUSH=0)       -- from mso.setup_program() or a recorded block
  2. queue async bulk reads on EP 0x86: one 4096-byte head + N data transfers of pkt_bytes
  3. start: W SYNC(0x03)=sync, W ACTION(0x02)=0, W CTRL(0x01)=1
  4. collect: head first (u32[0]=0x55555555), then data until `need_bytes` arrived (resubmitting transfers)
  5. stop: EP 0x84 handshake (read 1 byte twice), W CTRL=0, W ACTION=8, read 1 byte; cancel reads; W CTRL=0, W ACTION=4
Registers are never written while the stream runs except inside the EP 0x84 handshake.
"""
import struct
import time

import usb1

from . import regs
from .errors import DscopeError

HEAD_BYTES = 4096
EP_DATA = 0x86


class RawCapture:
    def __init__(self):
        self.head = None
        self.data = bytearray()
        self.frames = []          # repeat mode: list of (head, data)
        self.t_start = None
        self.t_head = None
        self.t_done = None
        self.timed_out = False
        self.ep84 = []

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

    def _submit(self, n):
        tr = self.t.h.getTransfer()
        tr.setBulk(EP_DATA, n, callback=self._cb, timeout=0)
        tr.submit()
        return tr

    def _cb(self, tr):
        st = tr.getStatus()
        if st == usb1.TRANSFER_COMPLETED:
            self._done.append(tr)
        elif st != usb1.TRANSFER_CANCELLED:
            self._errors.append(st)
            self._dead.add(id(tr))
        else:
            self._dead.add(id(tr))
            n = tr.getActualLength()
            if n:
                self._done.append(tr)

    def run(self, setup_writes, pkt_bytes, need_bytes, n_transfers=16, trigger_timeout=5.0,
            collect_timeout=10.0, start_extra=None, repeat_frames=0, on_chunk=None, stop_flag=None):
        """Execute one capture. need_bytes = data bytes (after the head) to collect.
        repeat_frames > 0: repeat mode; after the head every data transfer is one frame (data + 4 KiB tail);
        collect that many frames. on_chunk(bytes) is called for the head and for every data chunk as it
        arrives (live streaming). stop_flag(): optional callable; the capture stops when it returns True."""
        t = self.t
        cap = RawCapture()
        self._done, self._errors, self._dead = [], [], set()
        for addr, val in setup_writes:
            t.write(addr, val)
        trs = []
        try:
            trs.append(self._submit(HEAD_BYTES))
            for _ in range(n_transfers):
                trs.append(self._submit(pkt_bytes))
            t.write(regs.SYNC, self.sync)
            t.write(regs.ACTION, 0x0000)
            t.write(regs.CTRL, 0x0001)
            cap.t_start = time.time()
            deadline_head = cap.t_start + trigger_timeout
            deadline = None
            ctx = t.ctx
            while True:
                ctx.handleEventsTimeout(0.02)
                while self._done:
                    tr = self._done.pop(0)
                    n = tr.getActualLength()
                    buf = bytes(tr.getBuffer()[:n])
                    if cap.head is None and n == HEAD_BYTES and tr is trs[0]:
                        cap.head = buf
                        cap.t_head = time.time()
                        deadline = cap.t_head + collect_timeout
                        if buf[:4] != b'\x55\x55\x55\x55':
                            raise DscopeError('PROTOCOL_ERROR', 'bad head packet check id %s' % buf[:4].hex())
                    elif repeat_frames:
                        cap.frames.append(buf)
                    else:
                        cap.data += buf
                    if on_chunk is not None:
                        on_chunk(buf)
                    if tr.getStatus() == usb1.TRANSFER_COMPLETED and id(tr) not in self._dead \
                            and tr is not trs[0] and not self._enough(cap, need_bytes, repeat_frames):
                        tr.setBulk(EP_DATA, pkt_bytes, callback=self._cb, timeout=0)
                        tr.submit()
                if self._errors:
                    raise DscopeError('USB_ERROR', 'bulk transfer error status %s' % self._errors)
                if self._enough(cap, need_bytes, repeat_frames) or (stop_flag is not None and stop_flag()):
                    break
                now = time.time()
                if (cap.head is None and now > deadline_head) or (deadline and now > deadline):
                    cap.timed_out = True
                    break
        finally:
            self._stop(trs, cap)
        cap.t_done = time.time()
        return cap

    @staticmethod
    def _enough(cap, need_bytes, repeat_frames):
        if repeat_frames:
            return len(cap.frames) >= repeat_frames
        return cap.head is not None and need_bytes is not None and len(cap.data) >= need_bytes

    def _stop(self, trs, cap):
        t = self.t
        try:
            for _ in range(2):
                b = t.ep84_byte(500)
                cap.ep84.append(b)
            t.write(regs.CTRL, 0x0000)
            t.write(regs.ACTION, 0x0008)
            cap.ep84.append(t.ep84_byte(500))
        except DscopeError:
            pass
        for tr in trs:
            try:
                if tr.isSubmitted():
                    tr.cancel()
            except usb1.USBError:
                pass
        end = time.time() + 2.0
        while any(tr.isSubmitted() for tr in trs) and time.time() < end:
            t.ctx.handleEventsTimeout(0.02)
        self._done.clear()
        t.write(regs.CTRL, 0x0000)
        t.write(regs.ACTION, 0x0004)
