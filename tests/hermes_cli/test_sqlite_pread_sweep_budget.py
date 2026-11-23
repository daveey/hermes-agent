"""Bound cleanup work across realistic session-capture header inventories. [daveey]"""
import os
from unittest.mock import patch

import pytest

from hermes_cli import sqlite_safe_read as reader


@pytest.fixture
def inventory(tmp_path, monkeypatch):
    monkeypatch.setattr(reader, '_PREAD_FDS', {})
    monkeypatch.setattr(reader, '_RETIRED_PREAD_FDS', [])
    clock = [100.0]
    monkeypatch.setattr(reader, '_pread_sweep_now', lambda: clock[0], raising=False)
    monkeypatch.setattr(reader, '_PREAD_NEXT_SWEEP', 0.0, raising=False)
    paths = []
    try:
        for index in range(147):
            path = tmp_path / f'{index}.db'
            path.write_bytes(b'SQLite format 3\0' + b'x' * 100)
            assert reader.pread_lock_safe(path, 0, 16) == b'SQLite format 3\0'
            paths.append(path)
        yield paths, clock
    finally:
        for fd in {entry[0] for entry in reader._PREAD_FDS.values()} | set(reader._RETIRED_PREAD_FDS):
            os.close(fd)


def test_capture_header_inventory_does_not_resweep_every_read(inventory):
    paths, clock = inventory
    with patch.object(reader, '_fd_is_unlinked', wraps=reader._fd_is_unlinked) as probe:
        for _ in range(10):
            for path in paths:
                assert reader.pread_lock_safe(path, 0, 16) == b'SQLite format 3\0'
        # The capture's 1,470 reads must not cause 216,090 cleanup fstats.
        assert probe.call_count == 0
        clock[0] = reader._PREAD_NEXT_SWEEP - .01
        assert reader.pread_lock_safe(paths[0], 0, 16)
        assert probe.call_count == 0
        clock[0] = reader._PREAD_NEXT_SWEEP
        assert reader.pread_lock_safe(paths[0], 0, 16)
        assert probe.call_count == len(paths)
        assert reader.pread_lock_safe(paths[1], 0, 16)
        assert probe.call_count == len(paths)


def test_explicit_sweep_remains_immediate_and_retains_linked_inodes(inventory):
    paths, _ = inventory
    linked = reader._PREAD_FDS[str(paths[0])][0]
    dead = reader._PREAD_FDS[str(paths[1])][0]
    paths[1].unlink()
    reader.release_unlinked_fds()
    assert str(paths[1]) not in reader._PREAD_FDS
    with pytest.raises(OSError):
        os.fstat(dead)
    assert os.fstat(linked).st_nlink == 1
    assert linked in reader.lock_safe_fds()


def test_inventory_sweep_retains_retired_linked_inode_until_unlinked(inventory):
    paths, _ = inventory
    old = reader._PREAD_FDS[str(paths[0])][0]
    alias = paths[0].with_suffix('.alias')
    paths[0].rename(alias)
    paths[0].write_bytes(b'new')
    assert reader.pread_lock_safe(paths[0], 0, 3) == b'new'
    assert old in reader.lock_safe_fds()
    assert os.fstat(old).st_nlink == 1
    alias.unlink()
    assert old not in reader.lock_safe_fds()
    with pytest.raises(OSError):
        os.fstat(old)


def test_scheduled_sweep_reclaims_replaced_unlinked_inode(inventory):
    paths, clock = inventory
    old = reader._PREAD_FDS[str(paths[0])][0]
    replacement = paths[0].with_suffix('.new')
    replacement.write_bytes(b'new contents')
    replacement.replace(paths[0])
    assert reader.pread_lock_safe(paths[0], 0, 3) == b'new'
    assert old in reader._RETIRED_PREAD_FDS
    clock[0] += 1
    assert reader.pread_lock_safe(paths[0], 0, 3) == b'new'
    assert old not in reader._RETIRED_PREAD_FDS
    with pytest.raises(OSError):
        os.fstat(old)
