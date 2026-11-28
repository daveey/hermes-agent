"""Request reads borrow an idle read-only ``SessionDB`` instead of opening one per call. [daveey]

Hub serve AnyIO residual (t_01a21ab4eb964866): every ``GET /api/sessions/{id}/messages`` and each ``after_id``
delta ran ``_with_db(read_only=True)``, which built a fresh ``SessionDB(read_only=True)``; its first statement
parses the whole schema (49 triggers, ~40 KB of trigger SQL, 1.3-1.4 ms on the hub) before the read itself.
A lent handle is reused only while it reads what a fresh open would: these tests pin both halves.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

import pytest

import hermes_state_kept_reader as kept
from hermes_cli.web_routers import sessions as routes
from hermes_state import SessionDB


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    monkeypatch.setattr(routes, "_session_db_path_for_profile", lambda profile: path)
    from hermes_cli import web_server_sessions
    monkeypatch.setattr(web_server_sessions, "_session_db_path_for_profile", lambda profile: path)
    writer = SessionDB(path)
    writer.create_session("s1", source="cli")
    writer.append_message("s1", role="user", content="one")
    yield path, writer
    writer.close()
    kept.close_all_under(None)


@pytest.fixture
def ro_opens(monkeypatch):
    """Every ``SessionDB(read_only=True)`` construction."""
    calls: list = []
    real = SessionDB.__init__

    def counting(self, db_path=None, read_only=False):
        if read_only:
            calls.append(db_path)
        return real(self, db_path=db_path, read_only=read_only)

    monkeypatch.setattr(SessionDB, "__init__", counting)
    return calls


def _messages(db):
    return [m["content"] for m in db.get_messages("s1")]


def test_repeated_reads_open_one_handle(store, ro_opens):
    """RED on main: five reads constructed five read-only SessionDBs (one schema parse each)."""
    for _ in range(5):
        assert routes._with_db(None, _messages, read_only=True) == ["one"]
    assert len(ro_opens) == 1


def test_commit_between_reads_is_visible_on_the_reused_handle(store, ro_opens):
    """Notification loss: a commit made after a read must be seen by the very next read, with no reopen."""
    _path, writer = store
    seen: list = []
    assert routes._with_db(None, lambda db: (seen.append(db), _messages(db))[1], read_only=True) == ["one"]
    writer.append_message("s1", role="assistant", content="two")
    assert routes._with_db(None, lambda db: (seen.append(db), _messages(db))[1], read_only=True) == ["one", "two"]
    assert seen[0] is seen[1]
    assert len(ro_opens) == 1


def test_replaced_store_reopens(store, ro_opens, tmp_path):
    path, writer = store
    routes._with_db(None, _messages, read_only=True)
    writer.close()
    other = tmp_path / "other.db"
    fresh = SessionDB(other)
    fresh.create_session("s1", source="cli")
    fresh.append_message("s1", role="user", content="replaced")
    fresh.close()
    for base in (path, other):
        for suffix in ("-wal", "-shm"):
            try:
                os.unlink(str(base) + suffix)
            except FileNotFoundError:
                pass
    os.replace(other, path)
    assert routes._with_db(None, _messages, read_only=True) == ["replaced"]
    assert len(ro_opens) == 2


def test_schema_change_reopens(store, ro_opens):
    """A DDL bumps ``schema_version``: the open-time FTS flags and the declared-schema probe no longer prove
    anything, so the next read gets a fresh, re-probed handle."""
    path, _writer = store
    first: list = []
    routes._with_db(None, lambda db: first.append(db), read_only=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE lend_probe_marker (x INTEGER)")
    second: list = []
    routes._with_db(None, lambda db: second.append(db), read_only=True)
    assert second[0] is not first[0]
    assert first[0]._conn is None  # the stale handle was closed, not pooled
    assert len(ro_opens) == 2


def test_concurrent_borrowers_get_their_own_handles(store, ro_opens):
    """Exclusive lending: a long read never makes another request wait on its connection."""
    inside = threading.Event()
    release = threading.Event()
    held: list = []

    def slow(db):
        held.append(db)
        inside.set()
        release.wait(10)
        return _messages(db)

    t = threading.Thread(target=routes._with_db, args=(None, slow), kwargs={"read_only": True})
    t.start()
    try:
        assert inside.wait(10)
        other: list = []
        assert routes._with_db(None, lambda db: (other.append(db), _messages(db))[1], read_only=True) == ["one"]
        assert other[0] is not held[0]
    finally:
        release.set()
        t.join(10)
    assert len(ro_opens) == 2
    # Both went back idle; later reads reuse them rather than opening a third.
    routes._with_db(None, _messages, read_only=True)
    assert len(ro_opens) == 2


def test_database_error_discards_the_handle(store, ro_opens):
    held: list = []

    def boom(db):
        held.append(db)
        raise sqlite3.DatabaseError("database disk image is malformed")

    with pytest.raises(sqlite3.DatabaseError):
        routes._with_db(None, boom, read_only=True)
    assert held[0]._conn is None
    routes._with_db(None, _messages, read_only=True)
    assert len(ro_opens) == 2


def test_open_transaction_is_never_lent_again(store, ro_opens):
    """A body that left a read transaction open would pin an old snapshot: that handle is closed, not pooled."""
    held: list = []

    def leave_open(db):
        held.append(db)
        db._conn.execute("BEGIN")
        db._conn.execute("SELECT 1").fetchone()

    routes._with_db(None, leave_open, read_only=True)
    assert held[0]._conn is None
    routes._with_db(None, _messages, read_only=True)
    assert len(ro_opens) == 2


def test_fake_store_is_closed_after_use(monkeypatch, tmp_path):
    """A non-SessionDB opener result (test doubles, legacy fakes) is never pooled: closed after the body."""
    closed: list = []

    class Fake:
        def close(self):
            closed.append(True)

    monkeypatch.setattr(routes, "_session_db_path_for_profile", lambda profile: tmp_path / "x.db")
    monkeypatch.setattr(routes, "_open_session_db_for_profile", lambda *a, **kw: Fake())
    assert routes._with_db(None, lambda db: "ok", read_only=True) == "ok"
    assert routes._with_db(None, lambda db: "ok", read_only=True) == "ok"
    assert closed == [True, True]


@pytest.fixture
def profile_home(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    root.mkdir()
    (root / "config.yaml").write_text("{}")
    home = root / "profiles" / "example"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("{}")
    monkeypatch.setenv("HERMES_HOME", str(root))
    yield home
    kept.close_all_under(None)


def test_idle_lent_handle_does_not_block_profile_delete(profile_home):
    """An idle pooled handle holds its profile lease like any open SessionDB; the retirement monitor must
    close it so the delete's drain completes, and the next borrow must not hand out the retired handle."""
    from hermes_constants import mark_named_profile_deleted
    from hermes_profile_resources import wait_profile_resources

    path = profile_home / "state.db"
    with SessionDB(path) as writer:
        writer.create_session("s1", source="cli")
    lent: list = []
    with kept.lend(path, lambda: SessionDB(path, read_only=True)) as db:
        lent.append(db)
        assert db.get_session("s1") is not None
    assert kept._idle.get(kept._key(path)), "the handle went back idle"
    mark_named_profile_deleted(profile_home)
    assert wait_profile_resources(profile_home, timeout=10)
    assert lent[0]._conn is None


def test_close_all_under_retires_idle_and_borrowed(store, ro_opens):
    path, _writer = store
    routes._with_db(None, _messages, read_only=True)
    idle_db = kept._idle[kept._key(path)][0].db
    held: list = []

    def during(db):
        held.append(db)
        kept.close_all_under(Path(path).parent)
        return _messages(db)  # a borrowed handle stays usable to its borrower

    assert idle_db._conn is not None
    assert routes._with_db(None, during, read_only=True) == ["one"]
    assert held[0]._conn is None  # closed at give-back, never pooled
    routes._with_db(None, _messages, read_only=True)
    assert len(ro_opens) == 2
