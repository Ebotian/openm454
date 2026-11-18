"""The soak disk budget is checked offline: the rule that decides whether a long run may start."""
import tempfile
from pathlib import Path

from tests.support import soak as soakmod


def test_a_path_that_does_not_exist_yet_is_measured_on_its_parent():
    root = Path(tempfile.mkdtemp())
    missing = root / "does" / "not" / "exist"
    assert soakmod.free_disk_mib(str(missing)) > 0
    assert soakmod.free_disk_mib(str(missing)) == soakmod.free_disk_mib(str(root))


def test_the_budget_uses_the_whole_run_and_a_margin(monkeypatch):
    monkeypatch.setattr(soakmod, "free_disk_mib", lambda path: 100.0)
    ok = soakmod.disk_budget("/tmp/soak", 50 * 1024 * 1024, margin=1.25)
    assert ok["ok"] and ok["need_mib"] == 50.0 and ok["margin"] == 1.25
    # 100 MiB free is not enough for 50 MiB x 1.25 when the free space is 60
    monkeypatch.setattr(soakmod, "free_disk_mib", lambda path: 60.0)
    tight = soakmod.disk_budget("/tmp/soak", 50 * 1024 * 1024, margin=1.25)
    assert not tight["ok"] and "60.0 MiB free" in tight["reason"]


def test_a_one_hour_log_does_not_start_with_a_gigabyte_floor(monkeypatch):
    """1 MSa/s for 3600 s is 3.6 GB: a 1 GiB floor would have started a run that cannot finish."""
    monkeypatch.setattr(soakmod, "free_disk_mib", lambda path: 1024.0)
    need = 1e6 * 3600
    budget = soakmod.disk_budget("/tmp/soak", need, margin=1.25)
    assert not budget["ok"] and budget["need_mib"] > 3000