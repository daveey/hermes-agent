from __future__ import annotations

import sqlite3
import time

import pytest

from tui_gateway import server


@pytest.fixture(autouse=True)
def _no_digest_throttle(monkeypatch):
    monkeypatch.setattr(server, "SESSION_DIGEST_MIN_S", 0.0)


def _seed_store(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            last_activity_at REAL,
            message_count INTEGER,
            archived INTEGER DEFAULT 0,
            pinned INTEGER DEFAULT 0
        );
        CREATE TABLE gateway_heartbeats (
            backend_id TEXT PRIMARY KEY,
            last_heartbeat REAL
        );
        INSERT INTO sessions(id, title, last_activity_at, message_count)
        VALUES ('session-1', 'Original', 100, 1);
        INSERT INTO gateway_heartbeats(backend_id, last_heartbeat)
        VALUES ('backend-1', 100);
        """
    )
    conn.commit()
    conn.close()


def test_sessions_signature_ignores_gateway_heartbeat_only_writes(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _seed_store(db_path)
    monkeypatch.setattr(server, "_watcher_home", lambda: tmp_path)
    monkeypatch.setattr(server, "_served_profile_homes", [])

    before = server._sessions_sig()
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE gateway_heartbeats SET last_heartbeat = 200")
    conn.commit()
    conn.close()

    assert server._sessions_sig() == before


def test_sessions_signature_changes_for_session_metadata_updates(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _seed_store(db_path)
    monkeypatch.setattr(server, "_watcher_home", lambda: tmp_path)
    monkeypatch.setattr(server, "_served_profile_homes", [])

    before = server._sessions_sig()
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE sessions SET title = 'Renamed' WHERE id = 'session-1'")
    conn.commit()
    conn.close()

    assert server._sessions_sig() != before


def test_sessions_signature_sees_a_commit_inside_the_same_mtime_tick(tmp_path, monkeypatch):
    """Filesystem timestamps tick coarsely (~4 ms; whole seconds on some filesystems), and SQLite
    commits in place: a commit in the tick of the previous read leaves the DB/WAL mtime unchanged."""
    db_path = tmp_path / "state.db"
    _seed_store(db_path)
    monkeypatch.setattr(server, "_watcher_home", lambda: tmp_path)
    monkeypatch.setattr(server, "_served_profile_homes", [])
    frozen_mtime = time.time_ns()
    monkeypatch.setattr(server, "_newest_mtime_ns", lambda _paths: frozen_mtime)

    before = server._sessions_sig()
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE sessions SET title = 'Renamed' WHERE id = 'session-1'")
    conn.commit()
    conn.close()

    assert server._sessions_sig() != before


def test_busy_db_is_not_redigested_more_often_than_min_interval(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _seed_store(db_path)
    monkeypatch.setattr(server, "_watcher_home", lambda: tmp_path)
    monkeypatch.setattr(server, "_served_profile_homes", [])
    monkeypatch.setattr(server, "SESSION_DIGEST_MIN_S", 3.0)
    server._sessions_db_sig_cache.clear()
    server._sessions_digest_at.clear()
    clock = [1000.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    reads = []
    orig = server._session_digest
    monkeypatch.setattr(server, "_session_digest", lambda *a, **k: (reads.append(1), orig(*a, **k))[1])

    before = server._sessions_sig()
    assert len(reads) == 1
    for i in range(5):  # a busy db: the mtime moves on every pass
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE sessions SET title = ?", (f"T{i}",))
        conn.commit()
        conn.close()
        clock[0] += 0.5
        assert server._sessions_sig() == before
    assert len(reads) == 1  # no re-digest inside the window
    clock[0] += 3.0
    assert server._sessions_sig() != before  # trailing edge still fires
    assert len(reads) == 2
