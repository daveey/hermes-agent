"""A close error must not turn a cached probe fd number into a foreign close."""
import errno
import os

import pytest

import hermes_state_dbfile as dbfile
from hermes_cli import sqlite_safe_read as reader


@pytest.mark.parametrize('kind', ['header', 'pread'])
def test_failed_close_does_not_retry_on_reused_fd(tmp_path, monkeypatch, kind):
    target, foreign = tmp_path / 'probe.db', tmp_path / 'foreign.db'
    target.write_bytes(b'probe')
    foreign.write_bytes(b'foreign')
    fd, foreign_fd = os.open(target, os.O_RDONLY), os.open(foreign, os.O_RDONLY)
    stat = os.fstat(fd)
    entry = (fd, stat.st_dev, stat.st_ino)
    key = str(target)
    if kind == 'header':
        monkeypatch.setattr(dbfile, '_HEADER_PROBE_FDS', {key: entry})
        monkeypatch.setattr(dbfile, '_RETIRED_HEADER_PROBE_FDS', {})
        close = lambda: dbfile.close_header_probe_if_unchanged(key, entry)
    else:
        monkeypatch.setattr(reader, '_PREAD_FDS', {key: entry})
        monkeypatch.setattr(reader, '_RETIRED_PREAD_FDS', [])
        close = lambda: reader.release_profile_pread_fds(tmp_path)
    original_close = os.close
    failed = False
    def close_then_error(number):
        nonlocal failed
        if number == fd and not failed:
            failed = True
            original_close(fd)
            os.dup2(foreign_fd, fd)
            raise OSError(errno.EIO, 'controlled close-after-release error')
        original_close(number)
    monkeypatch.setattr(os, 'close', close_then_error)
    try:
        with pytest.raises(OSError, match='controlled close-after-release'):
            close()
        assert close() is True
        assert os.pread(fd, 7, 0) == b'foreign'
    finally:
        original_close(fd)
        original_close(foreign_fd)
