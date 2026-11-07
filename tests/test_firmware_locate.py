"""Finding DSWave 1.2.1 for the FPGA bitstream (no hardware, no real DSWave, no real registry).

A synthetic DSWave.exe carries a small DSLZ container; the module's hash constants are patched to match it. The
Windows registry is a fake winreg module, the data folder and the environment are temporary.
"""
import hashlib
import json
import os
import struct
import sys
import types

import pytest

from openm454 import firmware as fw
from openm454.errors import DscopeError

U = fw.UNINSTALL_KEY


class FakeKey:
    def __init__(self, subkeys=None, values=None):
        self.subkeys, self.values = subkeys or {}, values or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeWinreg:
    """Just what a read-only uninstall scan needs; there is nothing here that could write."""
    HKEY_LOCAL_MACHINE, HKEY_CURRENT_USER = 'HKLM', 'HKCU'
    KEY_READ, KEY_WOW64_64KEY, KEY_WOW64_32KEY = 0x20019, 0x0100, 0x0200

    def __init__(self, tree):
        self.tree, self.access = tree, []          # tree: {(hive, view): {subkey: {value: str}}}

    def OpenKey(self, key, sub, reserved=0, access=0x20019):
        self.access.append(access)
        if isinstance(key, str):
            entries = self.tree.get((key, access & 0x0300))
            if entries is None or sub != U:
                raise FileNotFoundError(sub)
            return FakeKey(subkeys=entries)
        return FakeKey(values=key.subkeys[sub])

    def EnumKey(self, key, i):
        names = list(key.subkeys)
        if i >= len(names):
            raise OSError('no more data')
        return names[i]

    def QueryValueEx(self, key, name):
        if name not in key.values:
            raise FileNotFoundError(name)
        return key.values[name], 1


def lz4_literals(raw):
    """An LZ4 block holding raw as one literal run (valid input for lz4.block and the built-in decoder)."""
    if len(raw) < 15:
        return bytes([len(raw) << 4]) + raw
    n = len(raw) - 15
    return bytes([0xF0]) + b'\xff' * (n // 255) + bytes([n % 255]) + raw


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Temporary data folder, no DSWave variables, no Program Files variables, an empty fake registry and a
    synthetic DSWave 1.2.1: env.make(folder) writes DSWave.exe there (good=False: another version)."""
    root = tmp_path / 'home'
    monkeypatch.setattr(fw, 'ROOT', str(root))
    monkeypatch.setattr(fw, 'CACHE', str(root / 'firmware' / 'm454_fpga_3100.bin'))
    monkeypatch.setattr(fw, 'SAVED', str(root / 'state' / 'dswave.json'))
    for v in ('OPENM454_DSWAVE', 'DSCOPE_DSWAVE', 'ProgramW6432', 'ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
        monkeypatch.delenv(v, raising=False)
    reg = FakeWinreg({})
    monkeypatch.setitem(sys.modules, 'winreg', reg)

    raw = bytes(range(256)) * 9 + b'end'
    blob = b'DSLZ' + struct.pack('>I', len(raw)) + hashlib.sha256(raw).digest() + lz4_literals(raw)
    exe = b'MZ' + b'\0' * 62 + blob + b'rest of the program'
    monkeypatch.setattr(fw, 'DSLZ_OFFSET', 64)
    monkeypatch.setattr(fw, 'DSLZ_LEN', len(blob))
    monkeypatch.setattr(fw, 'RAW_SIZE', len(raw))
    monkeypatch.setattr(fw, 'RAW_SHA256', hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(fw, 'DSWAVE_EXE_SHA256', hashlib.sha256(exe).hexdigest().upper())

    def make(folder, good=True):
        os.makedirs(str(folder), exist_ok=True)
        p = os.path.join(str(folder), 'DSWave.exe')
        with open(p, 'wb') as f:
            f.write(exe if good else exe + b'1.3.0')
        return p

    return types.SimpleNamespace(root=root, tmp=tmp_path, reg=reg, raw=raw, make=make, mp=monkeypatch)


def same(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def entry(name, version='', loc='', icon='', uninst=''):
    return {'DisplayName': name, 'DisplayVersion': version, 'InstallLocation': loc, 'DisplayIcon': icon,
            'UninstallString': uninst}


# ------------------------------------------------------------------------------------------- search order
def test_search_order_env_saved_copy_registry_folders(env):
    t = env.tmp
    env.mp.setenv('OPENM454_DSWAVE', str(t / 'envdir'))
    os.makedirs(os.path.dirname(fw.SAVED))
    with open(fw.SAVED, 'w') as f:
        json.dump({'dswave': str(t / 'saveddir' / 'DSWave.exe')}, f)
    env.reg.tree[('HKLM', 0x100)] = {'DSWave': entry('DSWave 1.2.1', '1.2.1', loc=str(t / 'regdir'))}
    env.mp.setenv('ProgramFiles', str(t / 'pf'))
    env.mp.setenv('LOCALAPPDATA', str(t / 'la'))
    c = fw.candidates()
    assert [s for s, _ in c] == ['env', 'saved', 'copy', 'registry', 'folder', 'folder', 'folder', 'folder']
    want = [t / 'envdir' / 'DSWave.exe', t / 'saveddir' / 'DSWave.exe', env.root / 'DSWave' / 'DSWave.exe',
            t / 'regdir' / 'DSWave.exe', t / 'pf' / 'DSWave' / 'DSWave.exe',
            t / 'pf' / 'DreamSourceLab' / 'DSWave' / 'DSWave.exe', t / 'la' / 'Programs' / 'DSWave' / 'DSWave.exe',
            t / 'la' / 'Programs' / 'DreamSourceLab' / 'DSWave' / 'DSWave.exe']
    assert all(same(p, w) for (_, p), w in zip(c, want))
    assert fw.candidate_exes() == [p for _, p in c]


def test_env_takes_the_folder_or_the_exe_quoted_or_not(env):
    d = env.tmp / 'My DSWave'
    exe = env.make(d)
    for v in (str(d), '"%s"' % d, exe, '"%s"' % exe, str(d) + os.sep):
        env.mp.setenv('OPENM454_DSWAVE', v)
        src, p = fw.candidates()[0]
        assert src == 'env' and same(p, exe)
    env.mp.delenv('OPENM454_DSWAVE')
    env.mp.setenv('DSCOPE_DSWAVE', str(d))                   # the old name still works
    assert fw.locate() == ('env', os.path.join(str(d), 'DSWave.exe'))


def test_duplicates_are_listed_once(env):
    d = env.root / 'DSWave'
    env.mp.setenv('OPENM454_DSWAVE', str(d))
    env.reg.tree[('HKLM', 0x100)] = {'DSWave': entry('DSWave 1.2.1', loc=str(d))}
    assert [s for s, _ in fw.candidates()] == ['env']


def test_nothing_set_names_only_the_copy(env):
    assert [s for s, _ in fw.candidates()] == ['copy']
    assert fw.locate() == (None, None)


# ------------------------------------------------------------------------------------------- registry
def test_registry_scan_is_read_only_and_finds_dswave_in_both_views(env):
    t = env.tmp
    new, old = t / 'Oscilloscope' / 'DSWave', t / 'old' / 'DSWave'
    env.reg.tree[('HKLM', 0x100)] = {
        'Other': entry('Some Tool', loc=str(t / 'other')),
        'DSView': entry('DSView 1.3.0', '1.3.0', icon=str(t / 'DSView' / 'DSView.exe')),
        'DSWave': entry('DSWave 1.2.1', '1.2.1', icon=str(new / 'DSWave.exe'),
                        uninst='"%s" /AllUsers' % (new / 'uninst.exe')),
    }
    env.reg.tree[('HKLM', 0x200)] = {'{GUID}': entry('dswave 1.1.0', '1.1.0', loc=str(old),
                                                     uninst='MsiExec.exe /X{GUID}')}
    env.reg.tree[('HKCU', 0x100)] = {'DSWave': entry('DSWave 1.2.1', '1.2.1', loc=str(new) + os.sep)}
    dirs = fw.registry_dirs()
    assert same(dirs[0], new) and any(same(d, old) for d in dirs)
    assert not any('DSView' in d or 'other' in d for d in dirs)
    assert same(dirs[-1], old)                               # the 1.2.1 entries come first
    r = env.reg
    assert r.access and all(a & ~(r.KEY_READ | r.KEY_WOW64_64KEY | r.KEY_WOW64_32KEY) == 0 for a in r.access)
    assert all(a & r.KEY_READ == r.KEY_READ for a in r.access)
    assert {a & 0x300 for a in r.access} == {0x100, 0x200}   # both views of both hives were asked
    regs = [p for s, p in fw.candidates() if s == 'registry']
    assert len(regs) == 2 and same(regs[0], new / 'DSWave.exe')


def test_no_windows_registry_means_no_registry_candidates(env):
    env.mp.setitem(sys.modules, 'winreg', None)              # import winreg fails, as on Linux
    assert fw.registry_dirs() == [] and fw._dswave_entries() == []


def test_program_folder_of_registry_command_lines(tmp_path):
    d = tmp_path / 'Program Files' / 'DSWave'
    assert same(fw._exe_dir('"%s" /AllUsers' % (d / 'uninst.exe')), d)
    assert same(fw._exe_dir('%s,0' % (d / 'DSWave.exe')), d)
    assert same(fw._exe_dir('%s /S' % (d / 'uninst.exe')), d)
    assert fw._exe_dir('MsiExec.exe /X{0000}') == '' and fw._exe_dir('') == '' and fw._exe_dir(None) == ''


def test_common_folders(env):
    assert fw.common_dirs() == []
    env.mp.setenv('ProgramFiles', str(env.tmp / 'pf'))
    env.mp.setenv('ProgramFiles(x86)', str(env.tmp / 'pf86'))
    env.mp.setenv('ProgramW6432', str(env.tmp / 'pf'))
    want = [env.tmp / 'pf' / 'DSWave', env.tmp / 'pf' / 'DreamSourceLab' / 'DSWave',
            env.tmp / 'pf86' / 'DSWave', env.tmp / 'pf86' / 'DreamSourceLab' / 'DSWave']
    got = fw.common_dirs()
    assert len(got) == 4 and all(same(g, w) for g, w in zip(got, want))


# ------------------------------------------------------------------------------------------- bitstream
def test_found_through_the_registry_extracted_once_then_cached(env):
    d = env.tmp / 'Oscilloscope' / 'DSWave'
    exe = env.make(d)
    env.reg.tree[('HKLM', 0x100)] = {'DSWave': entry('DSWave 1.2.1', '1.2.1', loc=str(d))}
    before = open(exe, 'rb').read(), os.path.getmtime(exe)
    st = fw.status()
    assert st['cached'] is False and st['ready'] and st['source'] == 'registry' and same(st['dswave'], exe)
    assert fw.bitstream() == env.raw
    assert (open(exe, 'rb').read(), os.path.getmtime(exe)) == before   # DSWave is only read
    assert open(fw.CACHE, 'rb').read() == env.raw
    os.remove(exe)                                               # DSWave is not needed any more
    env.reg.tree.clear()
    assert fw.bitstream() == env.raw
    st = fw.status()
    assert st['cached'] and st['ready'] and st['dswave'] is None and 'hint' not in st


def test_builtin_lz4_decoder_too(env):
    env.make(env.root / 'DSWave')
    env.mp.setitem(sys.modules, 'lz4', None)
    env.mp.setitem(sys.modules, 'lz4.block', None)
    assert fw.bitstream() == env.raw


def test_wrong_version_is_skipped_and_reported(env):
    env.mp.setenv('OPENM454_DSWAVE', str(env.tmp / 'v13'))
    env.make(env.tmp / 'v13', good=False)
    good = env.make(env.tmp / 'pf' / 'DreamSourceLab' / 'DSWave')
    env.mp.setenv('ProgramFiles', str(env.tmp / 'pf'))
    assert fw.locate()[0] == 'env' and fw.locate(verify=True) == ('folder', good)
    assert fw.status(verify=True)['source'] == 'folder'
    assert fw.bitstream() == env.raw
    os.remove(fw.CACHE)
    os.remove(good)
    with pytest.raises(DscopeError) as ei:
        fw.bitstream()
    assert 'not DSWave 1.2.1' in ei.value.message and 'v13' in ei.value.message


def test_corrupt_cache_is_replaced(env):
    os.makedirs(os.path.dirname(fw.CACHE))
    with open(fw.CACHE, 'wb') as f:
        f.write(b'half a bitstream')
    assert fw.cached() is None and fw.status()['cached'] is False
    env.make(env.root / 'DSWave')
    assert fw.bitstream() == env.raw and fw.cached() == env.raw
    assert not [n for n in os.listdir(os.path.dirname(fw.CACHE)) if n.endswith('.tmp')]


def test_not_found_says_what_to_do_including_for_the_mcp_server(env):
    env.mp.setenv('OPENM454_DSWAVE', str(env.tmp / 'typo'))
    with pytest.raises(DscopeError) as ei:
        fw.bitstream()
    e = ei.value
    assert e.code == 'FIRMWARE_SOURCE' and e.exit_code == 6
    assert 'DSWave 1.2.1 not found' in e.message and 'OPENM454_DSWAVE=%s' % (env.tmp / 'typo') in e.message
    h = e.hint
    assert 'claude mcp add -s user openm454 -e "OPENM454_DSWAVE=<folder>" -- openm454-mcp' in h
    assert "'--'" in h and 'restart Claude' in h and 'background' in h
    assert fw.SAVE_CMD in h and fw.SAVED in h and os.path.dirname(fw.CACHE) in h
    st = fw.status()
    assert st['ready'] is False and st['hint'] == h


def test_missing_saved_path_is_reported(env):
    os.makedirs(os.path.dirname(fw.SAVED))
    with open(fw.SAVED, 'w') as f:
        json.dump({'dswave': str(env.tmp / 'gone' / 'DSWave.exe')}, f)
    with pytest.raises(DscopeError) as ei:
        fw.bitstream()
    assert 'saved path' in ei.value.message and 'no longer exists' in ei.value.message


# ------------------------------------------------------------------------------------------- saved path
def test_save_dswave_checks_caches_and_is_found_later(env):
    d = env.tmp / 'Some Folder' / 'DSWave'
    exe = env.make(d)
    st = fw.save_dswave(str(d))
    assert st['cached'] and st['ready'] and st['source'] == 'saved' and same(st['saved'], exe)
    with open(fw.SAVED, encoding='utf-8') as f:
        assert same(json.load(f)['dswave'], exe)
    assert fw.load_saved() == st['saved'] and fw.cached() == env.raw
    os.remove(fw.CACHE)
    assert fw.bitstream() == env.raw                            # extracted again from the saved path


def test_save_dswave_refuses_a_missing_or_wrong_file(env):
    with pytest.raises(DscopeError) as ei:
        fw.save_dswave(str(env.tmp / 'nothing'))
    assert ei.value.code == 'INVALID_ARG'
    bad = env.make(env.tmp / 'v13', good=False)
    with pytest.raises(DscopeError) as ei:
        fw.save_dswave(bad)
    assert ei.value.code == 'FIRMWARE_SOURCE' and 'not DSWave 1.2.1' in ei.value.message
    assert not os.path.exists(fw.SAVED) and fw.cached() is None
    st = fw.save_dswave(bad, check=False)                        # recorded as asked, without the check
    assert st['source'] == 'saved' and not st['cached']


def test_saved_file_tolerates_bom_and_garbage(env):
    os.makedirs(os.path.dirname(fw.SAVED))
    with open(fw.SAVED, 'w', encoding='utf-8-sig') as f:      # PowerShell's Out-File writes a BOM
        json.dump({'dswave': '/opt/dswave/DSWave'}, f)
    assert fw.load_saved() == '/opt/dswave/DSWave'
    for junk in ('{', '[]', '{"dswave": 3}', '{"dswave": " "}'):
        with open(fw.SAVED, 'w') as f:
            f.write(junk)
        assert fw.load_saved() is None


# ------------------------------------------------------------------------------- python -m openm454.firmware
def test_module_command_saves_and_reports(env, capsys):
    d = env.tmp / 'DSWave'
    env.make(d)
    assert fw.main([str(d)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['ok'] and out['cmd'] == 'firmware' and out['cached'] and out['source'] == 'saved'
    assert '\\' not in out['cache'] + out['dswave'] + out['saved']
    assert fw.main([]) == 0 and json.loads(capsys.readouterr().out)['cached']


def test_module_command_without_dswave_explains(env, capsys):
    assert fw.main([]) == 6
    out = json.loads(capsys.readouterr().out)
    assert out['ok'] is False and out['error']['code'] == 'FIRMWARE_SOURCE' and 'claude mcp add' in out['error']['hint']
    assert fw.main(['--help']) == 0 and 'FOLDER' in capsys.readouterr().out
