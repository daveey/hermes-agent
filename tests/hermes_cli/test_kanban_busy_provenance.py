"""Busy diagnostics distinguish native SQLite from a sampled lifecycle holder."""
import logging
import os
import sqlite3
import threading

import pytest

from hermes_cli import kanban_generation as generation
from hermes_cli import kanban_lockguard
from hermes_cli import kanban_session_activity_busy as busy


@pytest.mark.parametrize("close_path", [False, True])
def test_timeout_captures_holder_without_waiting_for_release(tmp_path, close_path, caplog):
    path = tmp_path / "board.db"
    entered, release = threading.Event(), threading.Event()
    owner = {}

    class Closing:
        def close(self):
            entered.set()
            assert release.wait(5)

    def holder():
        owner["thread"] = threading.get_ident()
        lock = generation.lifecycle_lock(path)
        if close_path:
            conn = kanban_lockguard.lifecycle_factory(Closing)()
            conn._hermes_lifecycle_lock = lock
            conn.close()
        else:
            with generation.lifecycle_access(path, 1):
                with lock:  # inner release must not clear the outer owner
                    pass
                entered.set()
                assert release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(generation.BoardLifecycleBusy) as caught:
            with generation.lifecycle_access(path, .01):
                pytest.fail("acquired held lock")
        diagnostic = caught.value.diagnostic
        assert diagnostic["pid"] == os.getpid()
        assert diagnostic["holder_thread"] == owner["thread"]
        assert diagnostic["held_ms"] >= 0
        assert diagnostic["wait_ms"] >= 0
        assert any("holder" in frame for frame in diagnostic["holder_stack"])
        if close_path:
            assert any("close" in frame for frame in diagnostic["holder_stack"])
        assert len(diagnostic["holder_stack"]) <= 16
        assert diagnostic["sampled_at"] > 0

        def fail(*args, **kwargs):
            raise caught.value

        busy.board_reads(path, (), ((), {}), False, False, fail)
        assert str(diagnostic) in caplog.records[-1].getMessage()
        busy.session_tasks(path, ["holder"], (), lambda *a: [])
        busy.session_tasks(path, ["holder"], (), fail)
        assert str(diagnostic) in caplog.records[-1].getMessage()
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    with generation.lifecycle_access(path, 0) as lock:
        assert lock.snapshot()["holder_thread"] == threading.get_ident()
    assert lock.snapshot() == {}


@pytest.mark.parametrize("entry", ["board_reads", "session_tasks"])
@pytest.mark.parametrize("lifecycle", [False, True])
def test_fallback_logs_typed_provenance(tmp_path, caplog, entry, lifecycle):
    busy.reset()
    path = tmp_path / "board.db"
    error = generation.BoardLifecycleBusy() if lifecycle else sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    error.sqlite_errorname = "SQLITE_BUSY"

    def fail(*args, **kwargs):
        raise error

    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_session_activity"):
        if entry == "board_reads":
            assert busy.board_reads(path, (), ((), {}), False, False, fail) == ({}, False, False)
        else:
            busy.session_tasks(path, ["id"], (), lambda *a: [{"id": "id"}])
            assert busy.session_tasks(path, ["id"], (), fail) == [{"id": "id", "stale": True}]
    message = caplog.records[-1].getMessage()
    assert "busy; reused last counts" in message
    assert f"pid={os.getpid()}" in message
    assert f"kind={type(error).__name__}" in message
    assert "sqlite_code=5" in message
    assert "holder=" in message


def test_rejected_release_keeps_owner_and_snapshot_drops_changed_owner(monkeypatch):
    from hermes_cli import kanban_lifecycle_diagnostics as diagnostics

    lock = diagnostics.LifecycleLock()
    with pytest.raises(RuntimeError):
        lock.release()
    with lock:
        original = lock._owner
        with monkeypatch.context() as patcher:
            patcher.setattr(diagnostics.threading, "get_ident", lambda: -1)
            with pytest.raises(RuntimeError):
                lock.release()
        assert lock._owner is original

        def changed_frames():
            lock._owner = (original[0], original[1])
            return {}

        monkeypatch.setattr(diagnostics.sys, "_current_frames", changed_frames)
        assert lock.snapshot() == {}
