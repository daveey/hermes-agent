"""Deterministic real opener/publication order, no sleeps or global process signals."""
import contextlib
import sqlite3
import threading
import zipfile

import pytest

from hermes_cli import backup_restore as restore
from hermes_cli import kanban_lockguard as guard
from hermes_cli import sqlite_safe_read as tracking


def test_missing_import_reservation_excludes_open_until_publication(tmp_path, monkeypatch):
    target, source = tmp_path / 'kanban.db', tmp_path / 'snapshot.db'
    with contextlib.closing(sqlite3.connect(source)) as conn:
        conn.execute('CREATE TABLE t(v)')
        conn.execute("INSERT INTO t VALUES ('snapshot')")
        conn.commit()
    archive = tmp_path / 'snapshot.zip'
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.write(source, 'kanban.db')
    real_extract = restore._extract_member_atomically
    observed = []

    def during_publication(*args, **kwargs):
        # Run a separate thread: same-thread reservations deliberately refuse too.
        def contender():
            try:
                with pytest.raises(sqlite3.OperationalError) as exc:
                    guard.connect(target, timeout=0)
                assert exc.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
                assert not target.exists()
                # A reservation of A must not pin B or the whole tracking registry.
                with contextlib.closing(guard.connect(tmp_path / 'unrelated.db', timeout=0)):
                    pass
                observed.append('excluded')
            except BaseException as exc:
                observed.append(exc)
        thread = threading.Thread(target=contender)
        thread.start()
        thread.join(5)
        assert not thread.is_alive()
        assert observed == ['excluded']
        return real_extract(*args, **kwargs)

    monkeypatch.setattr(restore, '_extract_member_atomically', during_publication)
    with zipfile.ZipFile(archive) as zf:
        restore._import_db_member(zf, 'kanban.db', target)
    with contextlib.closing(guard.connect(target, timeout=0)) as conn:
        assert conn.execute('SELECT v FROM t').fetchone() == ('snapshot',)


def test_open_registration_precedes_offline_refusal(tmp_path, monkeypatch):
    target = tmp_path / 'kanban.db'
    opened, finish, attempting, done = (threading.Event() for _ in range(4))
    real_connect = sqlite3.connect
    handles, results = [], []

    def paused_connect(database, **kwargs):
        conn = real_connect(database, **kwargs)
        opened.set()
        assert finish.wait(5)
        return conn

    monkeypatch.setattr(tracking.sqlite3, 'connect', paused_connect)

    def opener():
        try:
            handles.append(guard.connect(target, timeout=0, check_same_thread=False))
        except BaseException as exc:
            results.append(exc)

    def offline():
        attempting.set()
        try:
            with tracking.offline_file_access(target):
                results.append('unsafe publication')
        except tracking.LiveConnectionError:
            results.append('refused')
        finally:
            done.set()

    first = threading.Thread(target=opener)
    second = threading.Thread(target=offline)
    try:
        first.start()
        assert opened.wait(3)
        second.start()
        assert attempting.wait(3)
        assert not done.is_set()
    finally:
        finish.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
        for conn in handles:
            conn.close()
    assert not first.is_alive() and not second.is_alive()
    assert len(handles) == 1
    assert results == ['refused']
    assert not tracking.has_live_connection(target)


def test_guard_release_is_outside_global_tracking_mutex(tmp_path, monkeypatch):
    target = tmp_path / 'kanban.db'
    conn = guard.connect(target)
    original = guard._release
    observed = []

    def release(handle):
        # Cross-thread nonblocking probe is independent of private RLock ownership APIs.
        def probe():
            ok = tracking._live_lock.acquire(blocking=False)
            observed.append(ok)
            if ok:
                tracking._live_lock.release()
        thread = threading.Thread(target=probe)
        thread.start()
        thread.join(3)
        assert not thread.is_alive()
        original(handle)

    monkeypatch.setattr(guard, '_release', release)
    conn.close()
    assert observed == [True]
    assert not tracking.has_live_connection(target)
