"""Single-user lock for the scope (WinUSB allows one opener) and DSWave detection."""
import os
import subprocess

from .errors import DscopeError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK_PATH = os.path.join(ROOT, 'state', 'dscope.lock')


def dswave_pids():
    try:
        out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq DSWave.exe', '/FO', 'CSV', '/NH'],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[0].lower().startswith('dswave'):
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


class DeviceLock:
    def __init__(self):
        self.fh = None

    def acquire(self):
        import msvcrt
        os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
        self.fh = open(LOCK_PATH, 'a+')
        self.fh.seek(0)              # 'a+' starts at the end of the file: always lock byte 0 (release unlocks byte 0)
        try:
            msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.fh.close()
            self.fh = None
            raise DscopeError('BUSY', 'another dscope process is using the scope',
                              'wait for it (e.g. a running "dscope watch") or stop it, then retry')
        pids = dswave_pids()
        if pids:
            self.release()
            raise DscopeError('BUSY', 'DSWave is running (pid %s) and owns the scope' % ','.join(map(str, pids)),
                              'close DSWave, then retry')
        try:
            self.fh.seek(0); self.fh.truncate(); self.fh.write(str(os.getpid())); self.fh.flush()
        except OSError:
            self.release()
            raise DscopeError('BUSY', 'another dscope process is using the scope', 'wait for it, then retry')
        return self

    def release(self):
        if self.fh:
            import msvcrt
            try:
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self.fh.close()
            self.fh = None
