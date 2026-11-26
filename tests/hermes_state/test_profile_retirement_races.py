"""Adversarial custody interleavings; actual SQLite handles, not fake acknowledgements."""
import os
import sqlite3
import threading

import pytest

from hermes_constants import mark_named_profile_deleted
from hermes_state import SessionDB
from tests.hermes_state import test_profile_retirement_custody as helpers
from tests.waiting import wait_until

profile_home = helpers.profile_home


class DelayedClose:
    def __init__(self, connection, allow):
        self.connection, self.allow = connection, allow
        self.attempted = threading.Event()

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def close(self):
        self.attempted.set()
        if not self.allow.is_set():
            raise sqlite3.OperationalError('controlled close failure')
        self.connection.close()


@pytest.mark.parametrize('initialization_failure', [False, True])
def test_failed_writer_close_is_retained_and_retried(profile_home, monkeypatch, initialization_failure):
    from hermes_profile_resources import wait_profile_resources
    allow = threading.Event()
    held = []
    original = SessionDB._open_writer

    def open_then_fail(self):
        original(self)
        self._conn = proxy = DelayedClose(self._conn, allow)
        held.append(proxy)
        if initialization_failure:
            raise RuntimeError('controlled schema setup failure')

    monkeypatch.setattr(SessionDB, '_open_writer', open_then_fail)
    db = None
    try:
        if initialization_failure:
            with pytest.raises(RuntimeError, match='controlled schema'):
                SessionDB(profile_home / 'state.db')
        else:
            db = SessionDB(profile_home / 'state.db')
        mark_named_profile_deleted(profile_home)
        assert held[0].attempted.wait(10)
        assert not wait_profile_resources(profile_home, timeout=0)
        assert held[0].connection.execute('SELECT 1').fetchone()[0] == 1
        allow.set()
        assert wait_profile_resources(profile_home, timeout=10)
        with pytest.raises(sqlite3.ProgrammingError):
            held[0].connection.execute('SELECT 1')
    finally:
        allow.set()
        if db is not None:
            db.close()


def test_failed_pooled_close_withholds_ack_until_retried(profile_home):
    from hermes_profile_resources import wait_profile_resources
    allow = threading.Event()
    db = SessionDB(profile_home / 'state.db')
    conn = db._get_read_conn()
    assert conn is not None
    proxy = DelayedClose(conn, allow)
    try:
        db._close_read_conn(proxy)
        mark_named_profile_deleted(profile_home)
        wait_until(lambda: db._conn is None, timeout=10)
        assert not wait_profile_resources(profile_home, timeout=0)
        allow.set()
        assert wait_profile_resources(profile_home, timeout=10)
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute('SELECT 1')
    finally:
        allow.set()
        db.close()


def test_shared_binding_finishes_before_retirement(profile_home, monkeypatch):
    import hermes_state_registry as registry
    from hermes_profile_resources import wait_profile_resources
    entered, proceed = threading.Event(), threading.Event()
    original = registry._stat_db_file_identity
    results = []

    def held_identity(path):
        if threading.current_thread().name == 'custody-opener':
            entered.set()
            assert proceed.wait(10)
        return original(path)

    monkeypatch.setattr(registry, '_stat_db_file_identity', held_identity)
    opener = threading.Thread(target=lambda: results.append(registry.acquire(profile_home / 'state.db')),
                              name='custody-opener')
    opener.start()
    try:
        assert entered.wait(10)
        mark_named_profile_deleted(profile_home)
        assert not wait_profile_resources(profile_home, timeout=0)
    finally:
        proceed.set()
        opener.join(10)
    assert not opener.is_alive()
    assert wait_profile_resources(profile_home, timeout=10)
    assert results and results[0]._conn is None
    assert registry.stats()['live_generations'] == 0


@pytest.mark.platforms('linux')
def test_ack_includes_retired_header_probe_inode(profile_home):
    import hermes_state_dbfile as dbfile
    from hermes_profile_resources import wait_profile_resources
    path = profile_home / 'state.db'
    with SessionDB(path):
        pass
    reader = SessionDB(path, read_only=True)
    dbfile._pread_db_range(path, 0, 100)
    previous = dbfile._HEADER_PROBE_FDS[str(path)]
    path.rename(path.with_suffix('.previous'))
    replacement = profile_home / 'replacement.db'
    with SessionDB(replacement):
        pass
    replacement.replace(path)
    dbfile._pread_db_range(path, 0, 100)
    assert previous[0] in dbfile._RETIRED_HEADER_PROBE_FDS
    mark_named_profile_deleted(profile_home)
    try:
        assert wait_profile_resources(profile_home, timeout=10)
        assert reader._conn is None
        assert not dbfile.all_header_probes_under(profile_home)
        targets = []
        for fd in os.listdir('/proc/self/fd'):
            try:
                targets.append(os.readlink('/proc/self/fd/' + fd))
            except FileNotFoundError:
                pass
        assert not [target for target in targets if str(profile_home) in target]
    finally:
        reader.close()
