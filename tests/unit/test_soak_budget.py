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


def test_steady_growth_is_tri_state():
    """pass / fail / insufficient_data: a short series must not count as verified."""
    short = soakmod.steady_growth([1.0] * 10, 8.0)
    assert short["state"] == "insufficient_data" and short["ok"] is None
    flat = soakmod.steady_growth([1.0] * 40, 8.0)          # the last quarter is flat
    assert flat["state"] == "pass" and flat["ok"] is True and flat["value"] == 0.0
    leaking = [1.0] * 30 + [float(i) for i in range(1, 11)]
    leak = soakmod.steady_growth(leaking, 8.0)             # the last quarter climbs by 9 MiB
    assert leak["state"] == "fail" and leak["ok"] is False and leak["value"] == 9.0


def test_a_release_inside_the_last_quarter_is_not_a_leak():
    """The stream soak on this bench ends by releasing 23 MiB; a peak-to-trough range reads that as growth."""
    plateau_then_release = [179.7] * 70 + [156.3] * 12
    released = soakmod.steady_growth(plateau_then_release, 8.0)
    assert released["state"] == "pass" and released["ok"] is True and released["value"] <= 0.0


def test_one_spike_does_not_decide_the_steady_verdict():
    """A single sample 40 MiB above the plateau is not a trend: the median pairwise slope stays at the plateau's."""
    spiked = [120.0] * 60 + [160.0] + [120.0] * 19
    steady = soakmod.steady_growth(spiked, 8.0)
    assert steady["state"] == "pass" and steady["value"] == 0.0


def test_net_growth_leaves_the_allocator_warmup_out():
    """A cold stream process climbs 62 MiB over its first records and stays flat after that: not a leak, and not
    something a whole-run bound can tell apart from one."""
    warmup_then_flat = [50.0 + 2 * i for i in range(20)] + [119.0] * 60
    net = soakmod.net_growth(warmup_then_flat, 64.0)
    assert net["state"] == "pass" and net["value"] == 50.0          # the whole run grew 69 MiB: the bound is 64


def test_net_growth_fails_on_a_step_that_stays():
    stepped = [100.0] * 40 + [180.0] * 40
    net = soakmod.net_growth(stepped, 64.0)
    assert net["state"] == "fail" and net["ok"] is False and net["value"] == 80.0
    down = soakmod.net_growth([180.0] * 40 + [156.0] * 40, 64.0)   # a release is negative growth
    assert down["state"] == "pass" and down["value"] == -24.0


def test_net_growth_is_tri_state():
    short = soakmod.net_growth([1.0, 2.0, 3.0], 64.0)
    assert short["state"] == "insufficient_data" and short["ok"] is None