"""Large-table read confirmations must not quarantine healthy writers on budget exhaustion."""
import sqlite3
import threading

import pytest

import hermes_state_corruption_confirm as cc
from hermes_state import SessionDB
from hermes_state_health import reset_storage_state, storage_state


@pytest.fixture
def db(tmp_path):
    reset_storage_state()
    handle = SessionDB(db_path=tmp_path / "state.db")
    handle.create_session("s1", source="cli")
    yield handle
    handle.close()
    reset_storage_state()


def corrupt_read(conn):
    conn.execute("SELECT id FROM sessions").fetchall()
    exc = sqlite3.DatabaseError("database disk image is malformed")
    exc.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
    exc.sqlite_errorname = "SQLITE_CORRUPT"
    raise exc


@pytest.mark.parametrize("outcome", ["timeout", "no_budget"])
def test_read_budget_exhaustion_does_not_latch_and_schedules_once(db, monkeypatch, outcome):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def confirm(path, tables, timeout_s):
        if timeout_s is not None:
            return cc.ConfirmResult(outcome, "sessions", 5.0)
        calls.append((path, tables, timeout_s))
        entered.set()
        assert release.wait(10)
        return cc.ConfirmResult("clean", "full", 6.0)

    monkeypatch.setattr(cc, "confirm_corruption", confirm)
    try:
        for _ in range(2):
            with pytest.raises(sqlite3.DatabaseError):
                db._read_retrying_ioerr(corrupt_read)
            assert storage_state(db.db_path) == "ok"
        assert entered.wait(5)
        assert calls == [(str(db.db_path), None, None)]
        db.append_message(session_id="s1", role="user", content="write remains allowed")
        assert db.get_messages("s1")[-1]["content"] == "write remains allowed"
    finally:
        release.set()
        thread = getattr(cc, "_background_thread", None)
        if thread is not None:
            thread.join(10)


@pytest.mark.parametrize("background", [False, True])
def test_confirmed_damage_latches(db, monkeypatch, background):
    def confirm(path, tables, timeout_s):
        outcome = "timeout" if background and timeout_s is not None else "damaged"
        return cc.ConfirmResult(outcome, "sessions", 0.0, "bad page")

    monkeypatch.setattr(cc, "confirm_corruption", confirm)
    with pytest.raises(sqlite3.DatabaseError):
        db._read_retrying_ioerr(corrupt_read)
    thread = getattr(cc, "_background_thread", None)
    if thread is not None:
        thread.join(10)
    assert storage_state(db.db_path) == "corrupt"
    with pytest.raises(sqlite3.DatabaseError):
        db.append_message(session_id="s1", role="user", content="must not land")


@pytest.mark.parametrize("router", ["sessions", "profiles"])
def test_web_outer_handlers_do_not_relatch_unconfirmed_read(db, monkeypatch, router):
    from fastapi import HTTPException
    from hermes_cli.web_routers import sessions, profiles

    monkeypatch.setattr(cc, "confirm_corruption", lambda *args: cc.ConfirmResult("timeout", "sessions", 5.0))
    monkeypatch.setattr(cc, "schedule_background_confirmation", lambda *args: True)
    with monkeypatch.context() as m:
        m.setattr(db, "close", lambda: None)
        if router == "sessions":
            m.setattr(sessions, "_open_session_db_for_profile", lambda *a, **k: db)
            m.setattr(sessions, "_session_db_path_for_profile", lambda *a: db.db_path)
            m.setattr(db, "list_sessions_rich", lambda **k: db._read_retrying_ioerr(corrupt_read))
            with pytest.raises(HTTPException) as caught:
                sessions.get_sessions(limit=20, offset=0)
            assert caught.value.status_code == 503
            assert "background integrity check" in caught.value.detail
        else:
            m.setattr(profiles, "_open_session_db_at_path", lambda *a, **k: db)
            errors = []
            assert profiles._read_profile_db("default", db.db_path.parent, errors,
                lambda handle: handle._read_retrying_ioerr(corrupt_read)) is None
            assert len(errors) == 1
        assert storage_state(db.db_path) == "ok"
        db.append_message(session_id="s1", role="user", content="router did not latch")


def test_distinct_profile_checks_queue_and_errors_do_not_latch(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    paths = [tmp_path / "a.db", tmp_path / "b.db"]
    seen = []

    def confirm(path, tables, timeout_s):
        seen.append(path)
        if path == str(paths[0]):
            entered.set()
            assert release.wait(10)
        return cc.ConfirmResult("error", "full", 0, "spawn failed")

    monkeypatch.setattr(cc, "confirm_corruption", confirm)
    try:
        assert cc.schedule_background_confirmation(paths[0], "first")
        assert entered.wait(5)
        thread = cc._background_thread
        assert cc.schedule_background_confirmation(paths[1], "second")
        assert not cc.schedule_background_confirmation(paths[1], "duplicate")
        assert cc.background_confirmation_pending(paths[1])
    finally:
        release.set()
        thread = cc._background_thread
        if thread is not None:
            thread.join(10)
    assert seen == [str(path) for path in paths]
    assert all(storage_state(path) == "ok" for path in paths)
