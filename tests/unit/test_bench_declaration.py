"""The hardware declaration is checked offline, so a broken bench.json fails in CI and not on the bench.

Every link has to name a source, a sink, the probe/volts-div/offset a capture is configured for, the signal that
source puts on the wire and the tolerance it must be seen within. The numbers also have to agree with each other and
with the instrument: a declaration that clips its own signal, or asks the generator for 5 V, would make the hw test
fail for the wrong reason.
"""
import pytest

from tests.support import bench as benchmod

AWG_MAX_V = 3.0            # DSCope M454 waveform generator, 0..3 V
AWG_MAX_HZ = 1e6
DIVISIONS = 8.0
SOURCES = ('awg', 'cal', 'external')


def links():
    return benchmod.load()['links']


def test_the_declaration_is_not_empty():
    d = benchmod.load()
    assert d['links'], 'bench.json has to declare at least the link the hw suite uses'
    assert d['scope']['model'] == 'DSCope M454'


def test_every_declared_link_is_complete():
    for name, link in links().items():
        for key in ('source', 'sink', 'probe', 'vdiv_v', 'offset_v', 'signal', 'tolerance'):
            assert key in link, '%s: missing %r' % (name, key)
        assert link['source'] in SOURCES, name
        assert link['sink'] in benchmod.ANALOG + benchmod.DIGITAL, name
        assert 0 < link['probe'] <= 1000, name
        assert link['vdiv_v'] > 0, name
        assert link['signal']['high_v'] > link['signal']['low_v'], name
        assert link['tolerance']['freq_rel'] > 0 and link['tolerance']['level_v'] > 0, name


def test_the_declared_levels_fit_the_declared_settings():
    for name, link in links().items():
        sig = link['signal']
        assert 0.0 <= sig['low_v'] and sig['high_v'] <= AWG_MAX_V, '%s: the generator makes 0..3 V' % name
        assert 0.1 <= sig['freq_hz'] <= AWG_MAX_HZ, name
        if 'duty_pct' in sig:
            assert 0.1 <= sig['duty_pct'] <= 99.9, name
        span = DIVISIONS * link['vdiv_v'] * link['probe']      # the screen, at the probe tip
        lo, hi = link['offset_v'] - span / 2, link['offset_v'] + span / 2
        assert lo <= sig['low_v'] and sig['high_v'] <= hi, \
            '%s: %g..%g V leaves the declared screen (%g..%g V): fix vdiv/offset' % (
                name, sig['low_v'], sig['high_v'], lo, hi)


def test_the_hw_suite_only_uses_declared_links():
    """A test that asks for a link the file does not declare skips; this keeps the names in step."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / 'hardware' / 'test_awg_loopback.py').read_text()
    used = set(re.findall(r"bench\.link\('([^']+)'\)", src))
    assert used <= set(links()), 'undeclared link(s) used by the hw tests: %s' % sorted(used - set(links()))