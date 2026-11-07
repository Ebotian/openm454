"""Extract the M454 FPGA bitstream from a locally installed DSWave.exe (never redistributed).

DSWave 1.2.1 embeds the bitstream in a 'DSLZ' container: b'DSLZ' + u32 BE raw size + SHA-256(raw) + LZ4 block.
DSWave is needed once: the verified bitstream is cached in firmware/ in the data folder (openm454.paths.ROOT).

Where DSWave.exe is looked for, in order (a folder holding DSWave.exe or the exe itself; files are only read):
  1. $OPENM454_DSWAVE ($DSCOPE_DSWAVE, the old name)
  2. the path saved in state/dswave.json (save_dswave; `python -m openm454.firmware <folder>`), which also reaches
     processes that do not see the user's shell variables, such as the MCP server Claude starts in the background
  3. a copy in DSWave/ in the data folder (the checkout, or %LOCALAPPDATA%\\openm454 for an installed copy)
  4. the Windows uninstall entries whose DisplayName contains DSWave (HKLM and HKCU, 64- and 32-bit views, read only)
  5. the usual install folders: Program Files, Program Files (x86), %LOCALAPPDATA%\\Programs, each with DSWave\\ or
     DreamSourceLab\\DSWave\\. No disk is searched beyond these.
"""
import hashlib
import itertools
import json
import os
import struct
import sys
import time

from .errors import DscopeError
from .paths import ROOT

DSWAVE_EXE_SHA256 = 'A21A1D410593C4981F1D10309E7A819E60BBACA2558563249EE939BB37ECA42C'
DSLZ_OFFSET, DSLZ_LEN = 0xC471E0, 0x3E414
RAW_SIZE = 466408
RAW_SHA256 = 'f45a97ea44ffda110cffc659eaba250d191985f7e60fdfcbd153b57372e9cfa1'
BIN_VERSION = 0x3100

EXE = 'DSWave.exe'
CACHE = os.path.join(ROOT, 'firmware', 'm454_fpga_%04x.bin' % BIN_VERSION)
SAVED = os.path.join(ROOT, 'state', 'dswave.json')
SAVE_CMD = 'python -m openm454 setup --dswave "<folder>"'
UNINSTALL_KEY = r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'
REG_VALUES = ('DisplayName', 'DisplayVersion', 'InstallLocation', 'DisplayIcon', 'UninstallString')
SOURCES = {'env': '$OPENM454_DSWAVE', 'saved': 'the saved path', 'copy': 'the DSWave/ copy',
           'registry': 'the uninstall entry', 'folder': 'the install folder'}


# ------------------------------------------------------------------------------------------- where DSWave is
def exe_path(p):
    """A folder holding DSWave.exe, or the exe itself -> the exe path (quotes, %VARS%/$VARS and ~ expanded)."""
    p = os.path.expanduser(os.path.expandvars(str(p).strip().strip('"').strip()))
    if os.path.isdir(p) or not p.lower().endswith('.exe'):
        return os.path.join(p, EXE)
    return p


def _env():
    for name in ('OPENM454_DSWAVE', 'DSCOPE_DSWAVE'):
        if os.environ.get(name, '').strip():
            return name, os.environ[name]
    return None, None


def load_saved():
    """The DSWave.exe path recorded by save_dswave() (state/dswave.json), or None."""
    try:
        with open(SAVED, encoding='utf-8-sig') as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    p = d.get('dswave') if isinstance(d, dict) else None
    return p if isinstance(p, str) and p.strip() else None


def _reg_value(winreg, key, name):
    try:
        v = winreg.QueryValueEx(key, name)[0]
    except OSError:
        return ''
    return v if isinstance(v, str) else ''


def _dswave_entries():
    """Uninstall entries whose DisplayName contains DSWave, as {value name: str}; HKLM and HKCU, 64- and 32-bit
    views, opened with KEY_READ only. [] where there is no Windows registry."""
    try:
        import winreg
    except ImportError:
        return []
    out = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            access = winreg.KEY_READ | view
            try:
                root = winreg.OpenKey(hive, UNINSTALL_KEY, 0, access)
            except OSError:
                continue
            with root:
                for i in itertools.count():
                    try:
                        name = winreg.EnumKey(root, i)
                    except OSError:
                        break
                    try:
                        with winreg.OpenKey(root, name, 0, access) as k:
                            if 'dswave' in _reg_value(winreg, k, 'DisplayName').lower():
                                out.append({v: _reg_value(winreg, k, v) for v in REG_VALUES})
                    except OSError:
                        continue
    return out


def _exe_dir(cmd):
    """Folder of the program in a registry command line ('"C:\\a b\\uninst.exe" /AllUsers', 'C:\\a\\x.exe,0')."""
    s = (cmd or '').strip()
    if s.startswith('"'):
        s = s[1:].split('"', 1)[0]
    else:
        i = s.lower().find('.exe')
        if i >= 0:
            s = s[:i + 4]
    return os.path.dirname(s)


def registry_dirs():
    """Install folders named by the DSWave uninstall entries (InstallLocation, else the folder of DisplayIcon or
    UninstallString), DSWave 1.2.1 entries first."""
    entries = sorted(_dswave_entries(), key=lambda e: '1.2.1' not in e['DisplayVersion'] + e['DisplayName'])
    out = []
    for e in entries:
        for d in (e['InstallLocation'].strip().strip('"'), _exe_dir(e['DisplayIcon']), _exe_dir(e['UninstallString'])):
            d = os.path.expandvars(d) if d else d
            if d and os.path.isabs(d) and d not in out:
                out.append(d)
    return out


def common_dirs():
    """The usual install folders: Program Files (both), %LOCALAPPDATA%\\Programs; DSWave or DreamSourceLab\\DSWave."""
    la = os.environ.get('LOCALAPPDATA')
    bases = [os.environ.get(v) for v in ('ProgramW6432', 'ProgramFiles', 'ProgramFiles(x86)')]
    bases.append(os.path.join(la, 'Programs') if la else None)
    out = []
    for b in bases:
        for sub in (('DSWave',), ('DreamSourceLab', 'DSWave')):
            p = os.path.join(b, *sub) if b else None
            if p and p not in out:
                out.append(p)
    return out


def candidates():
    """[(source, DSWave.exe path)] in search order (module docstring), duplicates removed. Only names paths."""
    out = []
    env = _env()[1]
    if env:
        out.append(('env', exe_path(env)))
    saved = load_saved()
    if saved:
        out.append(('saved', exe_path(saved)))
    out.append(('copy', os.path.join(ROOT, 'DSWave', EXE)))
    out += [('registry', exe_path(d)) for d in registry_dirs()]
    out += [('folder', os.path.join(d, EXE)) for d in common_dirs()]
    seen, res = set(), []
    for src, p in out:
        k = os.path.normcase(os.path.abspath(p))
        if k not in seen:
            seen.add(k)
            res.append((src, p))
    return res


def candidate_exes():
    return [p for _, p in candidates()]


def is_dswave_121(path):
    """True when the file is DSWave 1.2.1 (SHA-256 of the whole exe)."""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest().upper() == DSWAVE_EXE_SHA256


def locate(verify=False):
    """-> (source, exe) of the first candidate that exists (verify=True: and is DSWave 1.2.1), else (None, None)."""
    for src, exe in candidates():
        if os.path.isfile(exe):
            try:
                if not verify or is_dswave_121(exe):
                    return src, exe
            except OSError:
                continue
    return None, None


# ------------------------------------------------------------------------------------------- the bitstream
def _lz4_block_decode(src, size):
    try:
        import lz4.block
        return lz4.block.decompress(src, uncompressed_size=size)
    except ImportError:
        pass
    dst = bytearray()
    i, n = 0, len(src)
    while i < n:
        tok = src[i]; i += 1
        lit = tok >> 4
        if lit == 15:
            while True:
                b = src[i]; i += 1; lit += b
                if b != 255:
                    break
        dst += src[i:i + lit]; i += lit
        if i >= n:
            break
        off = src[i] | (src[i + 1] << 8); i += 2
        ml = tok & 15
        if ml == 15:
            while True:
                b = src[i]; i += 1; ml += b
                if b != 255:
                    break
        ml += 4
        start = len(dst) - off
        for k in range(ml):
            dst.append(dst[start + k])
    return bytes(dst)


def extract_from_exe(path):
    with open(path, 'rb') as f:
        data = f.read()
    if hashlib.sha256(data).hexdigest().upper() != DSWAVE_EXE_SHA256:
        raise DscopeError('FIRMWARE_SOURCE', '%s is not DSWave 1.2.1 (hash mismatch)' % path,
                          'DSWave 1.2.1 is needed: other versions embed a different FPGA bitstream')
    blob = data[DSLZ_OFFSET:DSLZ_OFFSET + DSLZ_LEN]
    if blob[:4] != b'DSLZ' or struct.unpack('>I', blob[4:8])[0] != RAW_SIZE:
        raise DscopeError('FIRMWARE_SOURCE', 'DSLZ container not found where expected')
    raw = _lz4_block_decode(blob[40:], RAW_SIZE)
    if len(raw) != RAW_SIZE or hashlib.sha256(raw).digest() != blob[8:40] or hashlib.sha256(raw).hexdigest() != RAW_SHA256:
        raise DscopeError('FIRMWARE_SOURCE', 'bitstream checksum mismatch')
    return raw


def cached():
    """The cached bitstream (firmware/ in the data folder) if it is there and verifies, else None."""
    try:
        with open(CACHE, 'rb') as f:
            raw = f.read()
    except OSError:
        return None
    return raw if hashlib.sha256(raw).hexdigest() == RAW_SHA256 else None


def _write_cache(raw):
    """Store the verified bitstream (atomically; a read-only data folder only costs the cache). -> True if stored."""
    tmp = '%s.%d.tmp' % (CACHE, os.getpid())
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(tmp, 'wb') as f:
            f.write(raw)
        os.replace(tmp, CACHE)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def not_found_hint():
    return ('install DSWave 1.2.1 from DreamSourceLab (it is then found through its uninstall entry), or tell openm454 '
            'once where it is (<folder> holds DSWave.exe): %s saves the path in %s, which every openm454 process '
            'reads, the MCP server included; or set OPENM454_DSWAVE=<folder>. The MCP server is started by Claude in '
            'the background and does not see variables set later in a shell: give it the variable when registering it, '
            'claude mcp add -s user openm454 -e "OPENM454_DSWAVE=<folder>" -- openm454-mcp '
            "(in PowerShell write the separator in quotes: '--'), then restart Claude. After one extraction the "
            'bitstream is cached in %s and DSWave is no longer needed.' % (SAVE_CMD, SAVED, os.path.dirname(CACHE)))


def bitstream():
    """The verified FPGA bitstream: from the cache, else extracted from the first DSWave 1.2.1 found (and cached)."""
    raw = cached()
    if raw is not None:
        return raw
    problems = []
    for src, exe in candidates():
        if not os.path.isfile(exe):
            if src == 'env':
                problems.append('%s=%s: no %s there' % (_env()[0], _env()[1], EXE))
            elif src == 'saved':
                problems.append('saved path %s (%s) no longer exists' % (exe, SAVED))
            continue
        try:
            raw = extract_from_exe(exe)
        except (DscopeError, OSError) as e:
            problems.append('%s (%s): %s' % (exe, SOURCES[src], getattr(e, 'message', None) or e))
            continue
        _write_cache(raw)
        return raw
    raise DscopeError('FIRMWARE_SOURCE', 'DSWave 1.2.1 not found; it is needed once to extract the FPGA bitstream '
                      '(openm454 does not redistribute it). Looked at $OPENM454_DSWAVE, the saved path, %s, the '
                      'Windows uninstall entries, Program Files and %%LOCALAPPDATA%%\\Programs%s'
                      % (os.path.join(ROOT, 'DSWave'), ('. ' + '; '.join(problems)) if problems else ''),
                      not_found_hint())


def save_dswave(path, check=True):
    """Record where DSWave is (its folder or DSWave.exe) in state/dswave.json, for every later openm454 process.
    check=True first verifies it is DSWave 1.2.1 and caches the bitstream (then DSWave is no longer needed).
    -> status()."""
    exe = os.path.abspath(exe_path(path))
    if not os.path.isfile(exe):
        raise DscopeError('INVALID_ARG', 'no %s at %s' % (EXE, exe), 'give the DSWave 1.2.1 folder or its %s' % EXE)
    if check:
        _write_cache(extract_from_exe(exe))
    tmp = '%s.%d.tmp' % (SAVED, os.getpid())
    os.makedirs(os.path.dirname(SAVED), exist_ok=True)
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'dswave': exe, 'since': time.strftime('%Y-%m-%d %H:%M')}, f, indent=1)
    os.replace(tmp, SAVED)
    return status(verify=check)          # checked: report the verified source, not a wrong version found earlier


def status(verify=False):
    """Bitstream source report (no USB): {'cached', 'cache', 'dswave', 'source', 'saved', 'ready'[, 'hint']}.
    cached: the verified bitstream is in firmware/, so DSWave is not needed; dswave/source: the DSWave.exe the search
    finds first and where it came from (env, saved, copy, registry, folder), verify=True: only DSWave 1.2.1 (hashes it);
    ready: the FPGA can be configured (cached, or a DSWave.exe found)."""
    src, exe = locate(verify)
    d = {'cached': cached() is not None, 'cache': CACHE, 'dswave': exe, 'source': src, 'saved': load_saved()}
    d['ready'] = d['cached'] or exe is not None
    if not d['ready']:
        d['hint'] = not_found_hint()
    return d


def main(argv=None):
    """python -m openm454.firmware [FOLDER]: with a folder (or DSWave.exe), check it, cache the bitstream and save the
    path; without, extract the bitstream if it is not cached yet. Prints one JSON document."""
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ('-h', '--help'):
        print(main.__doc__)
        return 0
    try:
        if argv:
            d = save_dswave(argv[0])
        else:
            bitstream()
            d = status()
    except DscopeError as e:
        print(json.dumps({'ok': False, 'cmd': 'firmware', 'error': e.to_dict()}))
        return e.exit_code
    except OSError as e:
        print(json.dumps({'ok': False, 'cmd': 'firmware', 'error': {
            'code': 'INVALID_ARG', 'message': 'could not read DSWave or write the data folder: %s' % e}}))
        return 2
    for k in ('cache', 'dswave', 'saved'):                 # paths in JSON use forward slashes
        if isinstance(d.get(k), str):
            d[k] = d[k].replace('\\', '/')
    print(json.dumps({'ok': True, 'cmd': 'firmware', **d}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
