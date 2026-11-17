"""Self-test logic (openm454/selftest.py) against a fake scope: which checks run on which wiring, and that the
wiring-free checks never touch the signal generator."""
import pytest

from openm454 import selftest
from openm454.errors import DscopeError
from openm454.fixture import STANDARD, UNKNOWN


class FakeCap:
    def __init__(self, st, short=False):
        an, dg = list(st.analog), list(st.digital)
        rate = st.rate or 100e6
        self.n = int(rate * st.duration * (0.5 if short else 1.0)) + 32
        self.meta = {'acq': {'analog_rate_hz': rate if an else None, 'digital_rate_hz': rate if dg else None,
                             'stop': 'normal', 'complete': True, 'triggered': st.trig_mode != 'force'}}

    def measure(self, ch, edges_n=0):
        return {'samples': self.n, 'state': 'low', 'vpp_v': 0.0}


class FakeScope:
    def __init__(self, short=False, fail_on=None, awg_enabled=None, fail_while_driven=False):
        self.calls, self.short, self.fail_on = [], short, fail_on
        self.awg_state = None if awg_enabled is None else {'enabled': awg_enabled}
        self.driven, self.fail_while_driven = False, fail_while_driven

    def info(self):
        return {'device': {'fpga_done': True, 'security_pass': True, 'fw_version': '0x3110', 'usb_speed': 'super',
                           'fpga_bin_version': '0x3100', 'panel_version': '0x3100'}}

    def capture(self, st):
        self.calls.append(('capture', st.trig_mode, tuple(st.analog), tuple(st.digital), st.rate))
        if self.fail_on and len(self.calls) == self.fail_on:
            raise DscopeError('USB_ERROR', 'transfer failed')
        if self.fail_while_driven and self.driven:
            raise DscopeError('TIMEOUT_NO_TRIGGER', 'no trigger')
        return FakeCap(st, self.short)

    def awg_status(self):                    # host-side record of the last setting: reading it touches nothing
        return self.awg_state

    def awg_set(self, *a, **k):
        self.calls.append(('awg_set',))
        self.driven = True

    def awg_off(self):
        self.calls.append(('awg_off',))
        self.driven = False


def test_unknown_wiring_runs_only_the_wiring_free_checks():
    sc = FakeScope()
    res = selftest.run(sc, quick=True, awg=True, fx=UNKNOWN)
    ran = [i['id'] for i in res['items'] if not i.get('skipped')]
    assert ran == ['V01', 'V12', 'V13', 'V11'] and res['ok'] and res['passed'] == 4
    assert all(c[0] == 'capture' and c[1] == 'force' for c in sc.calls)       # forced only, generator untouched
    assert res['skipped'] == len(res['items']) - 4 and 'standard fixture' in res['note']
    v13 = next(i for i in res['items'] if i['id'] == 'V13')['measured']
    assert {k: v['rate'] for k, v in v13.items()} == {'A1': 400e6, 'A1,A3': 200e6, 'D0,D1': 400e6, 'D0-D3': 200e6}


def test_a_short_record_fails_the_wiring_free_checks():
    res = selftest.run(FakeScope(short=True), quick=True, awg=False, fx=UNKNOWN)
    by_id = {i['id']: i for i in res['items']}
    assert not res['ok'] and by_id['V12']['pass'] is False and by_id['V13']['pass'] is False


def test_a_device_error_stops_the_run():
    res = selftest.run(FakeScope(fail_on=1), quick=True, awg=False, fx=UNKNOWN)
    assert not res['ok'] and res['stopped_at'] == 'V12' and res['device_error']['code'] == 'USB_ERROR'
    assert all(i.get('skipped') for i in res['items'] if i['id'] in ('V13', 'V11')) and 'note' not in res


@pytest.mark.parametrize('awg', [True, False])
def test_the_standard_fixture_enables_the_signal_checks(awg):
    sc = FakeScope()
    res = selftest.run(sc, quick=True, awg=awg, fx=STANDARD)
    ids = [i['id'] for i in res['items']]
    assert not any(i.get('skipped') for i in res['items']) and 'note' not in res
    assert ids[:4] == ['V01', 'V00', 'V12', 'V13']                       # the lowest-risk capture first
    touched = [c for c in sc.calls if c[0].startswith('awg')]
    assert ('V10' in ids) == awg and bool(touched) == awg                  # the generator only with awg
    assert not sc.driven


def test_v09_is_skipped_while_the_generator_runs_and_may_not_be_touched():
    sc = FakeScope(awg_enabled=True)
    res = selftest.run(sc, quick=True, awg=False, fx=STANDARD)
    v09 = next(i for i in res['items'] if i['id'] == 'V09')
    assert v09['skipped'] and v09['pass'] is None and not v09['gating'] and 'awg off' in v09['measured']['skipped']
    assert not any(c[0].startswith('awg') for c in sc.calls)
    sc = FakeScope(awg_enabled=False)                                    # a stopped generator: V09 runs
    res = selftest.run(sc, quick=True, awg=False, fx=STANDARD)
    assert not next(i for i in res['items'] if i['id'] == 'V09').get('skipped')


def test_v10_turns_the_generator_off_when_a_capture_fails():
    sc = FakeScope(fail_while_driven=True)
    res = selftest.run(sc, quick=True, awg=True, fx=STANDARD)
    v10 = next(i for i in res['items'] if i['id'] == 'V10')
    assert v10['pass'] is False and v10['measured']['error']['code'] == 'TIMEOUT_NO_TRIGGER'
    assert not sc.driven and sc.calls[-1][0] != 'awg_set'


def test_run_reads_the_recorded_wiring(monkeypatch):
    monkeypatch.setattr(selftest.fixture, 'load', lambda: dict(STANDARD))
    res = selftest.run(FakeScope(), quick=True, awg=False)
    assert res['fixture'] == STANDARD and not any(i.get('skipped') for i in res['items'])
    monkeypatch.setattr(selftest.fixture, 'load', lambda: dict(UNKNOWN, error='fixture.json could not be read (x)'))
    res = selftest.run(FakeScope(), quick=True, awg=False)
    assert res['note'].startswith('fixture.json could not be read (x); the signal checks')


def test_cli_fixture_option():
    from openm454.cli import build_parser
    a = build_parser().parse_args(['selftest', '--quick', '--no-awg', '--fixture', 'standard'])
    assert (a.quick, a.no_awg, a.fixture) == (True, True, 'standard')
    assert build_parser().parse_args(['selftest']).fixture == 'file'
    with pytest.raises(SystemExit):
        build_parser().parse_args(['selftest', '--fixture', 'bench'])


def test_a_missing_fixture_file_means_unknown_wiring(tmp_path):
    from openm454 import fixture
    assert fixture.load(str(tmp_path / 'none.json')) == UNKNOWN
    p = tmp_path / 'f.json'
    p.write_text('{"analog": "standard"}')
    assert fixture.load(str(p)) == {'analog': 'standard', 'digital': 'unknown'}
    p.write_text('\ufeff{"analog": "standard", "digital": "standard"}', encoding='utf-8')   # PowerShell Out-File
    assert fixture.load(str(p)) == STANDARD
    for bad in ('[1, 2]', '{"analog": '):
        p.write_text(bad)
        d = fixture.load(str(p))
        assert {k: d[k] for k in UNKNOWN} == UNKNOWN and 'f.json' in d['error']
