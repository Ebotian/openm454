"""Long-run helpers for the hwsoak suite: resource readings, drift, and the artifact files.

Artifacts follow the format already in re/soaks/ (see the 1000/1000 watch run): <stem>.meta with key=value lines,
<stem>.jsonl with one row per interval, <stem>.exit with the exit code, <stem>.err with whatever went wrong, plus a
summary.json holding every check and its measured value, so a failed run can be reviewed without the device.
"""
import json
import os
import shutil
import statistics
import time

STATE = 'state'
MIB = 1024.0 * 1024.0


def free_disk_mib(path):
    """Free space on the filesystem that will hold the data: the nearest existing directory of path.

    The checkout and tmp_path are not necessarily on the same filesystem, so a caller has to ask about the path it
    actually writes to.
    """
    probe = os.path.abspath(path)
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return shutil.disk_usage(probe).free / (1024.0 * 1024.0)


def disk_budget(path, need_bytes, margin=1.25):
    """Is there room for a run that will write need_bytes to path, with a margin? Returns the numbers it judged."""
    need_mib = need_bytes / (1024.0 * 1024.0)
    free_mib = free_disk_mib(path)
    ok = free_mib >= need_mib * margin
    return {'ok': ok, 'need_mib': round(need_mib, 1), 'free_mib': round(free_mib, 1), 'margin': margin,
            'reason': None if ok else '%.1f MiB free on %s, %.1f MiB needed (x%.2f)' % (
                free_mib, path, need_mib, margin)}


def rss_mib():
    """Resident set size of this process, or None where the platform does not say (the checks then skip)."""
    try:
        with open('/proc/self/status', encoding='utf-8') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        return None
    return None


def _count(path):
    try:
        return len(os.listdir(path))
    except OSError:
        return None


def fd_count():
    return _count('/proc/self/fd')


def thread_count():
    return _count('/proc/self/task')


def drift(values, tolerance):
    """Median of the first tenth against the last tenth: (first, last, relative drift, ok)."""
    n = len(values)
    k = max(1, n // 10)
    first, last = statistics.median(values[:k]), statistics.median(values[-k:])
    rel = abs(last - first) / first if first else 0.0
    return {'first': first, 'last': last, 'relative': rel, 'tolerance': tolerance, 'ok': rel <= tolerance}


def steady_growth(values, tolerance, min_samples=40):
    """Growth inside the last quarter of a series: pass, fail, or insufficient_data — never a silent pass.

    Measured on this bench: 21 open/close cycles grow RSS by 26 MiB over the first ~15 of them and then stay flat, so
    the total growth is not a leak signal — the steady-state slope is. The value is the median of every pairwise slope
    inside that quarter (Theil-Sen) times the quarter's span, so a single step cannot decide the verdict: a stream run
    that ends by releasing 23 MiB drops RSS inside the tail, which is negative growth and not a leak, while a climb
    that every pair agrees on is still reported at nearly its full height. A series with too few points cannot judge
    it, and says so instead of counting as verified.
    """
    values = [v for v in values if v is not None]
    if len(values) < min_samples:
        return {'value': None, 'tolerance': tolerance, 'ok': None, 'state': 'insufficient_data',
                'note': 'too few samples (%d < %d) to judge steady growth' % (len(values), min_samples)}
    tail = values[3 * len(values) // 4:]
    slopes = [(b - a) / (j - i) for i, a in enumerate(tail) for j, b in enumerate(tail) if j > i]
    grow = round(statistics.median(slopes) * (len(tail) - 1), 3)
    ok = grow <= tolerance
    return {'value': grow, 'tolerance': tolerance, 'ok': ok, 'state': 'pass' if ok else 'fail'}


def net_growth(values, tolerance, warmup=0.25):
    """RSS left after the allocator warm-up: median of the last warmup-fraction of the samples against the first.

    The warm-up is real work, not a leak: a cold stream process climbs 57 -> 119 MiB over its first records and then
    stays flat, which made a 64 MiB whole-run bound fail by 0.4 MiB, while a warm process grew 0.4 MiB over the same
    run because its warm-up had happened inside an earlier test. Skipping the first quarter leaves the check the run
    actually promises — a step that stays — and a release is negative growth, not a failure. Tri-state like the
    steady check: a series too short to judge says so instead of counting as verified.
    """
    values = [v for v in values if v is not None]
    if len(values) < 4:
        return {'value': None, 'tolerance': tolerance, 'ok': None, 'state': 'insufficient_data',
                'note': 'too few samples (%d < 4) to judge growth after the warm-up' % len(values)}
    k = max(1, int(len(values) * warmup))
    base, end = statistics.median(values[:k]), statistics.median(values[-k:])
    grow = round(end - base, 3)
    ok = grow <= tolerance
    return {'value': grow, 'tolerance': tolerance, 'ok': ok, 'state': 'pass' if ok else 'fail'}


class Soak:
    """One long run: rows in, artifacts out.

    checks: a dict of name -> {'value': ..., 'ok': bool} (or a bare number, read as ok=True). finish() writes the
    files and raises nothing; the test asserts on what it passed in, so a failure still leaves the artifacts behind.
    """

    def __init__(self, name, root, meta=None):
        os.makedirs(root, exist_ok=True)
        stamp = time.strftime('%Y%m%d-%H%M%S')
        self.stem = os.path.join(root, '%s-%s' % (name, stamp))
        self.name = name
        self.meta = dict(meta or {})
        self.rows = []
        self.t0 = time.time()
        self.finished = False

    def row(self, **kw):
        kw['t_s'] = round(time.time() - self.t0, 3)
        self.rows.append(kw)

    def snapshot(self):
        return {'rss_mib': rss_mib(), 'fds': fd_count(), 'threads': thread_count(), 't_s': round(time.time() - self.t0, 3)}

    def growth(self, before, after):
        out = {}
        for key in ('rss_mib', 'fds', 'threads'):
            a, b = before.get(key), after.get(key)
            out[key] = None if (a is None or b is None) else round(b - a, 3)
        return out

    def finish(self, checks, notes=None):
        summary = {'test': self.name, 'date': time.strftime('%Y-%m-%dT%H:%M:%S'), 'seconds': round(time.time() - self.t0, 1),
                   'checks': checks, 'notes': notes or {}}
        self.finished = True
        failed = any(self._ok(v) is False for v in checks.values())
        self._write(exit_code=1 if failed else 0, summary=summary)

    @staticmethod
    def _ok(value):
        """True: passed. False: failed. None (insufficient_data): not verified, not a failure."""
        return value.get('ok', True) if isinstance(value, dict) else True

    def abandon(self, reason):
        """Called by the fixture when the test ended without finish(): keep the evidence and mark the run failed."""
        if self.finished:
            return
        self._write(exit_code=1, summary={'test': self.name, 'seconds': round(time.time() - self.t0, 1),
                                          'error': reason, 'checks': {}})

    def _write(self, exit_code, summary):
        for ext, body in (('.meta', self._meta_text()), ('.jsonl', ''.join(json.dumps(r, separators=(',', ':')) + '\n' for r in self.rows)),
                          ('.exit', '%d\n' % exit_code), ('.err', '' if exit_code == 0 else (summary.get('error') or '') + '\n'),
                          ('.summary.json', json.dumps(summary, indent=1, sort_keys=True) + '\n')):
            with open(self.stem + ext, 'w', encoding='utf-8') as f:
                f.write(body)

    def _meta_text(self):
        lines = ['date=%s' % time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'test=%s' % self.name]
        for key, value in sorted(self.meta.items()):
            lines.append('%s=%s' % (key, value))
        return '\n'.join(lines) + '\n'
