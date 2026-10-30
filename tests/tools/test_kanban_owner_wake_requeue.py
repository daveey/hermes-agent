"""An owner-wake supervisor card may requeue exactly the cards its body lists. [daveey]

On 10-28..10-30 four hub cards sat in triage for days. The chat-closeout owner
wake routes such orphaned cards to a board-supervisor card ("Owner wake:
supervise ...", created_by='owner-wake'), but that card's worker could only
comment: kanban_unblock is orchestrator-only, and only a self-heal fix card had
a requeue grant. Now the supervisor card may unblock the cards its body lists
(the ``- <board>/<id>`` card lines on this board), only while each is in triage
or blocked. Every other worker and target stays refused.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def board(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    for key in (
        "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_HOME",
        "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_WORKER_TASK",
    ):
        monkeypatch.delenv(key, raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _card(conn, kb, title, status):
    tid = kb.create_task(conn, title=title, assignee="default")
    conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
    conn.commit()
    return tid


def _line(key, title="T", gateway=None):
    where = f" on {gateway}" if gateway else ""
    return (f"- {key}{where} \"{title}\": waiting on an agent to clear a transient block since "
            "2026-10-28 08:38 UTC")


def _supervisor(conn, kb, lines, *, title=None, created_by="owner-wake"):
    """The card chat-closeout's ``supervise_on_board`` creates (its body shape)."""
    first = lines[0].split()[1] if lines else "x"
    body = ("[owner-wake] These card(s) parked for an agent-owned reason and their owning chat "
            "cron_x is an ended cron session (cron_complete), so nobody would wake for them:\n"
            + "\n".join(lines)
            + "\n\nSupervise each: read the card ... - default/t_ffffffffffffffff is only context "
              "here. Complete this card with what you changed.")
    return kb.create_task(
        conn, title=title or f"Owner wake: supervise {first} (owning chat an ended cron session)",
        body=body, assignee="default", created_by=created_by,
        idempotency_key=f"owner-wake:cron_x:{len(lines)}{title or ''}{created_by}",
    )


def _as_worker(monkeypatch, conn, kb, tid):
    kb.claim_task(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(kb._current_run_id(conn, tid)))


def _unblock(target):
    from tools import kanban_tools as kt
    return json.loads(kt._handle_unblock({"task_id": target}))


def _status(tid):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        return kb.get_task(conn, tid).status
    finally:
        conn.close()


@pytest.mark.parametrize("status", ["triage", "blocked"])
def test_supervisor_requeues_a_listed_triage_or_blocked_card(board, monkeypatch, status):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    conn = kbc.connect()
    a = _card(conn, kb, "deploy guide", status)
    b = _card(conn, kb, "second", "triage")
    sup = _supervisor(conn, kb, [_line(f"default/{a}"), _line(f"default/{b}", "second")])
    _as_worker(monkeypatch, conn, kb, sup)
    conn.close()

    assert kt._check_kanban_unblock() is True
    for target in (a, b):
        out = _unblock(target)
        assert out.get("ok") is True, out
        assert out.get("via") == "owner-wake-requeue"
        assert _status(target) in ("ready", "todo")
    conn = kbc.connect()
    try:
        events = [e for e in kb.list_events(conn, a) if e.kind == "owner_wake_requeue"]
        assert [e.payload for e in events] == [{"by": sup, "supervisor_card": sup, "target": a}]
    finally:
        conn.close()


def test_supervisor_cannot_requeue_an_unlisted_card(board, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect()
    listed = _card(conn, kb, "listed", "triage")
    other = _card(conn, kb, "other", "triage")
    remote = _card(conn, kb, "same id, other gateway", "triage")
    sup = _supervisor(conn, kb, [
        _line(f"default/{listed}"),
        # A card on another gateway or another board is not this board's card, even with a matching id.
        _line(f"hub/default/default/{remote}", gateway="hub"),
        _line(f"default/{remote}", gateway="hub"),
        _line(f"ops/{other}"),
    ])
    _as_worker(monkeypatch, conn, kb, sup)
    conn.close()

    for target in (other, remote):
        out = _unblock(target)
        assert out.get("ok") is not True, out
        assert "orchestrator-only" in out.get("error", "")
        assert _status(target) == "triage"


@pytest.mark.parametrize("status", ["running", "done", "ready", "scheduled", "todo"])
def test_supervisor_cannot_requeue_a_listed_card_outside_triage_or_blocked(board, monkeypatch, status):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect()
    target = _card(conn, kb, "moved on", status)
    sup = _supervisor(conn, kb, [_line(f"default/{target}")])
    _as_worker(monkeypatch, conn, kb, sup)
    conn.close()

    out = _unblock(target)
    assert out.get("ok") is not True, out
    assert _status(target) == status


def test_owner_wake_card_with_another_title_is_not_a_grant(board, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    conn = kbc.connect()
    target = _card(conn, kb, "listed", "triage")
    sup = _supervisor(conn, kb, [_line(f"default/{target}")], title="Owner wake notes for the board")
    _as_worker(monkeypatch, conn, kb, sup)
    conn.close()

    assert kt._check_kanban_unblock() is False
    assert _unblock(target).get("ok") is not True
    assert _status(target) == "triage"


def test_supervisor_title_by_another_creator_is_not_a_grant(board, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    conn = kbc.connect()
    target = _card(conn, kb, "listed", "triage")
    sup = _supervisor(conn, kb, [_line(f"default/{target}")], created_by="default")
    _as_worker(monkeypatch, conn, kb, sup)
    conn.close()

    assert kt._check_kanban_unblock() is False
    assert _unblock(target).get("ok") is not True
    assert _status(target) == "triage"


def test_plain_worker_is_still_refused(board, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    conn = kbc.connect()
    target = _card(conn, kb, "triaged", "triage")
    worker = kb.create_task(conn, title="plain worker", body=_line(f"default/{target}"), assignee="default")
    _as_worker(monkeypatch, conn, kb, worker)
    conn.close()

    assert kt._check_kanban_unblock() is False
    out = _unblock(target)
    assert out.get("ok") is not True and "orchestrator-only" in out.get("error", "")
    assert _status(target) == "triage"


def test_requeue_grant_function(board):
    """The grant function the live proof calls: (via, source card) or (None, None)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.kanban_requeue import requeue_grant

    conn = kbc.connect()
    try:
        listed = _card(conn, kb, "listed", "triage")
        running = _card(conn, kb, "running", "running")
        other = _card(conn, kb, "other", "triage")
        sup = _supervisor(conn, kb, [_line(f"default/{listed}"), _line(f"default/{running}")])
        assert requeue_grant(conn, sup, listed, board="default") == ("owner-wake-requeue", sup)
        assert requeue_grant(conn, sup, running, board="default") == (None, None)
        assert requeue_grant(conn, sup, other, board="default") == (None, None)
        assert requeue_grant(conn, sup, listed, board="ops") == (None, None)
    finally:
        conn.close()
