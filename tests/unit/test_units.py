import pytest
from openm454.units import si, channel_list, per_channel, window
from openm454.errors import DscopeError


def test_si():
    assert si('500mV', 'V') == 0.5 and si('10k', 'Hz') == 1e4 and si('5ms', 's') == 0.005
    assert si('1MHz', 'Hz') == 1e6 and si('1mHz', 'Hz') == 1e-3 and si('100MSa/s', 'Hz') == 1e8
    with pytest.raises(DscopeError):
        si('5Hz', 'V')


def test_channels():
    assert channel_list('A0-A3,D0-D7') == (['A0', 'A1', 'A2', 'A3'], ['D%d' % i for i in range(8)])
    assert channel_list('A1,D5') == (['A1'], ['D5'])
    with pytest.raises(DscopeError):
        channel_list('A4')


def test_per_channel_and_window():
    assert per_channel('A0=500mV,A2=2V', 'V', 'vdiv', 1.0) == {'A0': 0.5, 'A1': 1.0, 'A2': 2.0, 'A3': 1.0}
    assert window('-1ms:2ms') == (-0.001, 0.002) and window(None) is None
