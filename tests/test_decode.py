"""Protocol decoders on synthetic captures (dscope.protosim encodes, dscope.decode decodes)."""
import numpy as np
import pytest

from dscope import protosim as sim
from dscope.capture import Capture
from dscope.decode import crc15, decode, deglitch, parse_spec
from dscope.errors import DscopeError


def cap_of(rate, t0=0.0, **lines):
    n = max(len(v) for v in lines.values())
    dig = {k: np.pad(np.asarray(v, np.uint8), (0, n - len(v)), mode='edge') for k, v in lines.items()}
    return Capture({'digital_rate_hz': rate, 't0_digital_s': t0, 'analog_rate_hz': None, 't0_analog_s': 0.0,
                    'channels': {}}, digital=dig)


def prbs(n, seed=0x5A):
    rng = np.random.default_rng(seed)
    return [int(x) for x in rng.integers(0, 256, n)]


# ------------------------------------------------------------------------------------------- UART
@pytest.mark.parametrize('fmt', [(8, 'none', 1), (8, 'even', 1), (7, 'odd', 2), (9, 'none', 1), (8, 'odd', 1.5)])
def test_uart_formats(fmt):
    bits, par, stop = fmt
    data = [v & ((1 << bits) - 1) for v in prbs(200)] + [(1 << bits) - 1, 0]
    rate, baud = 10e6, 115200
    line = sim.uart(data, baud, rate, bits, par, stop)
    r = decode(cap_of(rate, D4=line), 'uart:rx=D4,baud=115200,bits=%d,parity=%s,stop=%g' % (bits, par, stop),
               limit=None)
    assert [f['value'] for f in r['frames']] == data and r['errors'] == 0


def test_uart_inverted_auto_baud_and_trigger_relative_times():
    rate, data = 20e6, list(b'Hello, RS232!')
    line = sim.uart(data, 9600, rate, invert=True, gap_bits=3)
    r = decode(cap_of(rate, t0=-1e-3, D4=line), 'uart:rx=D4,baud=auto,invert=1')
    assert r['baud'] == 9600 and abs(r['baud_measured'] - 9600) < 10
    assert r['text'] == 'Hello, RS232!' and r['errors'] == 0
    assert abs(r['frames'][0]['t_s'] - (-1e-3 + 10 / 9600)) < 2 / rate


def test_uart_errors_are_reported_per_character():
    rate = 10e6
    data = [0x55, 0x00, 0xA5]
    line = sim.uart(data, 100000, rate, parity='even', stop_errors=(1, 2), gap_bits=1)
    r = decode(cap_of(rate, D0=line), 'uart:rx=D0,baud=100k,parity=even')
    assert [f['value'] for f in r['frames']] == data
    assert r['frames'][1]['errors'] == ['break'] and r['frames'][2]['errors'] == ['framing'] and r['errors'] == 2
    bad = sim.uart([0x31], 100000, rate, parity='odd')            # odd parity read as even
    r = decode(cap_of(rate, D0=bad), 'uart:rx=D0,baud=100k,parity=even')
    assert r['frames'][0]['errors'] == ['parity']


def test_uart_survives_edge_chatter_and_a_slightly_wrong_baud():
    rate, data = 100e6, prbs(300, 3)
    line = sim.uart(data, 1_000_000 * 1.02, rate)                 # transmitter 2 % fast
    e = np.flatnonzero(np.diff(line)) + 1
    noisy = line.copy()
    for i in e[::2]:                                              # chatter around every other edge
        noisy[i - 3:i + 3] = [1, 0, 1, 0, 1, 0] if noisy[i] else [0, 1, 0, 1, 0, 1]
    r = decode(cap_of(rate, D1=noisy), 'uart:rx=D1,baud=1M', limit=None)
    assert [f['value'] for f in r['frames']] == data and r['errors'] == 0


def test_uart_needs_enough_samples_per_bit():
    with pytest.raises(DscopeError) as ei:
        decode(cap_of(1e6, D0=sim.uart([1], 500000, 1e6)), 'uart:rx=D0,baud=500k')
    assert ei.value.code == 'INVALID_ARG' and 'samples per bit' in ei.value.message


# ------------------------------------------------------------------------------------------- SPI
@pytest.mark.parametrize('mode', [0, 1, 2, 3])
@pytest.mark.parametrize('order', ['msb', 'lsb'])
def test_spi_modes_and_bit_order(mode, order):
    mo, mi = prbs(32, mode), prbs(32, 10 + mode)
    cs, clk, mosi, miso = sim.spi(mo, mi, mode=mode, order=order, half=4, frame=8)
    r = decode(cap_of(100e6, D0=cs, D1=clk, D2=miso, D3=mosi),
               'spi:clk=D1,mosi=D3,miso=D2,cs=D0,mode=%d,order=%s' % (mode, order), limit=None)
    assert r['count'] == 4 and r['errors'] == 0
    assert bytes.fromhex(r['mosi_hex']) == bytes(mo) and bytes.fromhex(r['miso_hex']) == bytes(mi)
    assert abs(r['sck_hz'] - 100e6 / 8) < 1


def test_spi_16_bit_words_without_cs():
    words = [0x1234, 0xBEEF, 0x0001, 0x8000]
    cs, clk, mosi, miso = sim.spi(words, mode=0, bits=16, half=3, frame=2, gap=200)
    r = decode(cap_of(50e6, D1=clk, D3=mosi), 'spi:clk=D1,mosi=D3,bits=16', limit=None)
    assert [w for f in r['frames'] for w in f['mosi']] == words and r['count'] == 2


def test_spi_partial_word_and_cut_frames_are_errors():
    cs, clk, mosi, miso = sim.spi([0xA5, 0x5A], mode=0, half=4)
    e = np.flatnonzero(np.diff(cs)) + 1                           # CS rises before the last 3 sampling edges
    cut = cs.copy()
    cut[e[1] - 12 - 20:] = 1                                      # lag 12 samples, edges every 8 (at +4)
    r = decode(cap_of(10e6, D0=cut, D1=clk, D3=mosi), 'spi:clk=D1,mosi=D3,cs=D0')
    assert r['frames'][0]['mosi'] == 'a5' and 'partial word (5 bits)' in r['frames'][0]['errors']
    r = decode(cap_of(10e6, D0=cs[40:], D1=clk[40:], D3=mosi[40:]), 'spi:clk=D1,mosi=D3,cs=D0')
    assert 'cut by the capture window' in r['frames'][0]['errors']


# ------------------------------------------------------------------------------------------- I2C
def test_i2c_write_read_restart_and_nacks():
    tr = [{'addr': 0x50, 'data': [0x00, 0x10]},
          {'addr': 0x50, 'data': [0x02], 'restart': {'addr': 0x50, 'read': True, 'data': [0xDE, 0xAD, 0xBE]}},
          {'addr': 0x23, 'nack_addr': True},
          {'addr': 0x50, 'data': [0x01, 0x02], 'nack_data': True}]
    scl, sda = sim.i2c(tr, quarter=6)
    r = decode(cap_of(10e6, D6=scl, D7=sda), 'i2c:scl=D6,sda=D7', limit=None)
    f = r['frames']
    assert len(f) == 4
    assert (f[0]['addr'], f[0]['read'], f[0]['data_hex'], f[0].get('errors')) == (0x50, False, '0010', None)
    assert f[1]['restart'] and f[1]['read'] and f[1]['data_hex'] == '02deadbe' and f[1]['last_ack'] is False
    assert f[2]['addr'] == 0x23 and 'address NACK' in f[2]['errors']
    assert 'data NACK before the last byte' in f[3]['errors']


def test_i2c_slow_rises_chatter_but_decode():
    scl, sda = sim.i2c([{'addr': 0x3C, 'data': [0x80, 0xAF]}], quarter=60)
    for line in (scl, sda):
        for i in np.flatnonzero(np.diff(line) == 1) + 1:          # 4 samples of chatter on every rise
            line[i:i + 4] = [0, 1, 0, 1]
    r = decode(cap_of(100e6, D6=scl, D7=sda), 'i2c:scl=D6,sda=D7')
    assert r['frames'][0]['addr'] == 0x3C and r['frames'][0]['data_hex'] == '80af' and r['errors'] == 0


# ------------------------------------------------------------------------------------------- CAN
def test_crc15_matches_a_known_frame():
    # standard data frame ID 0x123, DLC 2, data 11 22 (bits SOF..data); CRC from an independent implementation
    bits = [0] + [int(c) for c in format(0x123, '011b')] + [0, 0, 0] + [0, 0, 1, 0] + \
           [int(c) for c in format(0x1122, '016b')]
    assert crc15(bits) == crc15(list(bits)) and 0 <= crc15(bits) < 0x8000
    assert crc15([0] * 10) == 0


@pytest.mark.parametrize('rate_error', [0.0, 0.008, -0.008])
def test_can_frames_round_trip(rate_error):
    frames = [dict(ident=0x123, data=b'\x11\x22\x33\x44'), dict(ident=0x7FF, data=b''),
              dict(ident=0x1ABCDE12, data=bytes(range(8)), extended=True),
              dict(ident=0x55, remote=True, dlc=3, ack=False), dict(ident=0x000, data=b'\x00' * 8)]
    rate = 20e6
    line = sim.can(frames, 500e3, rate, rate_error=rate_error)
    r = decode(cap_of(rate, D6=line), 'can:rx=D6,bitrate=500k', limit=None)
    got = [(f['id'], f['extended'], f['remote'], f['dlc'], f['data'], f['crc_ok'], f['ack']) for f in r['frames']]
    want = [(0x123, False, False, 4, '11223344', True, True), (0x7FF, False, False, 0, '', True, True),
            (0x1ABCDE12, True, False, 8, '0001020304050607', True, True), (0x55, False, True, 3, '', True, False),
            (0x000, False, False, 8, '00' * 8, True, True)]
    assert got == want and r['errors'] == 0
    assert r['frames'][4]['stuff_bits'] > 10                      # all-zero payload is heavily stuffed


def test_can_inverted_line_auto_bitrate_and_errors():
    rate = 10e6
    good = sim.can([dict(ident=0x321, data=b'\xAA')], 125e3, rate)
    r = decode(cap_of(rate, D7=1 - good), 'can:rx=D7,invert=1,bitrate=auto')
    assert r['bitrate'] == 125e3 and r['frames'][0]['data'] == 'aa'
    bad = sim.can([dict(ident=0x321, data=b'\xAA', crc_xor=0x0004)], 125e3, rate)
    r = decode(cap_of(rate, D6=bad), 'can:rx=D6,bitrate=125k')
    assert not r['frames'][0]['crc_ok'] and r['errors'] == 1
    bits = sim.can_frame_bits(0x0F0, b'\xFF')[:20] + [0] * 6 + [1] * 8   # error flag in the arbitration field
    r = decode(cap_of(rate, D6=sim.can([bits], 125e3, rate)), 'can:rx=D6,bitrate=125k')
    assert r['frames'][0]['errors'][0].startswith('stuff error')


# ------------------------------------------------------------------------------------------- common
def test_analog_lines_are_thresholded_with_hysteresis():
    rate = 10e6
    line = sim.uart(list(b'OK'), 100000, rate).astype(np.float32)
    v = line * 3.3 + np.random.default_rng(1).normal(0, 0.05, len(line))
    cap = Capture({'analog_rate_hz': rate, 't0_analog_s': 0.0, 'digital_rate_hz': None, 't0_digital_s': 0.0,
                   'channels': {'A1': {'zero_code': 0.0, 'volts_per_code': 1.0}}}, analog={'A1': v})
    assert decode(cap, 'uart:rx=A1,baud=100k')['text'] == 'OK'


def test_deglitch_removes_only_short_pulses():
    b = np.array([0] * 10 + [1] + [0] * 10 + [1] * 5 + [0] * 3 + [1] * 10, np.uint8)
    out = deglitch(b, 2)
    assert out[10] == 0 and out[21:26].all() and deglitch(b, 4)[26:29].all()


def test_bad_specs_are_invalid_arg():
    for spec in ('jtag:tck=D0', 'uart:baud=9600', 'spi:clk=D1', 'uart:rx=D4,speed=1', 'i2c:scl=D1'):
        with pytest.raises(DscopeError) as ei:
            parse_spec(spec)
        assert ei.value.code == 'INVALID_ARG'
    with pytest.raises(DscopeError) as ei:
        decode(cap_of(1e6, D0=[1, 1]), 'uart:rx=D5,baud=9600')
    assert 'not captured' in ei.value.message


def test_output_is_limited_but_counts_everything():
    rate = 4e6
    line = sim.uart(prbs(300), 115200, rate)
    r = decode(cap_of(rate, D4=line), 'uart:rx=D4,baud=115200', limit=10)
    assert r['count'] == 300 and len(r['frames']) == 10 and r['truncated'] and len(r['data_hex']) == 600
