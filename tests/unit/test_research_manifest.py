"""The research manifest says which files this run wrote, and which ones in the same directory are older.

The review's case: rerunning usb_ceiling.py with --quick in a directory that already holds usb-transfers.csv updated
the manifest and the ceiling CSV, left the trace from the previous run, and gave a reader no way to tell.
"""
import argparse
import json
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
