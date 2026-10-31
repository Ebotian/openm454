"""USB transport for the DSCope M454 with a hard safety whitelist.

Wire protocol (recovered from DSWave 1.2.1, hal::UsbBsp3x):
  register write : ctrl OUT bmRequestType 0x40, bRequest 0xB0, data [addr, lo, hi]
  register read  : ctrl OUT 0x40/0xB1 data [addr, len, ext]; wait 10 ms; ctrl IN 0xC0/0xB2 len bytes
  EP 0x02 bulk OUT : FPGA bitstream (and NVM writes, which this driver never does)
  EP 0x84 bulk IN  : 1-byte handshake used while an acquisition is running
  EP 0x86 bulk IN  : capture data (4096-byte head packet, then data packets)

Safety (do not relax without the owner's explicit approval):
  * only bRequest 0xB0/0xB1/0xB2 are ever sent (0xB3 = firmware ISP is impossible here)
  * writes to MCU registers 0xF0-0xFF are refused except FLUSH_DATA, HARDWARE_INDEX(0), and the
    FPGA-config / EEPROM-read registers inside their guarded contexts
  * EP 0x02 OUT is only reachable from fpga_config() and only while the FPGA reports not-configured
"""
import contextlib
import threading
import time

import usb1

from .errors import DscopeError
from . import regs

VID, PID = 0x2A0E, 0x0080
REQ_WRITE, REQ_READ_SETUP, REQ_READ = 0xB0, 0xB1, 0xB2
_ALLOWED_REQUESTS = (REQ_WRITE, REQ_READ_SETUP, REQ_READ)
EP_OUT, EP_CTRL_IN, EP_DATA_IN = 0x02, 0x84, 0x86
READ_DELAY = 0.010
TIMEOUT_MS = 1000


class Transport:
    def __init__(self):
        self.ctx = None
        self.dev = None
        self.h = None
        self.speed = None
        self._ctx_guard = set()          # active guarded contexts: 'fpga', 'nvm', 'security'
        self._fb_written = False         # D2: FPGA config must precede any EEPROM address write
        self._wrote_anything = False
        self.lock = threading.RLock()
        self.log = None                  # optional callable(str) for a wire log

    # ---------------------------------------------------------------- open / close
    def open(self):
        self.ctx = usb1.USBContext()
        self.ctx.open()
        try:
            dev = self.ctx.getByVendorIDAndProductID(VID, PID, skip_on_error=True)
        except usb1.USBError as e:
            raise DscopeError('USB_ERROR', 'USB enumeration failed: %s' % e)
        if dev is None:
            raise DscopeError('NOT_FOUND', 'DSCope M454 (2A0E:0080) not found',
                              'check the USB cable; the device must show "USB-based DSL Instrument v3"')
        self.dev = dev
        self.speed = dev.getDeviceSpeed()
        try:
            self.h = dev.open()
        except usb1.USBError as e:
            raise DscopeError('BUSY', 'cannot open the device (%s)' % e,
                              'close DSWave or any other program using the scope, then retry')
        try:
            manu, prod = self.h.getManufacturer(), self.h.getProduct()
        except usb1.USBError:
            manu, prod = None, None
        if manu != 'DreamSourceLab' or prod != 'USB-based DSL Instrument v3':
            self.close()
            raise DscopeError('PROTOCOL_ERROR', 'unexpected USB strings: %r / %r' % (manu, prod))
        try:
            self.h.claimInterface(0)
        except usb1.USBError as e:
            self.h.close()
            self.h = None
            raise DscopeError('BUSY', 'cannot claim the USB interface (%s)' % e,
                              'close DSWave or a running "dscope watch", then retry')
        return self

    def close(self):
        if self.h is not None:
            with contextlib.suppress(Exception):
                self.h.releaseInterface(0)
            with contextlib.suppress(Exception):
                self.h.close()
            self.h = None
        if self.ctx is not None:
            with contextlib.suppress(Exception):
                self.ctx.close()
            self.ctx = None

    # ---------------------------------------------------------------- raw control
    def _ctrl_out(self, req, data):
        assert req in _ALLOWED_REQUESTS, 'forbidden bRequest 0x%02X' % req
        try:
            n = self.h.controlWrite(0x40, req, 0, 0, bytes(data), timeout=TIMEOUT_MS)
        except usb1.USBError as e:
            raise DscopeError('USB_ERROR', 'control write 0x%02X failed: %s' % (req, e))
        if n != len(data):
            raise DscopeError('USB_ERROR', 'short control write (%d of %d)' % (n, len(data)))

    def _ctrl_in(self, req, n):
        assert req in _ALLOWED_REQUESTS, 'forbidden bRequest 0x%02X' % req
        try:
            return bytes(self.h.controlRead(0xC0, req, 0, 0, n, timeout=TIMEOUT_MS))
        except usb1.USBError as e:
            raise DscopeError('USB_ERROR', 'control read failed: %s' % e)

    # ---------------------------------------------------------------- registers
    def _check_write(self, addr, value):
        if not 0 <= addr <= 0xFF or not 0 <= value <= 0xFFFF:
            raise DscopeError('UNSAFE_OPERATION', 'register write out of range %r=%r' % (addr, value))
        if addr in (regs.SEC_CTRL, regs.SEC_DATA) and 'security' not in self._ctx_guard:
            raise DscopeError('UNSAFE_OPERATION', 'security registers only inside the handshake')
        if addr < 0x80:
            return
        if addr == regs.FLUSH_DATA:
            return
        if addr == regs.HARDWARE_INDEX and value == 0:
            return
        if addr == regs.EEPROM_ADDR and 'nvm' in self._ctx_guard:
            return
        if addr in (regs.PROG_B, regs.BULK_LO, regs.BULK_HI) and 'fpga' in self._ctx_guard:
            return
        raise DscopeError('UNSAFE_OPERATION', 'refused write to MCU register 0x%02X (%s)' % (addr, regs.name(addr)))

    def write(self, addr, value):
        with self.lock:
            self._check_write(addr, value)
            if addr == regs.EEPROM_ADDR:
                self._fb_written = True
            self._wrote_anything = True
            self._ctrl_out(REQ_WRITE, [addr, value & 0xFF, (value >> 8) & 0xFF])
            if self.log:
                self.log('W %02X(%s)=%s' % (addr, regs.name(addr), '****' if addr == regs.SEC_DATA else '%04X' % value))

    def read(self, addr, n=2, ext=0):
        if not 1 <= n < 128:
            raise DscopeError('INVALID_ARG', 'register read length must be 1..127')
        with self.lock:
            self._ctrl_out(REQ_READ_SETUP, [addr, n, ext])
            time.sleep(READ_DELAY)
            d = self._ctrl_in(REQ_READ, n)
            if self.log:
                secret = addr in (regs.SEC_DATA, regs.EEPROM_ADDR)
                self.log('R %02X(%s) ext=%d -> %s' % (addr, regs.name(addr), ext, '<%d bytes>' % n if secret else d.hex()))
            return d

    def read16(self, addr, ext=0):
        return int.from_bytes(self.read(addr, 2, ext), 'little')

    # ---------------------------------------------------------------- guarded contexts
    @contextlib.contextmanager
    def guarded(self, name):
        self._ctx_guard.add(name)
        try:
            yield
        finally:
            self._ctx_guard.discard(name)

    def nvm_read(self, addr, n):
        """Read n bytes (n < 128) of the device EEPROM. Never writes the EEPROM."""
        with self.lock, self.guarded('nvm'):
            self.write(regs.EEPROM_ADDR, addr)
            return self.read(regs.EEPROM_ADDR, n)

    def fpga_bulk_write(self, data):
        """EP 0x02 OUT. Only callable inside the 'fpga' guarded context (see device.fpga_config)."""
        if 'fpga' not in self._ctx_guard:
            raise DscopeError('UNSAFE_OPERATION', 'EP2 OUT outside FPGA configuration')
        if self._fb_written:
            raise DscopeError('UNSAFE_OPERATION', 'EP2 OUT after an EEPROM address write (reopen first)')
        try:
            n = self.h.bulkWrite(EP_OUT, bytes(data), timeout=5000)
        except usb1.USBError as e:
            raise DscopeError('FPGA_CONFIG_FAILED', 'bitstream transfer failed: %s' % e)
        if n != len(data):
            raise DscopeError('FPGA_CONFIG_FAILED', 'bitstream short write %d/%d' % (n, len(data)))

    # ---------------------------------------------------------------- bulk IN
    def ep84_byte(self, timeout_ms=TIMEOUT_MS):
        try:
            d = self.h.bulkRead(EP_CTRL_IN, 1, timeout=timeout_ms)
        except usb1.USBErrorTimeout:
            return None
        except usb1.USBError as e:
            raise DscopeError('USB_ERROR', 'EP 0x84 read failed: %s' % e)
        return d[0] if d else None
