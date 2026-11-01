"""M454 device bring-up: versions, FPGA configuration, security handshake, calibration read.

The sequence mirrors DSWave 1.2.1's UsbDevice::Open as seen on the wire (recorded cold and warm opens).
"""
import time

from . import regs
from .errors import DscopeError
from .transport import Transport

FW_MIN, PANEL_MIN, BIN_MIN = 0x3110, 0x3100, 0x3100
ZERO_INFO_ADDR, ZERO_INFO_BLOCKS, ZERO_INFO_BLOCK = 0x0000, 8, 64
SECU_KEY_ADDR, SECU_STEPS = 0x1000, 8
SECU_START, SECU_CHECK = 0x0513, 0x0219
ST_READY, ST_PASS = 0x02, 0x04


class Device:
    def __init__(self, log=None):
        self.t = Transport()
        self.t.log = log
        self.info = {}
        self.zero_info = None
        self.opened = False

    # ------------------------------------------------------------------ open
    def open(self, reload_fpga=False):
        """UsbDevice_Open order, with UsbDevice_Reopen's ForceAcquire(0) moved to the earliest safe point: right
        after HW_STATUS shows a configured FPGA, the device may still be running a capture that an earlier session
        left behind (error stop, killed process), so it is stopped before anything else is sent. An unconfigured
        FPGA cannot be running, and its configuration must stay the first write of the session.

        reload_fpga=True (`dscope recover`): reset and reconfigure the FPGA even
        when it reports DONE, as the first write of the session: PROG_B low clears it exactly like a power-up, then
        the verified bitstream is loaded. It cannot help when the MCU no longer answers register reads."""
        t0 = time.time()
        t = self.t.open()
        info = self.info
        info['usb_speed'] = {3: 'high', 4: 'super', 5: 'super+'}.get(t.speed, str(t.speed))
        try:
            fw = t.read16(regs.FW_VERSION)
        except DscopeError as e:
            if e.code != 'USB_ERROR':
                raise
            t.close()
            raise DscopeError('USB_ERROR', 'the scope does not answer register reads (%s)' % e.message,
                              'run "python -m dscope recover" once; if that fails the same way, '
                              'unplug and replug the scope')
        panel = t.read16(regs.PANEL_VERSION)
        info['fw_version'], info['panel_version'] = '0x%04X' % fw, '0x%04X' % panel
        if fw < FW_MIN or panel < PANEL_MIN:
            t.close()
            raise DscopeError('FW_TOO_OLD', 'device firmware 0x%04X / panel 0x%04X older than DSWave 1.2.1 expects' % (fw, panel),
                              'let the official DSWave update the device once; this driver never flashes firmware')
        status = t.read16(regs.HW_STATUS)
        info['fpga_configured_now'] = False
        if reload_fpga:
            info['fpga_was_done'] = bool(status & 0x80)
            self.fpga_config()
            info['fpga_configured_now'] = True
        elif status & 0x80:
            self.reset_acquisition()
        else:
            self.fpga_config()
            info['fpga_configured_now'] = True
        time.sleep(0.100)
        binv = t.read16(regs.BIN_VERSION)
        info['fpga_bin_version'] = '0x%04X' % binv
        if binv < BIN_MIN:
            raise DscopeError('PROTOCOL_ERROR', 'unexpected FPGA bin version 0x%04X' % binv)
        info['fpga_done'] = True
        info['security_pass'] = self.security_handshake()
        if not info['security_pass']:
            raise DscopeError('SECURITY_FAILED', 'security handshake did not pass', 'replug the scope and retry')
        info['probe_ratio_raw'] = [t.read16(regs.RATIO_INFO, ext=i) for i in range(4)]
        self.zero_info = b''.join(t.nvm_read(ZERO_INFO_ADDR + ZERO_INFO_BLOCK * i, ZERO_INFO_BLOCK)
                                  for i in range(ZERO_INFO_BLOCKS))
        self.idle()
        self.opened = True
        info['open_ms'] = round((time.time() - t0) * 1000)
        return self

    def fpga_config(self):
        """Configure the FPGA (HW_STATUS not-done, or `recover`). Mirrors UsbBsp3x::FpgaConfig."""
        from .firmware import bitstream
        raw = bitstream()
        t = self.t
        if t._wrote_anything:
            raise DscopeError('UNSAFE_OPERATION', 'FPGA configuration must be the first write of a session')
        with t.guarded('fpga'):
            t.write(regs.PROG_B, 0xFFFE)
            time.sleep(0.010)
            t.write(regs.PROG_B, 0x0001)
            for _ in range(11):
                if t.read16(regs.HW_STATUS) & 0x02:
                    break
                time.sleep(0.010)
            t.write(regs.BULK_LO, len(raw) & 0xFFFF)
            t.write(regs.BULK_HI, len(raw) >> 16)
            t.fpga_bulk_write(raw)
            for _ in range(11):
                if t.read16(regs.HW_STATUS) & 0x80:
                    return
                time.sleep(0.050)
        raise DscopeError('FPGA_CONFIG_FAILED', 'FPGA did not report DONE after configuration',
                          'replug the scope; if it persists, open DSWave once to let it configure the device')

    def security_handshake(self):
        t = self.t
        raw = t.nvm_read(SECU_KEY_ADDR, 2 * SECU_STEPS)
        key = [int.from_bytes(raw[2 * i:2 * i + 2], 'little') for i in range(SECU_STEPS)]
        del raw
        if all(k == 0xFFFF for k in key):
            raise DscopeError('SECURITY_KEY_BLANK', 'device security key is blank; refusing to write a new one',
                              'open the official DSWave once with this device')
        with t.guarded('security'):
            t.write(regs.SEC_CTRL, 0)
            time.sleep(0.010)
            t.write(regs.SEC_CTRL, 1)
            if t.read16(regs.CTRL) & ST_PASS:
                return True          # already unlocked in this power cycle
            t.write(regs.SEC_DATA, 0)
            t.write(regs.SEC_CTRL, SECU_START)
            for k in reversed(key):        # words are sent last-first (DSWave/DSView order)
                for _ in range(8):
                    st = t.read16(regs.CTRL)
                    if st & ST_PASS:
                        break
                    if t.read16(regs.CTRL) & ST_READY:
                        break
                t.read16(regs.SEC_DATA)
                t.write(regs.SEC_DATA, k)
                t.write(regs.SEC_CTRL, SECU_CHECK)
            key = None
            return bool(t.read16(regs.CTRL) & ST_PASS)

    def reset_acquisition(self):
        """End any acquisition a previous session left behind (DSWave UsbDevice_Reopen -> ForceAcquire(0)):
        W CTRL=0, 1 ms, W ACTION=4. DSWave sends the same pair after every capture, so it is harmless when idle.
        Best effort: a device that no longer answers needs a power cycle."""
        self.t.write(regs.CTRL, 0x0000)
        time.sleep(0.001)
        self.t.write(regs.ACTION, 0x0004)
        self.t.hold(0.1)

    def idle(self):
        """Stop state as left by DSWave after open: CTRL=0, ACTION=8 (flow pause)."""
        self.t.write(regs.CTRL, 0x0000)
        self.t.write(regs.ACTION, 0x0008)

    # ------------------------------------------------------------------ close
    def close(self):
        """Send nothing (DSWave UsbDevice_Close): a normal stop already ended with W CTRL=0 / W ACTION=4 and open
        ends with W CTRL=0 / W ACTION=8; after an error the device must not be touched."""
        self.t.close()
        self.opened = False

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()
