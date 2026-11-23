"""Sidecar reclamation cannot wait for an amortized main-file sweep. [daveey]"""
import os

import pytest

from hermes_cli import sqlite_safe_read as reader


@pytest.mark.parametrize('suffix', ['-wal', '-shm'])
def test_unlinked_sidecar_is_reclaimed_on_next_unrelated_read(tmp_path, monkeypatch, suffix):
    monkeypatch.setattr(reader, '_PREAD_FDS', {})
    monkeypatch.setattr(reader, '_RETIRED_PREAD_FDS', [])
    monkeypatch.setattr(reader, '_pread_sweep_now', lambda: 100.0)
    monkeypatch.setattr(reader, '_PREAD_NEXT_SWEEP', 101.0)
    main = tmp_path / 'state.db'
    sidecar = tmp_path / ('state.db' + suffix)
    other = tmp_path / 'board.db'
    for path in (main, sidecar, other):
        path.write_bytes(b'header')
    try:
        # Prime the unrelated fd so its next read cannot reuse the reclaimed fd.
        assert reader.pread_lock_safe(other, 0, 6) == b'header'
        assert reader.pread_lock_safe(sidecar, 0, 6) == b'header'
        dead = reader._PREAD_FDS[str(sidecar)][0]
        sidecar.unlink()
        assert reader.pread_lock_safe(other, 0, 6) == b'header'
        # Writer-open guards scan every process: a deferred sidecar reader
        # looks like a stranded SQLite connection even though it holds no locks.
        with pytest.raises(OSError):
            os.fstat(dead)
        assert str(sidecar) not in reader._PREAD_FDS
    finally:
        for fd in {entry[0] for entry in reader._PREAD_FDS.values()} | set(reader._RETIRED_PREAD_FDS):
            os.close(fd)
