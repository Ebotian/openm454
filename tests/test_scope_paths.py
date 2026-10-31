"""Scope-level paths around the engine (no hardware): completeness checks, trigger domain, timeouts, budgets."""
import types

import pytest

from dscope import scope as scopemod
from dscope.acquire import RawCapture
from dscope.errors import DscopeError


class FakeDev:
    def __init__(self, speed='super'):
        self.zero_info = scopemod._cached_zero()
        self.info = {'usb_speed': speed}
        self.t = types.SimpleNamespace(running=False, stale=False)


class OneShotEngine:
    def __init__(self, raw):
        self.raw, self.calls = raw, []

    def run(self, prog, **kw):
        self.calls.append(kw)
        return self.raw


def scope_with(raw, speed='super'):
    s = scopemod.Scope()
    s.dev = FakeDev(speed)
    s.engine = OneShotEngine(raw)
    return s


def head(triggered=True):
    h = bytearray(4096)
    h[:4] = b'\x55\x55\x55\x55'
    if not triggered:
        h[8:12] = (0x80000000).to_bytes(4, 'little')
    return bytes(h)


def st_a1(**kw):
    st = scopemod.Settings()
    st.analog, st.digital = ['A1'], []
    st.trig_source, st.trig_mode = 'A1', 'normal'
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def test_incomplete_record_is_an_error_not_ok():
    raw = RawCapture()
    raw.head, raw.data, raw.timed_out = head(), bytearray(1000), True
    s = scope_with(raw)
    with pytest.raises(DscopeError) as ei:
        s.capture(st_a1())
    assert ei.value.code == 'TIMEOUT_DATA' and ei.value.exit_code == 6


def test_aborted_before_trigger_is_not_reported_as_no_trigger():
    raw = RawCapture()
    raw.aborted = True
    with pytest.raises(DscopeError) as ei:
        scope_with(raw).capture(st_a1())
    assert ei.value.code == 'TIMEOUT_WATCHDOG'


def test_untriggered_capture_uses_a_domain_with_channels():
    for an, dg, want in ((['A1'], [], (1, 1)), (['A2', 'A3'], ['D0'], (1, 2)), ([], ['D5', 'D7'], (0, 5))):
        st = scopemod.Settings()
        st.analog, st.digital, st.trig_source, st.trig_mode = an, dg, None, 'force'
        hal = scopemod.hal_config(st)[0]
        assert (hal['trigger']['ch_type'], hal['trigger']['ch_index']) == want


def test_trigger_wait_includes_the_record_time():
    raw = RawCapture()
    s = scope_with(raw)
    with pytest.raises(DscopeError) as ei:
        s.capture(st_a1(duration=1.0, timeout=0.5))
    assert ei.value.code == 'TIMEOUT_NO_TRIGGER'
    waited = s.engine.calls[0]['trigger_timeout']
    assert waited >= 0.5 + 1.0                   # the head of a 1 s non-roll record cannot come sooner


def test_usb2_link_is_refused_before_any_run():
    s = scope_with(RawCapture(), speed='high')
    with pytest.raises(DscopeError) as ei:
        s.capture(st_a1())
    assert ei.value.code == 'UNSUPPORTED' and s.engine.calls == []


def test_out_of_range_settings_are_invalid_arg():
    s = scopemod.Scope()
    with pytest.raises(DscopeError) as ei:
        s.plan(st_a1(duration=1000.0))
    assert ei.value.code == 'INVALID_ARG'


def test_stream_defaults_avoid_small_packet_mode():
    from dscope import cli
    a = cli.build_parser().parse_args(['stream', '-c', 'A1', '--duration', '10s'])
    geom = scopemod.Scope().plan(cli.stream_settings(a))['geometry']
    assert not geom['small_pkt'] and geom['need_bytes'] == geom['acqu_bytes'] + 4096


def test_offline_plan_works_without_the_calibration_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(scopemod, 'STATE', str(tmp_path))
    plan = scopemod.Scope().plan(st_a1())
    assert plan['geometry']['record_s'] > 0


def test_zero_duration_is_invalid_arg():
    with pytest.raises(DscopeError) as ei:
        scopemod.hal_config(st_a1(duration=0.0))
    assert ei.value.code == 'INVALID_ARG'


def test_stream_user_stop_before_data_is_reported_as_stopped():
    raw = RawCapture()
    raw.aborted = raw.stopped = True
    s = scope_with(raw)
    st = scopemod.Settings()
    st.analog, st.digital, st.duration = ['A1'], [], 1.0
    with pytest.raises(DscopeError) as ei:
        s.stream(st)
    assert ei.value.code == 'STOPPED' and ei.value.exit_code == 0


def test_cli_output_has_one_owner_after_the_watchdog_fires(capsys):
    from dscope import cli
    cli._emit({'a': 1})
    cli._OUT['owner'] = 'watchdog'
    try:
        cli._emit({'b': 2})                          # main thread: suppressed
        cli._emit({'c': 3}, owner='watchdog')
    finally:
        cli._OUT['owner'] = None
    out = capsys.readouterr().out.splitlines()
    assert out == ['{"a":1}', '{"c":3}']


def test_explicit_max_wall_only_extends_the_budget():
    from dscope import cli
    a = cli.build_parser().parse_args(['--max-wall', '5s', 'capture', '-c', 'A1'])
    computed, _ = cli._budget(a)
    assert computed >= 60.0                          # capture keeps a 60 s floor; main() uses max(given, computed)
