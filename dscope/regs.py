"""M454 register map (8-bit addresses; values 16-bit). Names follow DSWave's HAL log."""

HW = {0x00: 'HW_BASE_MODE', 0x01: 'HW_BASE_CTRL', 0x02: 'HW_BASE_ACTION', 0x03: 'HW_BASE_SYNC', 0x04: 'HW_BASE_EXT',
      0x07: 'AWG_ADDR', 0x08: 'AWG_DATA', 0x09: 'MSO_BASE_TRIG_ADDR', 0x0A: 'MSO_BASE_TRIG_DATA',
      0x0B: 'MSO_BASE_TRIG_SHFT', 0x0E: 'HW_SEC_CTRL', 0x0F: 'HW_SEC_DATA'}
MSO = {0x10: 'HORI_BASE', 0x11: 'HORI_DEPTH0', 0x12: 'HORI_DEPTH1', 0x13: 'HORI_RWORDS0', 0x14: 'HORI_RWORDS1',
       0x15: 'HORI_ADIVL', 0x16: 'HORI_ADIVH', 0x17: 'HORI_DDIVL', 0x18: 'HORI_DDIVH', 0x19: 'HORI_AMTXS',
       0x1A: 'HORI_DMTXS', 0x1B: 'HORI_SLOWCNT',
       0x20: 'VERT_DFOLD', 0x21: 'VERT_DTHR', 0x22: 'VERT_DUP', 0x23: 'VERT_DEN0', 0x24: 'VERT_DEN1',
       0x25: 'VERT_DEN2', 0x26: 'VERT_DEN3', 0x28: 'VERT_AFOLD', 0x29: 'VERT_ACROSS', 0x2A: 'VERT_ABRH_INV',
       0x2B: 'VERT_AWR',
       0x30: 'TRIG_BASE', 0x31: 'TRIG_PRES0', 0x32: 'TRIG_PRES1', 0x33: 'TRIG_PREW0', 0x34: 'TRIG_PREW1',
       0x35: 'TRIG_LEVEL', 0x36: 'TRIG_INDEX', 0x37: 'TRIG_MARGIN', 0x38: 'TRIG_HOLD0', 0x39: 'TRIG_HOLD1',
       0x3A: 'TRIG_PULSE'}
for _n in range(4):
    _b = 0x40 + 0x10 * _n
    MSO.update({_b: 'ANA%d_BASE' % _n, _b + 1: 'ANA%d_VERT' % _n, _b + 2: 'ANA%d_VOFF' % _n,
                _b + 3: 'ANA%d_VPWM' % _n, _b + 4: 'ANA%d_CROSS' % _n})
MCU = {0xF0: 'FW_VERSION', 0xF1: 'BIN_VERSION', 0xF2: 'PANEL_VERSION', 0xF4: 'HARDWARE_INDEX', 0xF9: 'FLUSH_DATA',
       0xFA: 'RATIO_INFO', 0xFB: 'EEPROM_ADDR', 0xFC: 'BULK_SIZE_LOW', 0xFD: 'BULK_SIZE_HIGH', 0xFE: 'PROG_B',
       0xFF: 'HW_STATUS'}

CTRL, ACTION, SYNC, MODE = 0x01, 0x02, 0x03, 0x00
SEC_CTRL, SEC_DATA = 0x0E, 0x0F
HW_STATUS, PROG_B, BULK_LO, BULK_HI, EEPROM_ADDR = 0xFF, 0xFE, 0xFC, 0xFD, 0xFB
FW_VERSION, BIN_VERSION, PANEL_VERSION, HARDWARE_INDEX, FLUSH_DATA, RATIO_INFO = 0xF0, 0xF1, 0xF2, 0xF4, 0xF9, 0xFA


def name(addr):
    return HW.get(addr) or MSO.get(addr) or MCU.get(addr) or 'REG_%02X' % addr
