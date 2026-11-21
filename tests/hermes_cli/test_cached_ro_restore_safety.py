"""Real cached-reader custody at the public restore fallback boundary."""
import contextlib
import os
import shutil
import sqlite3
import zipfile

import pytest

from hermes_cli import backup_restore as restore
from hermes_cli.kanban_session_activity import cached_ro, evict_readers
from hermes_cli.sqlite_safe_read import pread_lock_safe, release_unlinked_fds

pytestmark = pytest.mark.platforms('linux')  # Real pread and deleted-inode descriptor proofs.


def make_db(path, marker):
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE t(v TEXT)')
        conn.execute('INSERT INTO t VALUES (?)', (marker,))
        conn.commit()


def file_state(path):
    result = {}
    for suffix in ('', '-wal', '-shm', '-journal'):
        file = path.with_name(path.name + suffix)
        if file.exists():
            st = file.stat()
            result[suffix] = (st.st_dev, st.st_ino, st.st_size,
                              pread_lock_safe(file, 0, st.st_size))
    return result


def deleted_fds(path):
    # Drop ONLY the measurement's own unlinked lock-safe pread descriptors.
    # This never closes any SQLite connection; real stranded readers remain visible.
    release_unlinked_fds()
    if not os.path.isdir('/proc/self/fd'):
        return []
    found = []
    for name in os.listdir('/proc/self/fd'):
        try:
            target = os.readlink('/proc/self/fd/' + name)
        except OSError:
            continue
        if target.startswith(str(path)) and target.endswith(' (deleted)'):
            found.append(target)
    return found


def fail_source_backup(monkeypatch, source, at_fault=None):
    real_connect = sqlite3.connect
    calls = []

    class FailedSource(sqlite3.Connection):
        def backup(self, *args, **kwargs):
            calls.append('source backup')
            if at_fault is not None:
                at_fault()
            raise sqlite3.OperationalError('fixture: source backup only')

    def connect(database, *args, **kwargs):
        if str(database) == restore.read_only_db_uri(source):
            kwargs['factory'] = FailedSource
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(restore.sqlite3, 'connect', connect)
    return calls


def test_fallback_refuses_real_idle_cached_ro(monkeypatch, tmp_path):
    source, target = tmp_path / 'snapshot.db', tmp_path / 'kanban.db'
    make_db(source, 'snapshot')
    make_db(target, 'current')
    at_fault = []
    calls = fail_source_backup(monkeypatch, source, lambda: at_fault.append(file_state(target)))
    try:
        with cached_ro(target) as held:
            assert held.execute('SELECT v FROM t').fetchone() == ('current',)
        # Leaving cached_ro returns the borrower, NOT the connection custody.
        before = file_state(target)
        ok = restore._safe_restore_db(source, target)
        after = file_state(target)
        assert calls == ['source backup']
        assert ok is False, (before, after, deleted_fds(target))
        # The pre-backup live-safe TRUNCATE checkpoint refreshes SHM salts.
        # The failing backup/fallback must leave EVERY byte/identity unchanged.
        assert after == at_fault[0]
        assert after[''] == before['']
        assert after['-wal'] == before['-wal']
        assert after['-shm'][:3] == before['-shm'][:3]
        assert all(i < 136 for i, (a, b) in enumerate(zip(before['-shm'][3], after['-shm'][3])) if a != b)
        assert deleted_fds(target) == []
        assert held.execute('SELECT v FROM t').fetchone() == ('current',)
        with cached_ro(target) as again:
            assert again is held
    finally:
        evict_readers(str(target.resolve()))


@pytest.mark.parametrize('custody', ['borrowed', 'transaction', 'cache-lock', 'writer'])
def test_fallback_preserves_active_holders(monkeypatch, tmp_path, custody):
    from hermes_cli import kanban_session_activity as activity
    from hermes_cli.kanban_dispatch_owner import connect_with_identity
    from hermes_cli.sqlite_safe_read import has_live_connection

    source, target = tmp_path / 'snapshot.db', tmp_path / 'kanban.db'
    make_db(source, 'snapshot')
    make_db(target, 'current')
    at_fault = []
    calls = fail_source_backup(monkeypatch, source, lambda: at_fault.append(file_state(target)))
    try:
        with contextlib.ExitStack() as stack:
            if custody == 'writer':
                held = stack.enter_context(contextlib.closing(connect_with_identity(target, timeout=0)))
            else:
                held = stack.enter_context(cached_ro(target))
            assert held.execute('SELECT v FROM t').fetchone() == ('current',)
            if custody == 'transaction':
                held.execute('BEGIN')
                held.execute('SELECT v FROM t').fetchall()
                stack.callback(held.rollback)
            if custody == 'cache-lock':
                stack.enter_context(activity._RO_CACHE_LOCK)
            assert has_live_connection(target)
            assert restore._safe_restore_db(source, target) is False
            assert calls == ['source backup']
            assert file_state(target) == at_fault[0]
            assert deleted_fds(target) == []
            assert held.execute('SELECT v FROM t').fetchone() == ('current',)
    finally:
        evict_readers(str(target.resolve()))
    assert not has_live_connection(target)
    # No custody remains: fallback is still available, not permanently poisoned.
    assert restore._safe_restore_db(source, target) is True
    with contextlib.closing(sqlite3.connect(target)) as new:
        assert new.execute('SELECT v FROM t').fetchone() == ('snapshot',)


def test_normal_live_backup_converges_same_cached_reader(tmp_path):
    source, target = tmp_path / 'snapshot.db', tmp_path / 'kanban.db'
    make_db(source, 'snapshot')
    make_db(target, 'current')
    try:
        with cached_ro(target) as held:
            assert held.execute('SELECT v FROM t').fetchone() == ('current',)
        inode = target.stat().st_ino
        assert restore._safe_restore_db(source, target) is True
        assert target.stat().st_ino == inode
        assert held.execute('SELECT v FROM t').fetchone() == ('snapshot',)
        with cached_ro(target) as again:
            assert again is held
        assert deleted_fds(target) == []
    finally:
        evict_readers(str(target.resolve()))


def test_missing_import_refuses_kept_deleted_reader_then_succeeds(tmp_path):
    source, target = tmp_path / 'snapshot.db', tmp_path / 'kanban.db'
    make_db(source, 'snapshot')
    make_db(target, 'current')
    archive = tmp_path / 'snapshot.zip'
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.write(source, 'kanban.db')
    try:
        with cached_ro(target) as held:
            assert held.execute('SELECT v FROM t').fetchone() == ('current',)
        target.unlink()  # Exact missing-target window; only fixture-owned board.
        before = file_state(target)
        with zipfile.ZipFile(archive) as zf:
            with pytest.raises(OSError, match='connection.*still open'):
                restore._import_db_member(zf, 'kanban.db', target)
        assert not target.exists()
        assert file_state(target) == before
        assert held.execute('SELECT v FROM t').fetchone() == ('current',)
    finally:
        evict_readers(str(target.resolve()))
    with zipfile.ZipFile(archive) as zf:
        restore._import_db_member(zf, 'kanban.db', target)
    assert not target.with_name(target.name + '-wal').exists()
    assert not target.with_name(target.name + '-shm').exists()
    try:
        with cached_ro(target) as new:
            assert new is not held
            assert new.execute('SELECT v FROM t').fetchone() == ('snapshot',)
            assert deleted_fds(target) == []
    finally:
        evict_readers(str(target.resolve()))


def test_missing_import_discards_nonempty_stale_wal(tmp_path):
    source, target = tmp_path / 'snapshot.db', tmp_path / 'kanban.db'
    make_db(source, 'snapshot')
    make_db(target, 'current')
    archive = tmp_path / 'snapshot.zip'
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.write(source, 'kanban.db')
    wal = target.with_name(target.name + '-wal')
    saved = tmp_path / 'saved-wal'
    with contextlib.closing(sqlite3.connect(target)) as writer:
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute("INSERT INTO t VALUES ('stale-wal-row')")
        writer.commit()
        # Preserve a real committed WAL only in this disposable fixture.
        shutil.copy2(wal, saved)
    assert saved.stat().st_size > 32
    target.unlink()
    shutil.copy2(saved, wal)
    target.with_name(target.name + '-journal').write_bytes(b'stale journal')
    with zipfile.ZipFile(archive) as zf:
        restore._import_db_member(zf, 'kanban.db', target)
    assert not wal.exists()
    assert not target.with_name(target.name + '-journal').exists()
    with contextlib.closing(sqlite3.connect(target)) as reader:
        assert reader.execute('SELECT v FROM t').fetchall() == [('snapshot',)]
        assert reader.execute('PRAGMA integrity_check').fetchone() == ('ok',)

