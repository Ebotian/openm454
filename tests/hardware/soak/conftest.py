"""Fixtures of the long-run group: the duration tier, the artifact writer, and the precondition checks."""
import os
import shutil
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
    disk = shutil.disk_usage(REPO).free / (1024.0 * 1024.0)
    if disk < float(need.get('free_disk_mib', 1024)):
        pytest.skip('not enough free disk for a soak run: %.0f MiB < %s MiB' % (disk, need['free_disk_mib']))
    root = os.path.join(REPO, soak_cfg.get('artifact_dir', 're/soaks'))
    meta = {'git': _git('rev-parse', 'HEAD'), 'branch': _git('rev-parse', '--abbrev-ref', 'HEAD'),
            'duration_s': duration, 'test': request.node.nodeid,
            'cmd': 'pytest %s' % request.node.nodeid}
    writer = soakmod.Soak(request.node.name.split('[')[0], root, meta)
    yield writer
    writer.abandon('the run ended without finish(): see the test failure above')
