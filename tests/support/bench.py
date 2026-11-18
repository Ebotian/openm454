"""The declared bench: which output is wired to which input, read from tests/hardware/bench.json.

A hardware test does not carry its wiring in constants or prose: it asks the declaration for a link by name and gets
the probe attenuation, the volts/div, the offset, the signal to put on the wire and the tolerance it must be seen
within. The file is data (checked offline by tests/unit/test_bench_declaration.py); rewiring the bench is an edit
to it, and a link that is not declared is never guessed.
"""
import json
import os

import pytest

DECLARATION = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'hardware', 'bench.json')
ANALOG = ('A0', 'A1', 'A2', 'A3')
DIGITAL = tuple('D%d' % i for i in range(8))


def load(path=None):
    with open(path or DECLARATION, encoding='utf-8') as f:
        return json.load(f)


class Link:
    """One declared connection: a source driving a sink, and what a capture of that sink has to look like."""

    def __init__(self, name, d):
        self.name, self.d = name, d

    def __getitem__(self, key):
        return self.d[key]

    @property
    def channel(self):
        return self.d['sink']

    def settings(self, duration, rate):
        """A forced, untriggered capture of the sink, configured the way the declaration says it is wired."""
        from openm454 import scope as scopemod
        st = scopemod.Settings()
        if self.channel in ANALOG:
            st.analog, st.digital = [self.channel], []
        else:
            st.analog, st.digital = [], [self.channel]
        st.rate, st.duration = rate, duration
        st.probe[self.channel] = self.d['probe']
        st.vdiv[self.channel] = self.d['vdiv_v']
        st.offset[self.channel] = self.d['offset_v']
        st.trig_source, st.trig_mode = None, 'force'
        return st

    def program(self, scope):
        """Put the declared signal on the wire (an `awg` source only: nothing else is programmable from a test)."""
        if self.d['source'] != 'awg':
            raise AssertionError('%s: source %r is not programmable from a test' % (self.name, self.d['source']))
        sig = self.d['signal']
        return scope.awg_set(sig['wave'], sig['freq_hz'], sig['high_v'], sig['low_v'], sig.get('duty_pct', 50.0))

    def assert_shows_the_declared_signal(self, measure):
        """The declaration is the oracle. A mismatched capture means the bench is not the declared one (a 10x probe
        in line, a divider, another channel), so the failure names bench.json instead of a bare number."""
        sig, tol, ch = self.d['signal'], self.d['tolerance'], self.channel
        freq = measure.get('freq_hz')
        assert freq is not None and abs(freq - sig['freq_hz']) <= tol['freq_rel'] * sig['freq_hz'], \
            ('bench.json %s: declares %.6g Hz on %s, the capture measured %s Hz. Check the wiring and the '
             'probe/divider this link declares, or fix the declaration.' % (self.name, sig['freq_hz'], ch, freq))
        for key, want in (('vmax_v', sig['high_v']), ('vmin_v', sig['low_v'])):
            got = measure.get(key)
            assert got is not None and abs(got - want) <= tol['level_v'], \
                ('bench.json %s: declares %s = %.3g V on %s, the capture measured %s. Check the probe attenuation '
                 'and any divider this link declares, or fix the declaration.' % (self.name, key, want, ch, got))
        duty = measure.get('duty_pct')
        if duty is not None and 'duty_pct' in sig:
            assert abs(duty - sig['duty_pct']) <= tol['duty_abs_pct'], \
                'bench.json %s: declares %.3g %% duty on %s, the capture measured %.3g %%' % (
                    self.name, sig['duty_pct'], ch, duty)


class Bench:
    """Every link the declaration names, plus the scope it was written for."""

    def __init__(self, d):
        self.d = d
        self.links = {name: Link(name, body) for name, body in d.get('links', {}).items()}

    @property
    def declared(self):
        return sorted(self.links)

    def link(self, name):
        if name not in self.links:
            pytest.skip('bench.json declares no link %r (declared: %s)' % (name, ', '.join(self.declared) or 'none'))
        return self.links[name]