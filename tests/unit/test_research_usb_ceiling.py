"""The full usb_ceiling entry point against a stand-in scope: a name in main() used to shadow the trace capture.

The review caught it: main() bound the output path to `transfers`, so `trace = transfers(scope)` raised TypeError after
the ceiling CSV had been written, and every run without --quick lost its trace. Everything here is offline.
"""
import csv
import json

import pytest

from tests.hardware.research import usb_ceiling as script

TRACE = [(0.0, 1024 * 1024, 16), (0.1005, 1024 * 1024, 15)]


class _Scope:
    def open(self):
        pass

    def close(self):
        pass

    def awg_status(self):
        return None

    def awg_off(self):
        pass

    def info(self):
        return {'device': {'model': 'stand-in'}}


class _Link:
    def program(self, scope):
        pass


class _Bench:
    def link(self, name):
        return _Link()


def run(tmp_path, monkeypatch, quick=False):
    monkeypatch.setattr(script.scopemod, 'Scope', _Scope)
    monkeypatch.setattr(script.benchmod, 'Bench', lambda declaration: _Bench())
    monkeypatch.setattr(script, 'matrix', lambda scope, seconds: [{'config': '1A 400M', 'host_mb_s': 287.0}])
    calls = []

    def transfers(scope):
        calls.append(scope)
        return list(TRACE)
    monkeypatch.setattr(script, 'transfers', transfers)
    argv = ['--bench-confirmed', '--out-dir', str(tmp_path)] + (['--quick'] if quick else [])
    return script.main(argv), calls


def manifest(tmp_path):
    return json.loads((tmp_path / 'manifest-usb_ceiling.json').read_text(encoding='utf-8'))


def test_a_full_run_captures_the_trace_and_writes_both_files(tmp_path, monkeypatch):
    code, calls = run(tmp_path, monkeypatch)
    assert code == 0 and len(calls) == 1, 'the trace has to be captured, not skipped'
    assert (tmp_path / 'usb-ceiling.csv').exists() and (tmp_path / 'usb-transfers.csv').exists()
    rows = list(csv.DictReader((tmp_path / 'usb-transfers.csv').open(encoding='utf-8')))
    assert len(rows) == 2 and rows[1]['bytes'] == str(1024 * 1024) and rows[1]['t_s'] == str(round(0.1005, 6))
    doc = manifest(tmp_path)
    assert doc['files'] == ['usb-ceiling.csv', 'usb-transfers.csv'] and doc['stale'] == []


def test_a_quick_run_skips_the_trace_and_marks_the_old_one_stale(tmp_path, monkeypatch):
    (tmp_path / 'usb-transfers.csv').write_text('t_s,bytes,inflight\n', encoding='utf-8')   # an earlier run
    code, calls = run(tmp_path, monkeypatch, quick=True)
    assert code == 0 and calls == []
    doc = manifest(tmp_path)
    assert doc['files'] == ['usb-ceiling.csv'] and doc['stale'] == ['usb-transfers.csv']
