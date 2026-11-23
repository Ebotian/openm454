"""The research manifest says which files this run wrote, and which ones in the same directory are older.

The review's case: rerunning usb_ceiling.py with --quick in a directory that already holds usb-transfers.csv updated
the manifest and the ceiling CSV, left the trace from the previous run, and gave a reader no way to tell.
"""
import argparse
import json

import pytest
import types

from tests.support import bench as benchmod


class _Scope:
    """Only what a manifest needs: a device that answers nothing."""
    def info(self):
        return {}


def manifest(out_dir, tmp_path, files, candidates):
    args = argparse.Namespace(quick=False, seconds=1.2, out_dir=str(out_dir))
    doc = benchmod.write_manifest(str(out_dir), _Scope(), str(tmp_path / 'usb_ceiling.py'), args,
                                  files=[out_dir / f for f in files], candidates=candidates)
    return doc, json.loads((out_dir / 'manifest-usb_ceiling.json').read_text())


def test_a_quick_run_records_the_trace_from_an_earlier_run_as_stale(tmp_path):
    out = tmp_path / 'out'
    out.mkdir()
    (out / 'usb-transfers.csv').write_text('t_s,bytes,inflight\n', encoding='utf-8')     # an older run's trace
    doc, written = manifest(out, tmp_path, ['usb-ceiling.csv'], ['usb-ceiling.csv', 'usb-transfers.csv'])
    assert written['files'] == ['usb-ceiling.csv'] and written['stale'] == ['usb-transfers.csv']
    assert doc['stale'] == ['usb-transfers.csv']


def test_a_full_run_owns_both_files_and_reports_nothing_stale(tmp_path):
    out = tmp_path / 'out'
    out.mkdir()
    (out / 'usb-transfers.csv').write_text('t_s,bytes,inflight\n', encoding='utf-8')
    _doc, written = manifest(out, tmp_path, ['usb-ceiling.csv', 'usb-transfers.csv'],
                             ['usb-ceiling.csv', 'usb-transfers.csv'])
    assert written['files'] == ['usb-ceiling.csv', 'usb-transfers.csv'] and written['stale'] == []


class _Bench:
    def link(self, name):
        return _Link()


class _Link:
    def program(self, scope):
        pass


class _FailingScope:
    """The scope the signal-quality script drives: usable, but the first capture raises."""
    def open(self):
        pass

    def close(self):
        pass

    def awg_status(self):
        return None

    def awg_set(self, *a, **kw):
        pass

    def awg_off(self):
        pass

    def info(self):
        return {'device': {'model': 'stand-in'}}


def test_a_failed_capture_does_not_list_an_old_csv_as_this_runs_output(tmp_path, monkeypatch):
    """The review's case: the first capture fails, the old signal-quality.csv stays untouched, and the manifest listed
    it as this run's file with nothing stale. A file is registered only once it has been written."""
    from tests.hardware.research import signal_quality as script
    out = tmp_path / 'out'
    out.mkdir()
    (out / 'signal-quality.csv').write_text('round,config\n', encoding='utf-8')      # an earlier run's file
    monkeypatch.setattr(script.scopemod, 'Scope', _FailingScope)
    monkeypatch.setattr(script.benchmod, 'Bench', lambda declaration: _Bench())
    monkeypatch.setattr(script, 'one', lambda scope, an, dg, rate: (_ for _ in ()).throw(RuntimeError('capture failed')))
    with pytest.raises(RuntimeError):
        script.main(['--bench-confirmed', '--out-dir', str(out)])
    doc = json.loads((out / 'manifest-signal_quality.json').read_text(encoding='utf-8'))
    assert doc['files'] == [] and doc['stale'] == ['signal-quality.csv']
    assert 'capture failed' in doc['extra']['error']
    assert (out / 'signal-quality.csv').read_text(encoding='utf-8') == 'round,config\n', 'the old file is untouched'
