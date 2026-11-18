"""Long-run helpers for the hwsoak suite: resource readings, drift, and the artifact files.

Artifacts follow the format already in re/soaks/ (see the 1000/1000 watch run): <stem>.meta with key=value lines,
<stem>.jsonl with one row per interval, <stem>.exit with the exit code, <stem>.err with whatever went wrong, plus a
summary.json holding every check and its measured value, so a failed run can be reviewed without the device.
"""
import json
import os
import statistics
import time

STATE = 'state'
MIB = 1024.0 * 1024.0


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
    """Growth inside the last quarter of a series: a warm-up plateaus, a leak keeps climbing.

    Measured on this bench: 21 open/close cycles grow RSS by 26 MiB over the first ~15 of them and then stay flat, so
    the total growth is not a leak signal — the steady-state slope is.
    """
    values = [v for v in values if v is not None]
    if len(values) < min_samples:
        return {'value': None, 'tolerance': tolerance, 'ok': True,
                'note': 'too few samples (%d < %d) to judge steady growth' % (len(values), min_samples)}
    tail = values[3 * len(values) // 4:]
    grow = max(tail) - min(tail)
    return {'value': round(grow, 3), 'tolerance': tolerance, 'ok': grow <= tolerance}


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
        self._write(exit_code=0 if all(self._ok(v) for v in checks.values()) else 1, summary=summary)

    @staticmethod
    def _ok(value):
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
