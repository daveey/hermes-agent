"""Guard clocks follow event occurrence, not the local insertion order of synced history."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_dispatch_guard_notice as notice


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
                "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_KANBAN_GUARD_HOLD_NOTIFY_SECONDS", "1800")
    monkeypatch.setenv("HERMES_KANBAN_GUARD_HOLD_CAP_LONG_SECONDS", "3600")
    monkeypatch.setattr(kb.time, "time", lambda: 1_793_387_536)
    kb.init_db()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="isolated guard history", assignee="default")
        conn.execute("DELETE FROM task_events WHERE task_id=?", (tid,))
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        conn.commit()
        yield conn, tid


def event(conn, tid, kind, at, reason="owner_restart_cooldown"):
    conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                 (tid, kind, json.dumps({"reason": reason}), at))
    conn.commit()


@pytest.mark.parametrize("iso", [False, True])
@pytest.mark.parametrize("consumer", [notice.notify_guard_hold, dispatch._block_if_guard_hold_capped])
def test_imported_history_does_not_trigger_notice_or_cap(board, iso, consumer):
    conn, tid = board
    recent = 1_793_387_522
    stamp = datetime.fromtimestamp(recent, UTC).isoformat() if iso else recent
    event(conn, tid, "review_started", recent - 100)
    event(conn, tid, "owner_restart_lost", stamp)
    event(conn, tid, "respawn_guarded", recent)
    # Import assigns higher local IDs to history that happened before the reviewer ran.
    event(conn, tid, "claimed", 1_793_379_380)
    event(conn, tid, "respawn_guarded", 1_793_379_389, "active_pr")
    assert consumer(conn, tid, "owner_restart_cooldown") is False
    assert notice.guard_hold_started_at(conn, tid) == recent
    assert notice.guard_hold_reason(conn, tid) == "owner_restart_cooldown"
    task = kb.get_task(conn, tid)
    assert task is not None and task.status == "review"
    assert conn.execute("SELECT count(*) FROM task_comments WHERE task_id=?", (tid,)).fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["claimed", "review_requested", "review_started", "spawned", "owner_restart_lost"])
def test_lifecycle_resets_hold_even_when_imported_before_older_guard(board, kind):
    conn, tid = board
    recent = 1_793_387_522
    event(conn, tid, kind, recent)
    event(conn, tid, "respawn_guarded", recent - 8000)
    assert notice.guard_hold_started_at(conn, tid) is None
    assert notice.guard_hold_reason(conn, tid) is None
    assert notice.notify_guard_hold(conn, tid, "owner_restart_cooldown") is False
    assert dispatch._block_if_guard_hold_capped(conn, tid, "owner_restart_cooldown") is False
    # Same-second tie: a guard appended after the lifecycle starts a fresh hold.
    event(conn, tid, "respawn_guarded", recent)
    assert notice.guard_hold_started_at(conn, tid) == recent
    assert notice.guard_hold_reason(conn, tid) == "owner_restart_cooldown"


def test_owner_restart_still_holds_for_default_120_seconds(board, monkeypatch):
    conn, tid = board
    now = 1_793_387_536
    monkeypatch.delenv("HERMES_KANBAN_OWNER_RESTART_COOLDOWN_SECONDS", raising=False)
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    conn.commit()
    assert kb.claim_task(conn, tid)
    task = kb.get_task(conn, tid)
    assert task is not None
    conn.execute("UPDATE task_runs SET status='reclaimed', outcome='reclaimed', ended_at=?, metadata=? WHERE id=?",
                 (now, json.dumps({"owner_restart": True}), task.current_run_id))
    conn.execute("UPDATE tasks SET status='review', current_run_id=NULL WHERE id=?", (tid,))
    conn.commit()
    monkeypatch.setattr(kb.time, "time", lambda: now + 119)
    assert dispatch.check_respawn_guard(conn, tid) == "owner_restart_cooldown"
    monkeypatch.setattr(kb.time, "time", lambda: now + 120)
    assert dispatch.check_respawn_guard(conn, tid) is None


def test_old_iso_notice_does_not_suppress_a_new_hold(board):
    conn, tid = board
    now = 1_793_387_536
    kb.add_comment(conn, tid, "kanban-dispatcher", "[respawn-guard] an earlier hold")
    old = datetime.fromtimestamp(now - 8000, UTC).isoformat()
    conn.execute("UPDATE task_comments SET created_at=? WHERE task_id=?", (old, tid))
    conn.commit()
    event(conn, tid, "claimed", now - 7000)
    event(conn, tid, "respawn_guarded", now - 3600)
    assert notice.notify_guard_hold(conn, tid, "owner_restart_cooldown") is True
    assert notice.notify_guard_hold(conn, tid, "owner_restart_cooldown") is False


@pytest.mark.parametrize("author", ["kanban-dispatcher", "default", "David"])
def test_sustained_hold_notices_once_and_caps_despite_comments(board, monkeypatch, author):
    conn, tid = board
    now = 1_793_387_536
    start = now - 3600
    iso_start = datetime.fromtimestamp(start, UTC).isoformat()
    event(conn, tid, "respawn_guarded", iso_start)
    # A late imported claim must not erase a newer hold either.
    event(conn, tid, "claimed", start - 1)
    assert notice.notify_guard_hold(conn, tid, "owner_restart_cooldown") is True
    kb.add_comment(conn, tid, author, "still working")
    before = conn.execute("SELECT count(*) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]
    notice.record_guard_hold(conn, tid, "owner_restart_cooldown")
    assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0] == (
        before + (author != "kanban-dispatcher")
    )
    assert notice.guard_hold_started_at(conn, tid) == start
    assert notice.guard_hold_reason(conn, tid) == "owner_restart_cooldown"
    assert notice.notify_guard_hold(conn, tid, "owner_restart_cooldown") is False
    assert conn.execute("SELECT count(*) FROM task_comments WHERE task_id=? AND body LIKE '[respawn-guard]%'",
                        (tid,)).fetchone()[0] == 1
    assert dispatch._block_if_guard_hold_capped(conn, tid, "owner_restart_cooldown") is True
    task = kb.get_task(conn, tid)
    assert task is not None and task.status == "triage"
    assert dispatch._block_if_guard_hold_capped(conn, tid, "owner_restart_cooldown") is False
