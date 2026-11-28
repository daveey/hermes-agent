"""Executed-byte and non-reusable home-incarnation contract controls."""
import errno

import importlib.util
import importlib.machinery
import os
import py_compile


import pytest

from hermes_cli import serve_producer_execution as execution
from hermes_cli import serve_producer_home as homes
from hermes_cli.serve_producer_manifest import verify_loaded
from hermes_cli.serve_producer_protocol import ProtocolError
from tests.hermes_cli.test_serve_producer_shared_broker import manifest


def load(path, name='exec_control'):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_exec_receipt_and_context_cleanup(tmp_path):
    path = tmp_path / 'provider.py'
    path.write_text('VALUE = "fresh"\n')
    declared = manifest(path)
    with execution.authenticated_execution(declared, tmp_path, entry={}) as executed:
        module = load(path)
        assert module.VALUE == 'fresh'
        verify_loaded(declared, tmp_path, modules={'m': module}, executed=executed)
        with pytest.raises(ProtocolError, match='executable bytes unverified'):
            verify_loaded(declared, tmp_path, modules={'m': module}, executed={str(path): 'wrong'})
    # The observer is inert after scope exit, including failed executions.
    path.write_text('VALUE = "after"\n')
    exec(compile(path.read_bytes(), str(path), 'exec'), {})
    assert not execution._guards


@pytest.mark.parametrize('mutation', ['undeclared', 'changed', 'nested-code'])
def test_late_exec_is_checked_before_effects(tmp_path, mutation):
    path = tmp_path / 'provider.py'
    path.write_text('VALUE = "fresh"\ndef f():\n    return "original"\n')
    declared = manifest(path)
    with execution.authenticated_execution(declared, tmp_path, entry={}):
        load(path)
        if mutation == 'undeclared':
            path = tmp_path / 'extra.py'
            source = 'VALUE = "bad"\n'
        elif mutation == 'nested-code':
            source = 'VALUE = "fresh"\ndef f():\n    return "tampered"\n'
        else:
            source = 'VALUE = "wrong"\ndef f():\n    return "original"\n'
        namespace = {}
        with pytest.raises(ProtocolError):
            exec(compile(source, str(path), 'exec'), namespace)
        assert 'VALUE' not in namespace
        # Unrelated platform/library code is outside the provider closure.
        exec(compile('VALUE = 7', str(tmp_path.parent / 'outside.py'), 'exec'), namespace)
        assert namespace['VALUE'] == 7


def test_snapshot_refuses_entry_disagreement_and_source_reread(tmp_path, monkeypatch):
    path = tmp_path / 'provider.py'
    path.write_text('VALUE = 1\n')
    declared = manifest(path)
    with pytest.raises(ProtocolError, match='entry manifest digest mismatch'):
        with execution.authenticated_execution(declared, tmp_path, entry={path.name: '0' * 64}):
            pytest.fail('accepted different entry bytes')
    monkeypatch.setattr(execution, '_read', lambda p: b'VALUE = 2\n')
    with pytest.raises(ProtocolError, match='manifest digest mismatch'):
        with execution.authenticated_execution(declared, tmp_path, entry={}):
            pytest.fail('accepted a changed snapshot')


def test_late_sourceless_loader_cannot_hide_behind_foreign_filename(tmp_path):
    path = tmp_path / 'provider.py'
    path.write_text('VALUE = "old"\n')
    cached = tmp_path / 'hidden.pyc'
    py_compile.compile(str(path), cfile=str(cached), dfile='/former-install/provider.py', doraise=True)
    path.write_text('VALUE = "new"\n')
    with execution.authenticated_execution(manifest(path), tmp_path, entry={}):
        load(path)  # legitimate bootstrap completes first
        loader = importlib.machinery.SourcelessFileLoader('late_stale', str(cached))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        with pytest.raises(ProtocolError):
            loader.exec_module(module)
        assert not hasattr(module, 'VALUE')


def test_snapshot_does_not_follow_declared_members_outside_root(tmp_path):
    base = tmp_path / 'base'
    base.mkdir()
    outside = tmp_path / 'outside.py'
    outside.write_text('VALUE = 1\n')
    path = base / 'provider.py'
    path.symlink_to(outside)
    with pytest.raises(ProtocolError, match='outside'):
        with execution.authenticated_execution(manifest(path), base, entry={}):
            pytest.fail('authenticated source escaped closure')


@pytest.mark.platforms('linux')
def test_home_incarnation_persists_and_changes_on_real_inode_reuse(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    before = homes.lifecycle_generation(home)[1]
    identity = home.stat().st_ino
    # Keep no old inode alive: real local filesystems can recycle it immediately.
    reused = False
    for _ in range(512):
        home.rmdir()
        home.mkdir()
        after = homes.lifecycle_generation(home)[1]
        assert after != before
        assert homes.lifecycle_generation(home)[1] == after
        if home.stat().st_ino == identity:
            reused = True
            break
    # Inode allocation is filesystem-specific; controlled identity reuse has its own mandatory test.
    print(f'real incarnation recreation iterations: {_ + 1}; reused original inode: {reused}')


def test_home_incarnation_race_adopts_winner(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    original = os.setxattr
    winner = b'a' * 64

    def race(fd, name, value, flags):
        original(fd, name, winner, flags)
        raise OSError(errno.EEXIST, 'another binder installed incarnation')

    monkeypatch.setattr(homes.os, 'setxattr', race)
    first = homes.lifecycle_generation(home)
    assert os.getxattr(home, homes._INCARNATION) == winner
    assert homes.lifecycle_generation(home) == first


@pytest.mark.parametrize('kind', ['unsupported', 'missing-read', 'malformed', 'replaced'])
def test_unverifiable_home_incarnation_fails_closed(tmp_path, monkeypatch, kind):
    home = tmp_path / 'home'
    home.mkdir()
    if kind == 'malformed':
        os.setxattr(home, homes._INCARNATION, b'not-a-generation')
    elif kind == 'replaced':
        original = os.fstat

        def stale(fd):
            info = list(original(fd))
            info[1] += 1
            return os.stat_result(info)

        monkeypatch.setattr(homes.os, 'fstat', stale)
    else:
        error = errno.EOPNOTSUPP if kind == 'unsupported' else errno.EACCES

        def fail(*args, **kwargs):
            raise OSError(error, 'cannot establish incarnation')

        monkeypatch.setattr(homes.os, 'getxattr', fail)
    with pytest.raises(homes.HomeRevoked):
        homes.lifecycle_generation(home)
