"""Annotation host-copy custody without ``fcntl`` (Windows): msvcrt byte-range lock, still fail closed. [daveey]"""
import os
import sys
import types

import pytest

from hermes_cli import kanban_annotation_disk as disk
from hermes_cli import kanban_annotation_storage as storage

SCOPE = ('win-host', 'default')


def _fake_msvcrt():
    """Models ``msvcrt.locking``: one exclusive byte-range lock per file, nonblocking attempt raises OSError."""
    held: dict[tuple[int, int], int] = {}
    calls: list[tuple[int, int, int]] = []
    mod = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0)

    def locking(fd, mode, nbytes):
        st = os.fstat(fd)
        key = (st.st_dev, st.st_ino)
        assert os.lseek(fd, 0, os.SEEK_CUR) == 0, 'lock must cover byte 0 for every holder'
        calls.append((fd, mode, nbytes))
        if mode == mod.LK_NBLCK:
            if key in held:
                raise OSError(36, 'Resource deadlock avoided')
            held[key] = fd
        elif mode == mod.LK_UNLCK:
            if held.get(key) != fd:
                raise OSError(13, 'Permission denied')
            del held[key]

    mod.locking = locking
    mod.held, mod.calls = held, calls
    return mod


@pytest.fixture
def windows(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'fcntl', None)  # ``import fcntl`` -> ImportError, as on Windows
    fake = _fake_msvcrt()
    monkeypatch.setitem(sys.modules, 'msvcrt', fake)
    monkeypatch.setattr(disk, 'FORCE', True)
    monkeypatch.setattr(disk, '_path', lambda scope: tmp_path / (scope[0] + '.json'))
    monkeypatch.setattr(disk.time, 'time', lambda: 1000.0)
    disk.reset()
    yield fake
    disk.reset()


def parts(value=1):
    return ({'session': value},) + tuple({} for _ in range(11))


def test_host_copy_round_trips_without_fcntl(windows):
    assert disk.save_host(SCOPE, 'input-1', 990, parts(), narrow_key='k1')
    disk.reset()  # another process knows only disk state
    header = {}
    assert disk.load_host(SCOPE, header_out=header) == ('input-1', 990, parts())
    assert header['parts_digest'] and header['verified_at'] == 990
    assert disk.confirm_host(SCOPE, 'k1', 995)
    header.clear()
    assert disk.load_host(SCOPE, shared=True, header_out=header) == ('input-1', 990, parts())
    assert header['verified_at'] == 995
    assert windows.calls and not windows.held  # locked and released every time


def test_contention_still_fails_closed_without_fcntl(windows, tmp_path):
    assert disk.save_host(SCOPE, 'input-1', 990, parts())
    with storage.custody(tmp_path / 'win-host.json', writer=True):
        assert disk.load_host(SCOPE) is None
        assert not disk.save_host(SCOPE, 'input-2', 999, parts(2))
    assert not windows.held
    disk.reset()
    assert disk.load_host(SCOPE) == ('input-1', 990, parts())


def test_no_lock_primitive_fails_closed(windows, monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, 'msvcrt', None)
    assert not disk.save_host(SCOPE, 'input-1', 990, parts())
    with pytest.raises(ImportError):
        with storage.custody(tmp_path / 'x.json', writer=False):
            pass
