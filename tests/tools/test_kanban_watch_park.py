"""A ``watch`` park: a later check that needs nothing from the chat does not hold it open. [daveey]

A chat created a card and parked it 69 h out ("check the DoorDash sign-in on Nov 5") while telling David
nothing was needed. Scheduled cards count as the chat's open work, so the chat stayed unfinished for days,
and ``kanban_complete`` on the parked card failed with a vague "stale run" (self-heal kanban-85e428ecb2).

``kanban_schedule(watch=true)`` parks the card as a watch: while it waits it is not the chat's open work
(counts 0, the close-out gate sees no task), and its notify subscription stays, so a later block or done
still reaches the chat. A long park without ``watch`` says which chat it holds and until when.
"""
from __future__ import annotations

import json
import time

import pytest

CHAT = "20261102_205456_6898ba"
HUMAN_REQUEST = {
    "action": "Sign in to the site again in Chrome. Unblock = re-check; archive = drop the check.",
    "why_human": "Only the account owner can complete the sign-in.",
    "interaction": "physical",
    "scope": "One sign-in",
    "evidence": "The renewed credential was rejected.",
}


@pytest.fixture
def board(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    for var in ("HERMES_SESSION_ID", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_KEY"):
        monkeypatch.delenv(var, raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return kb


def _chat_card(kb) -> str:
    """A card the chat created, with the chat's ``tui`` create subscription (what kanban_create writes)."""
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_notify as kbn
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="check sign-in after expiry", assignee="default", session_id=CHAT)
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=CHAT,
                           delivery_metadata={"reason": "create"})
    return tid


def _wake_iso(hours: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + hours * 3600))


def _schedule(tid: str, hours: float, ref: str = "credential expiry", **extra) -> dict:
    from tools import kanban_tools as kt
    return json.loads(kt._handle_schedule({
        "task_id": tid, "reason": f"SCHEDULED_UNTIL={_wake_iso(hours)} verify the credential renewed ({ref})",
        "clock_only": True, "awaiting": {"kind": "clock", "ref": ref, "why": f"the watch check due {ref}"}, **extra}))


def _open_for_chat(kb) -> tuple[int, list]:
    from hermes_cli import kanban_session_activity as ksa
    path = kb.kanban_db_path()
    counts = ksa._read_counts(path)
    tasks = ksa._read_session_tasks(path, [CHAT], ksa.ACTIVE_STATUSES)
    return counts.get(CHAT, 0), tasks


def test_watch_park_frees_the_chat_and_keeps_the_notice(board):
    tid = _chat_card(board)
    assert _open_for_chat(board)[0] == 1  # unparked: the chat's own work
    out = _schedule(tid, 72, watch=True)
    assert out.get("ok") is not False and not out.get("error"), out
    assert out["status"] == "scheduled"
    assert out.get("watch") is True
    assert "watch mark" in out.get("watch_note", ""), out
    n, tasks = _open_for_chat(board)
    assert n == 0, "a watch park must not hold the chat open"
    assert tasks == [], "the close-out gate (session_tasks ACTIVE_STATUSES) must see no open card"
    from hermes_cli import kanban_session_activity as ksa
    path = board.kanban_db_path()
    assert ksa._read_parked(path)[0].get(CHAT, 0) == 0
    # The subscription stays. When the watch wakes it is the chat's work again, and a block reaches the chat
    # as an unseen notice.
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_notify as kbn
    with kbc.connect() as conn:
        assert [s["chat_id"] for s in kbn.list_notify_subs(conn, tid)] == [CHAT]
        assert board.unblock_task(conn, tid)
    assert _open_for_chat(board)[0] == 1
    with kbc.connect() as conn:
        assert board.block_task(conn, tid, reason="renewal failed: re-sign in", kind="needs_input",
                                human_request=HUMAN_REQUEST) is not False
        assert board.get_task(conn, tid).status == "blocked"
    assert ksa._read_unseen(path).get(CHAT) == 1  # the block notice reaches the chat
    assert ksa._read_counts(path, ksa.BLOCKED_STATUSES).get(CHAT) == 1  # and the chat shows the ask


def test_watch_is_sticky_across_a_repark_without_it(board):
    tid = _chat_card(board)
    _schedule(tid, 72, ref="first expiry", watch=True)
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        assert board.unblock_task(conn, tid)
    out = _schedule(tid, 48, ref="second expiry")
    assert out.get("watch") is True, out
    assert _open_for_chat(board)[0] == 0
    with kbc.connect() as conn:
        assert board.unblock_task(conn, tid)
    out = _schedule(tid, 48, ref="third expiry", watch=False)
    assert out["status"] == "scheduled" and not out.get("watch"), out
    assert _open_for_chat(board)[0] == 1


def test_long_park_without_watch_names_the_chat_it_holds(board):
    tid = _chat_card(board)
    out = _schedule(tid, 72)
    assert out["status"] == "scheduled", out
    hint = out.get("chat_hold") or ""
    assert f"This keeps chat {CHAT} unfinished until" in hint, out
    assert "watch=true" in hint
    assert _open_for_chat(board)[0] == 1  # without watch the chat is still held (today's behaviour)


def test_short_park_has_no_hint(board):
    tid = _chat_card(board)
    out = _schedule(tid, 2)
    assert "chat_hold" not in out, out


def test_complete_on_scheduled_card_says_not_running(board):
    tid = _chat_card(board)
    _schedule(tid, 72)
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_complete({"task_id": tid, "summary": "verified later"}))
    assert out.get("error"), out
    assert out["rejection"] == "not_running"
    assert out["rejection"] in kt.COMPLETE_REJECTION_CLASSES
    assert "card is scheduled with no run" in out["error"]
    assert "archive it with a comment" in out["error"]
    assert "watch=true" in out["error"]


# [daveey] A watch park freed the chat, and its row then went plain green: "why is this session green" with the
# card's last check still pending (self-heal desktop-e6cce5965c). A done chat names its watches, the way it names
# the crons it owns, and the outcome audit lists it as ``done_watching``.
def _chat_outcome(tmp_path, refresh: bool = True):
    import hermes_state_outcome as outcomes
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / "state.db")
    if db._read_one("SELECT 1 FROM sessions WHERE id = ?", (CHAT,)) is None:
        db.create_session(CHAT, source="desktop")
        outcomes.declare_outcome(CHAT, {"kind": "done", "summary": "Fix landed; the live check runs Friday."}, db=db)
    out = outcomes.refresh_outcome(CHAT, "manual", db=db) if refresh else outcomes.get_outcome(CHAT, db=db)
    return db, out


def test_done_chat_names_its_watch_park(board, tmp_path):
    tid = _chat_card(board)
    _schedule(tid, 72, watch=True)
    db, out = _chat_outcome(tmp_path)
    try:
        assert out["kind"] == "done", out  # the watch does not hold the chat open (kanban-85e428ecb2)
        watches = out.get("watches") or []
        assert [w["id"] for w in watches] == [tid], out
        assert watches[0]["kind"] == "card" and watches[0]["state"] == "watching"
        assert watches[0]["label"] == "check sign-in after expiry"
        assert isinstance(watches[0].get("eta"), float) and watches[0]["eta"] > time.time() + 70 * 3600
        from hermes_cli.sessions_cmd_outcome import audit_rows
        row = next(r for r in audit_rows(db) if r["id"] == CHAT)
        assert row["reason"] == "done_watching", row
        assert [w["id"] for w in row["on"]] == [tid]
    finally:
        db.close()


def test_watch_mark_clears_once_the_card_is_done(board, tmp_path):
    tid = _chat_card(board)
    _schedule(tid, 72, watch=True)
    db, out = _chat_outcome(tmp_path)
    db.close()
    assert out.get("watches"), out
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        assert board.unblock_task(conn, tid)
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))
        conn.commit()
    db, out = _chat_outcome(tmp_path)
    try:
        assert out["kind"] == "done", out
        assert not out.get("watches"), out
        from hermes_cli.sessions_cmd_outcome import audit_rows
        row = next(r for r in audit_rows(db) if r["id"] == CHAT)
        assert row["reason"] != "done_watching", row
    finally:
        db.close()


def test_plain_scheduled_card_is_a_wait_not_a_watch(board, tmp_path):
    tid = _chat_card(board)
    _schedule(tid, 72)
    db, out = _chat_outcome(tmp_path)
    db.close()
    assert out["kind"] == "waiting", out
    assert not out.get("watches"), out


# [daveey] PR #2049 review nits (t_01a1d487e7253c82).
def test_audit_names_cron_and_watch_together(board, tmp_path, monkeypatch):
    """A done chat that owns a cron AND watches a card: the audit names both, like the tooltip does."""
    import hermes_state_outcome as outcomes
    tid = _chat_card(board)
    _schedule(tid, 72, watch=True)
    cron = {"kind": "cron", "id": "j1", "label": "sweep"}
    monkeypatch.setattr(outcomes, "_cron_items", lambda ids: [cron])
    db, _ = _chat_outcome(tmp_path)
    try:
        from hermes_cli.sessions_cmd_outcome import audit_rows
        row = next(r for r in audit_rows(db) if r["id"] == CHAT)
        assert row["reason"] == "done_owns_cron+done_watching", row
        assert [i["id"] for i in row["on"]] == ["j1", tid], row
    finally:
        db.close()


def test_declared_waiting_does_not_scan_boards_for_watches(board, tmp_path, monkeypatch):
    """A holding waiting declaration returns before ``named`` is used, so the boards are not scanned for it."""
    import hermes_state_outcome as outcomes
    from hermes_state import SessionDB
    calls = []
    monkeypatch.setattr(outcomes, "_watch_items", lambda ids: calls.append(ids) or [])
    monkeypatch.setattr(outcomes, "_cron_items", lambda ids: calls.append(ids) or [])
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(CHAT, source="desktop")
        outcomes.declare_outcome(CHAT, {"kind": "waiting", "on": [{"kind": "time", "id": "14:00"}]}, db=db)
        out = outcomes.refresh_outcome(CHAT, "manual", db=db)
        assert out["kind"] == "waiting", out
        assert calls == [], calls
    finally:
        db.close()


class _SubsQueryFails:
    """A board connection whose subscriber query fails with ``error`` (everything else runs for real)."""

    def __init__(self, conn, error):
        self._conn, self._error = conn, error

    def execute(self, sql, *args):
        if "kanban_notify_subs" in sql and "JOIN" in sql:
            raise self._error
        return self._conn.execute(sql, *args)


def test_locked_board_is_not_silently_read_as_no_subscriber_watches(board):
    """A busy/locked board must surface (the outcome guards and logs it), not read as "no watches"."""
    import sqlite3

    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_watch as kw
    with kbc.connect() as conn:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            kw._read_chat_watches(_SubsQueryFails(conn, sqlite3.OperationalError("database is locked")),
                                  [CHAT], "1=1")


def test_board_without_notify_subs_reads_created_watches(board):
    """A board that predates notify subscriptions still names the watches the chat created."""
    import sqlite3

    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_watch as kw
    tid = _chat_card(board)
    _schedule(tid, 72, watch=True)
    with kbc.connect() as conn:
        conn.execute("ALTER TABLE kanban_notify_subs RENAME TO legacy_subs_gone")
        rows = kw._read_chat_watches(_SubsQueryFails(conn, sqlite3.OperationalError("no such table")), [CHAT], "1=1")
    assert [r["id"] for r in rows] == [tid]
