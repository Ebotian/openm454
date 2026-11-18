"""The AWG restore contract, offline: the state file has to be enough to rebuild every mode."""
from tests.support.bench import restore_awg


class FakeScope:
    """Records what the restore programmed, in the call order the real Scope exposes."""

    def __init__(self):
        self.calls = []

    def awg_off(self):
        self.calls.append(('off',))

    def awg_set(self, wave, freq, high, low, duty=50.0, phase=0.0):
        self.calls.append(('set', wave, freq, high, low, duty, phase))

    def awg_dc(self, level):
        self.calls.append(('dc', level))

    def awg_burst(self, wave, freq, high, low, cycles, period, duty=50.0, phase=0.0):
        self.calls.append(('burst', wave, freq, high, low, cycles, period, duty, phase))

    def awg_sweep(self, start, end, time, sweep_type='linear', wave='sine', high=3.0, low=0.0, duty=50.0):
        self.calls.append(('sweep', start, end, time, sweep_type, wave, high, low, duty))

    def awg_modulate(self, kind, wave, freq, high, low, mod_wave='sine', mod_freq=100.0, depth=50.0,
                     deviation=None, hop=None, duty=50.0):
        self.calls.append(('mod', kind, freq, depth, deviation, hop, duty))


def _base(**kw):
    d = {'enabled': True, 'wave': 'sine', 'freq_hz': 100e3, 'high_v': 3.0, 'low_v': 0.0,
         'high_req_v': 3.0, 'low_req_v': 0.0, 'duty_pct': 20.0}
    d.update(kw)
    return d


def test_am_restores_its_depth_and_duty():
    """The state file is flat for the modulation kind: depth_pct, not a nested am.depth."""
    s = FakeScope()
    restore_awg(s, _base(mode='modulation', kind='am', mod_wave='sine', mod_freq_hz=1e3, depth_pct=30.0))
    assert s.calls == [('mod', 'am', 100e3, 30.0, None, None, 20.0)]


def test_fm_and_fsk_restore_their_deviation_and_hop():
    s = FakeScope()
    restore_awg(s, _base(mode='modulation', kind='fm', mod_wave='sine', mod_freq_hz=1e3, deviation_hz=2000.0))
    assert s.calls == [('mod', 'fm', 100e3, 50.0, 2000.0, None, 20.0)]
    s = FakeScope()
    restore_awg(s, _base(mode='modulation', kind='fsk', mod_wave='sine', mod_freq_hz=1e3, hop_hz=20000.0))
    assert s.calls == [('mod', 'fsk', 100e3, 50.0, None, 20000.0, 20.0)]


def test_burst_and_sweep_carry_duty_and_phase():
    s = FakeScope()
    restore_awg(s, _base(mode='burst', wave='square', freq_hz=1e3, cycles=2, period_s=0.5, phase_deg=90.0))
    assert s.calls == [('burst', 'square', 1e3, 3.0, 0.0, 2, 0.5, 20.0, 90.0)]
    s = FakeScope()
    restore_awg(s, _base(mode='sweep', wave='square', start_hz=1e3, end_hz=10e3, time_s=0.02, sweep_type='log'))
    assert s.calls == [('sweep', 1e3, 10e3, 0.02, 'log', 'square', 3.0, 0.0, 20.0)]


def test_levels_are_restored_from_what_was_asked_for():
    """Restoring the quantized reading drifts one 6.4 mV step per cycle: the requested values are recorded too."""
    s = FakeScope()
    restore_awg(s, _base(high_v=2.9872, low_v=0.4934, high_req_v=3.0, low_req_v=0.5))
    assert s.calls == [('set', 'sine', 100e3, 3.0, 0.5, 20.0, 0.0)]


def test_dc_uses_the_requested_level():
    s = FakeScope()
    restore_awg(s, _base(wave='dc', level_v=1.4934, level_req_v=1.5))
    assert s.calls == [('dc', 1.5)]


def test_an_off_or_unknown_state_parks_the_output():
    for before in (None, {}, {'enabled': False, 'wave': 'square', 'freq_hz': 1e3}):
        s = FakeScope()
        restore_awg(s, before)
        assert s.calls == [('off',)], before