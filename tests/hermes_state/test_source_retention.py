"""``sessions.source_retention_days``: an automation source's own retention window, applied even
while ``sessions.auto_prune`` is off, so Kanban/cron transcripts stop growing ``state.db`` without
deleting any chat history."""

import time
from pathlib import Path

import pytest

from hermes_state import SessionDB

DAY = 86400.0


@pytest.fixture
def db(tmp_path):
    store = SessionDB(db_path=tmp_path / "state.db")
    yield store
    store.close()


def _session(db: SessionDB, sid: str, source: str, *, idle_days: float, ended: bool = True) -> None:
    db.create_session(sid, source=source)
    db.append_message(sid, role="user", content="hello")
    if ended:
        db.end_session(sid, "done")
    ts = time.time() - idle_days * DAY
    db._conn.execute("UPDATE sessions SET started_at=?, last_activity_at=? WHERE id=?", (ts, ts, sid))
    db._conn.execute("UPDATE messages SET timestamp=? WHERE session_id=?", (ts, sid))
    db._conn.commit()


def test_prunes_only_the_listed_source_past_its_window(db):
    _session(db, "old-kanban", "kanban", idle_days=20)
    _session(db, "new-kanban", "kanban", idle_days=3)
    _session(db, "old-desktop", "desktop", idle_days=200)
    _session(db, "old-cron", "cron", idle_days=200)
    _session(db, "pinned-kanban", "kanban", idle_days=20)
    db.set_session_pinned("pinned-kanban", True)
    _session(db, "archived-kanban", "kanban", idle_days=20)  # archive hides a row, it is not a keep flag
    db.set_session_archived("archived-kanban", True, notify=False)

    result = db.maybe_prune_source_retention({"kanban": 14}, min_interval_hours=0)

    assert result["pruned"] == {"kanban": 2}
    assert db.get_session("old-kanban") is None
    assert db.get_session("archived-kanban") is None
    for kept in ("new-kanban", "old-desktop", "old-cron", "pinned-kanban"):
        assert db.get_session(kept) is not None, kept


def test_stale_open_row_is_closed_first_and_kept_for_a_full_window(db):
    _session(db, "dead-worker", "kanban", idle_days=20, ended=False)

    first = db.maybe_prune_source_retention({"kanban": 14}, min_interval_hours=0)

    assert first["closed"] == {"kanban": 1}
    row = db.get_session("dead-worker")
    assert row is not None and row["end_reason"] == "startup_orphan_reap"
    assert db.maybe_prune_source_retention({"kanban": 14}, min_interval_hours=0)["pruned"] == {"kanban": 0}


@pytest.mark.parametrize("entries", [
    {"desktop": 14},  # a chat source never gets its own window
    {"cli": 14},  # the classic CLI's chats share this source with one-shot runs
    {"kanban": 0},
    {"kanban": -5},
    {"kanban": "14"},
    {"kanban": True},
    {"kanban": 1.5},
    ["kanban"],
])
def test_invalid_entries_delete_nothing(db, entries, caplog):
    _session(db, "old-kanban", "kanban", idle_days=400)
    _session(db, "old-desktop", "desktop", idle_days=400)

    result = db.maybe_prune_source_retention(entries, min_interval_hours=0)

    assert result["skipped"] is True
    assert db.get_session("old-kanban") is not None
    assert db.get_session("old-desktop") is not None
    assert "source_retention_days" in caplog.text


def test_runs_at_most_once_per_interval(db):
    assert db.maybe_prune_source_retention({"kanban": 14}, min_interval_hours=24)["skipped"] is False
    _session(db, "old-kanban", "kanban", idle_days=20)

    assert db.maybe_prune_source_retention({"kanban": 14}, min_interval_hours=24)["skipped"] is True
    assert db.get_session("old-kanban") is not None


def _write_config(home: Path, body: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(body, encoding="utf-8")


_CONFIG = ("sessions:\n"
           "  auto_prune: false\n"
           "  min_interval_hours: 0\n"
           "  source_retention_days:\n"
           "    kanban: 14\n")


def test_cli_startup_applies_source_retention_with_auto_prune_off(tmp_path, monkeypatch):
    """E2E through the CLI start path and the real config loader."""
    import cli

    home = tmp_path / "hermes"
    _write_config(home, _CONFIG)
    monkeypatch.setenv("HERMES_HOME", str(home))
    store = SessionDB(db_path=home / "state.db")
    try:
        _session(store, "old-kanban", "kanban", idle_days=20)
        _session(store, "old-desktop", "desktop", idle_days=400)

        cli._run_state_db_auto_maintenance(store)

        assert store.get_session("old-kanban") is None
        assert store.get_session("old-desktop") is not None
    finally:
        store.close()


def test_cli_startup_runs_both_windows_when_auto_prune_is_on(tmp_path, monkeypatch):
    """The per-source pass and the chat-wide auto-prune share the maintenance lock in one call."""
    import cli

    home = tmp_path / "hermes"
    _write_config(home, _CONFIG.replace("auto_prune: false", "auto_prune: true\n  retention_days: 90\n"
                                        "  vacuum_after_prune: false"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    store = SessionDB(db_path=home / "state.db")
    try:
        _session(store, "old-kanban", "kanban", idle_days=20)
        _session(store, "old-desktop", "desktop", idle_days=400)
        _session(store, "new-desktop", "desktop", idle_days=20)

        cli._run_state_db_auto_maintenance(store)

        assert store.get_session("old-kanban") is None
        assert store.get_session("old-desktop") is None
        assert store.get_session("new-desktop") is not None
    finally:
        store.close()


def test_bad_interval_is_not_read_while_every_feature_is_off(tmp_path, monkeypatch):
    from hermes_cli.cli_auto_maintenance import run_sessions_retention
    from unittest.mock import MagicMock

    store = MagicMock()
    run_sessions_retention(store, {"min_interval_hours": "daily"}, tmp_path)

    store.maybe_prune_source_retention.assert_not_called()
    store.maybe_auto_prune_and_vacuum.assert_not_called()
