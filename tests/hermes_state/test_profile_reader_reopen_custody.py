"""Pooled readers keep their opening lease across a writer close/reopen."""
import sqlite3
import threading

import pytest

from hermes_constants import mark_named_profile_deleted
from hermes_profile_resources import wait_profile_resources
from hermes_state import SessionDB
from tests.hermes_state import test_profile_retirement_custody as helpers
from tests.hermes_state.test_profile_retirement_races import DelayedClose
from tests.waiting import wait_until

profile_home = helpers.profile_home


@pytest.mark.parametrize('failed_close', [False, True])
def test_checked_out_reader_return_after_live_reopen(profile_home, monkeypatch, failed_close):
    allow = threading.Event()
    db = SessionDB(profile_home / 'state.db')
    original = db._connect_read_only

    def open_reader(**kwargs):
        conn = original(**kwargs)
        return DelayedClose(conn, allow) if failed_close else conn

    monkeypatch.setattr(db, '_connect_read_only', open_reader)
    try:
        with db._read_ctx() as conn:
            assert conn is not db._conn
            old = db._profile_lease
            db.close()
            db.create_session('reopened', source='cli', model='test')
            new = db._profile_lease
            assert old is not None and new is not None and new is not old
            assert conn.execute('SELECT 1').fetchone()[0] == 1
        # The replacement writer must retain its own reference, even while the
        # old reader's concrete close is pending on the original lease.
        assert new._refs == 1
        if failed_close:
            assert conn.attempted.is_set()
            assert old._refs == 1
            assert conn.execute('SELECT 1').fetchone()[0] == 1
            assert not old.handle.closed
            allow.set()
        wait_until(lambda: old._refs == 0, timeout=10)
        assert new._refs == 1
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute('SELECT 1')
        db.close()
        assert new._refs == 0
        mark_named_profile_deleted(profile_home)
        assert wait_profile_resources(profile_home, timeout=10)
    finally:
        allow.set()
        db.close()
