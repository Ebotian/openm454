"""Fixtures of the long-run group: the duration tier, the artifact writer, and the precondition checks."""
import os
import subprocess

import pytest

from tests.support import soak as soakmod

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _git(*args):
    try:
        return subprocess.check_output(['git'] + list(args), cwd=REPO, text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception:                                  # noqa: BLE001 - a checkout without git is still a checkout
        return 'unknown'


def _free_ram_mib():
    try:
        with open('/proc/meminfo', encoding='utf-8') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        return None
    return None


free_disk_mib = soakmod.free_disk_mib


@pytest.fixture(scope='session')
def soak_cfg(bench):
    cfg = bench.d.get('soak') or {}
    assert cfg.get('tiers'), 'bench.json has to declare the soak tiers the group runs'
    return cfg


@pytest.fixture
def duration(request, soak_cfg):
    """Seconds this run lasts: --soak-seconds wins, else the configured tier."""
    override = request.config.getoption('--soak-seconds')
    if override is not None:
        if override <= 0:
            raise pytest.UsageError('--soak-seconds has to be positive')
        return int(override)
    return int(soak_cfg['tiers'][soak_cfg.get('default', '10min')])


@pytest.fixture
def soak(request, soak_cfg, duration):
    """The artifact writer; a run that ends without finish() still leaves its evidence behind as a failure."""
    need = soak_cfg.get('require') or {}
    ram = _free_ram_mib()
    if ram is not None and ram < float(need.get('free_ram_mib', 512)):
        pytest.skip('not enough free RAM for a soak run: %.0f MiB < %s MiB' % (ram, need['free_ram_mib']))
    root = os.path.join(REPO, soak_cfg.get('artifact_dir', 're/soaks'))
    disk = free_disk_mib(root)
    if disk < float(need.get('free_disk_mib', 1024)):
        pytest.skip('not enough free disk for the artifacts in %s: %.0f MiB < %s MiB' % (
            root, disk, need['free_disk_mib']))
    meta = {'git': _git('rev-parse', 'HEAD'), 'branch': _git('rev-parse', '--abbrev-ref', 'HEAD'),
            'duration_s': duration, 'test': request.node.nodeid,
            'cmd': 'pytest %s' % request.node.nodeid}
    writer = soakmod.Soak(request.node.name.split('[')[0], root, meta)
    yield writer
    writer.abandon('the run ended without finish(): see the test failure above')


@pytest.fixture
def disk_guard(soak_cfg):
    """Check the filesystem the data will land on against the worst case of the whole run, with a margin.

    A one-hour log at 1 MSa/s is 3.6 GB; a fixed 1 GiB floor would start a run that cannot finish, and the number
    has to come from the path the data is written to, not from the checkout.
    """
    def guard(path, need_bytes, what):
        budget = soakmod.disk_budget(path, need_bytes, float(soak_cfg.get('disk_margin', 1.25)))
        if not budget['ok']:
            pytest.skip('%s: %s' % (what, budget['reason']))
        return budget

    return guard