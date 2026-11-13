"""Kept read-only ``state.db`` handle for background readers (``hermes_state_kept_reader``). [daveey]

Change-proportional design, card I: ``kanban_remote_notices._session_db`` opened a writable ``SessionDB`` per
link per pass, and each open ran schema reconcile (``_reconcile_columns``) and the writer preflight. A kept
read-only handle reconciles nothing and opens once per file identity.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

import hermes_state_kept_reader as kept
from hermes_state import SessionDB


@pytest.fixture
def state_db(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(path)
    try:
        db.create_session("s1", source="cli")
    finally:
        db.close()
    yield path
    kept.close_all_under(None)


@pytest.fixture
def reconciles(monkeypatch):
    """Every schema pass a ``SessionDB`` open makes (``_init_schema``: the full reconcile or its memo restore)."""
    calls: list = []
    cls = next(c for c in SessionDB.__mro__ if "_init_schema" in c.__dict__)
    real = cls._init_schema

    def counting(self):
        calls.append(self)
        return real(self)

    monkeypatch.setattr(cls, "_init_schema", counting)
    return calls


@pytest.fixture
def opens(monkeypatch):
    calls: list = []
    real = kept._open_session_db

    def counting(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(kept, "_open_session_db", counting)
    return calls


def test_kept_handle_is_reused_and_never_reconciles_schema(state_db, reconciles, opens):
    for _ in range(5):
        with kept.read_only(state_db) as db:
            assert db.read_only
            assert db.get_session("s1") is not None
    assert len(opens) == 1
    assert reconciles == []


def test_remote_notice_session_db_does_not_reconcile_per_pass(state_db, reconciles, monkeypatch):
    """RED on main: ``_session_db`` built ``SessionDB(path)`` (writable) per call, reconciling the schema every
    time a pass looked at a linked chat."""
    from hermes_cli import profiles
    from tui_gateway import kanban_remote_notices as notices

    monkeypatch.setattr(profiles, "get_profile_dir", lambda name: state_db.parent)
    for _ in range(3):
        with notices._session_db("default", "s1") as (db, tip):
            assert db is not None and tip == "s1"
    assert reconciles == []


def test_replaced_file_reopens_the_kept_handle(state_db, opens):
    with kept.read_only(state_db) as db:
        old = db
    other = state_db.with_name("other.db")
    fresh = SessionDB(other)
    try:
        fresh.create_session("s2", source="cli")
    finally:
        fresh.close()
    for suffix in ("-wal", "-shm"):
        for base in (state_db, other):
            try:
                os.unlink(str(base) + suffix)
            except FileNotFoundError:
                pass
    os.replace(other, state_db)
    with kept.read_only(state_db) as db:
        assert db is not old
        assert db.get_session("s2") is not None and db.get_session("s1") is None
    assert len(opens) == 2
    assert old._conn is None  # the stale handle was closed, not left on the old inode


def test_closed_handle_is_replaced(state_db, opens):
    with kept.read_only(state_db) as db:
        db.close()
    with kept.read_only(state_db) as db:
        assert db._conn is not None
    assert len(opens) == 2


def test_database_error_in_body_retires_the_handle(state_db, opens):
    with pytest.raises(sqlite3.OperationalError):
        with kept.read_only(state_db) as db:
            first = db
            db._conn.execute("SELECT * FROM no_such_table")
    with kept.read_only(state_db) as db:
        assert db is not first
    assert first._conn is None


def test_retire_while_borrowed_closes_at_give_back(state_db):
    with kept.read_only(state_db) as db:
        assert kept.close_all_under(state_db.parent) == 1
        assert db.get_session("s1") is not None  # still usable by its borrower
    assert db._conn is None


def test_registry_close_all_under_retires_kept_handles(state_db):
    import hermes_state_registry

    with kept.read_only(state_db) as db:
        pass
    hermes_state_registry.close_all_under(state_db.parent)
    assert db._conn is None


def test_notice_write_goes_through_the_shared_writer(state_db, monkeypatch):
    from tui_gateway.kanban_notice_session_store import notice_writer, session_store

    with session_store(state_db, "s1") as (db, tip):
        assert tip == "s1"
        with pytest.raises(Exception):
            db.append_kanban_notice(tip, "x", {"notice_id": "b:t:0"})  # the kept handle is read-only
        with notice_writer(db) as wdb:
            assert not wdb.read_only
            row = wdb.append_kanban_notice(tip, "hello", {"notice_id": "b:t:1"})
        assert row
    with kept.read_only(state_db) as db:
        texts = [m.get("content") for m in db.get_messages("s1")]
    assert "hello" in " ".join(str(t) for t in texts)
