"""A busy board's first read must not serialize unrelated session browsing."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
import time

from hermes_cli import kanban_db_connect as kb
from hermes_cli import kanban_lockguard as guard
from hermes_cli.sqlite_safe_read import has_live_connection, offline_file_access, LiveConnectionError
from hermes_cli.web_server_sessions import _open_session_db_at_path
from hermes_state import SessionDB
import pytest


@pytest.mark.platforms("linux", "macos")
@pytest.mark.parametrize("http,discover_board", [(False, False), (True, False), (True, True)])
def test_busy_board_open_does_not_block_session_poll(tmp_path, monkeypatch, http, discover_board):
    state_path = tmp_path / "state.db"
    writer = SessionDB(db_path=state_path)
    writer.create_session("poll-target", source="cli")
    writer.close()
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hermes_cli import web_server_sessions
    from hermes_cli.web_routers import sessions

    monkeypatch.setattr(web_server_sessions, "_session_db_path_for_profile", lambda profile: state_path)
    app = FastAPI()
    app.include_router(sessions.manage_router)
    client = TestClient(app)
    assert client.get("/api/sessions/poll-target").status_code == 200
    board_path = tmp_path / "board.db"
    if discover_board:
        from hermes_cli import kanban_session_activity as activity
        activity._reset_cache_for_tests()
        monkeypatch.setattr(activity, "_board_db_paths", lambda: [board_path])
    blocker = sqlite3.connect(board_path)
    blocker.execute("PRAGMA journal_mode=DELETE")
    blocker.execute("CREATE TABLE marker(value)")
    blocker.commit()
    blocker.execute("BEGIN EXCLUSIVE")
    entered = threading.Event()
    original = guard._apply

    def observed_apply(conn, held_path=None, **kwargs):
        if getattr(conn, "_hermes_tracked_path", None) == str(board_path):
            # Every live attempt is probe-protected; busy attempts may close
            # fully between retries rather than retaining a blocked handle.
            assert has_live_connection(board_path)
            with pytest.raises(LiveConnectionError), offline_file_access(board_path):
                pass
            entered.set()
        return original(conn, held_path, **kwargs)

    monkeypatch.setattr(guard, "_apply", observed_apply)

    def open_board():
        conn = kb._sqlite_connect(board_path)
        try:
            assert conn.execute("SELECT count(*) FROM marker").fetchone()[0] == 0
        finally:
            conn.close()

    def poll():
        start = time.monotonic()
        if http:
            response = client.get("/api/sessions/poll-target")
            assert response.status_code == 200
            assert response.json()["id"] == "poll-target"
            if discover_board:
                assert response.json()["kanban_unknown"] is True
        else:
            db = _open_session_db_at_path(state_path, read_only=True)
            try:
                row = db.get_session("poll-target")
                assert row is not None and row["id"] == "poll-target"
            finally:
                db.close()
        return time.monotonic() - start

    with ThreadPoolExecutor(max_workers=5) as pool:
        opening = pool.submit(open_board)
        try:
            assert entered.wait(10), "board never entered its first read"
            elapsed = []
            for _ in range(8):
                polls = [pool.submit(poll) for _ in range(4)]
                # The blocker stays held until every unrelated request completes.
                elapsed.extend(future.result(timeout=4) for future in polls)
            assert max(elapsed) < 5
            assert not opening.done()
        finally:
            blocker.rollback()
            blocker.close()
        opening.result(timeout=10)
    if discover_board:
        # Annotation legitimately retains a tracked cached reader after its poll.
        # Release that custody before asserting that all board handles are gone.
        activity._reset_cache_for_tests()
    assert not has_live_connection(board_path)
    assert not has_live_connection(state_path)
    client.close()


def test_identity_setup_failure_closes_tracked_connection(tmp_path, monkeypatch):
    from hermes_cli import kanban_generation

    path = tmp_path / "board.db"
    seen = []

    def fail_after_open(conn, **kwargs):
        seen.append(conn)
        assert has_live_connection(path)
        raise RuntimeError("generation registration failed")

    monkeypatch.setattr(kanban_generation, "remember", fail_after_open)
    with pytest.raises(RuntimeError, match="generation registration failed"):
        kb._sqlite_connect(path)
    assert not has_live_connection(path)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        seen[0].execute("SELECT 1")
