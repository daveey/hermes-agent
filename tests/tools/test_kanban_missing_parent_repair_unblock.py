"""A missing-parent repair card may unblock exactly the child whose parent_missing block names it. [daveey]

The dispatcher blocks a child whose parent stayed missing (``source: parent_missing``) and files a repair card
(``created_by='missing-parent-detector'``, ``creator_task_id=<child>``) that owns recovery. Before the grant the
repair worker's ``kanban_unblock`` was refused (no rule), so it unblocked over ssh with the CLI and the event read
as David's (``actor: user``). Every other worker, target, or block stays refused.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def board(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("kanban:\n  sync:\n    enabled: false\n")  # repair admission reads it
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    for key in (
        "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_HOME",
        "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_WORKER_TASK", "HERMES_SESSION_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _parent_missing_child(conn, kb, title="dependent"):
    """A child the real detector blocked as parent_missing; returns ``(child, repair)``."""
    child = kb.create_task(conn, title=title, assignee="default")
    with kb.write_txn(conn):
        conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", ("t_ghost0000000000", child))
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (child,))
    kb.recompute_ready(conn)
    with kb.write_txn(conn):
        conn.execute("UPDATE task_events SET created_at = created_at - ? WHERE task_id = ?",
                     (kb.PARENT_MISSING_BLOCK_SECONDS + 1, child))
    kb.recompute_ready(conn)
    assert kb.get_task(conn, child).status == "blocked"
    ev = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked' "
                      "ORDER BY id DESC LIMIT 1", (child,)).fetchone()
    repair = json.loads(ev[0])["repair_task_id"]
    assert kb.get_task(conn, repair).created_by == "missing-parent-detector"
    return child, repair


def _as_worker(monkeypatch, conn, kb, tid):
    kb.claim_task(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(kb._current_run_id(conn, tid)))


def _unblock(target):
    from tools import kanban_tools as kt
    return json.loads(kt._handle_unblock({"task_id": target}))


def _unblocked(conn, kb, tid):
    return [e.payload for e in kb.list_events(conn, tid) if e.kind == "unblocked"]


def test_repair_worker_unblocks_its_child_attributed_to_the_repair_card(board, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    conn = kbc.connect()
    child, repair = _parent_missing_child(conn, kb)
    _as_worker(monkeypatch, conn, kb, repair)
    conn.close()

    assert kt._check_kanban_unblock() is True
    out = _unblock(child)
    assert out.get("ok") is True, out
    assert out.get("via") == "missing-parent-repair"
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, child).status in ("ready", "todo")
        [payload] = _unblocked(conn, kb, child)
        assert payload["actor"] == f"agent (task {repair})"
        assert payload["repair_task_id"] == repair
    finally:
        conn.close()


def test_another_worker_cannot_unblock_a_parent_missing_child(board, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect()
    child, _repair = _parent_missing_child(conn, kb)
    other = kb.create_task(conn, title="plain worker", assignee="default")
    _as_worker(monkeypatch, conn, kb, other)
    conn.close()

    assert _unblock(child).get("ok") is not True
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, child).status == "blocked"
        assert not _unblocked(conn, kb, child)
    finally:
        conn.close()


def test_repair_card_cannot_unblock_another_target(board, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect()
    child, repair = _parent_missing_child(conn, kb, "mine")
    other, _ = _parent_missing_child(conn, kb, "someone else's")
    held = kb.create_task(conn, title="hand hold", assignee="default")
    kb.block_task(conn, held, reason="hold", actor="user", kind="transient")
    _as_worker(monkeypatch, conn, kb, repair)
    conn.close()

    for target in (other, held):
        assert _unblock(target).get("ok") is not True, target
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, other).status == "blocked"
        assert kb.get_task(conn, held).status == "blocked"
        assert kb.get_task(conn, child).status == "blocked"
    finally:
        conn.close()


def test_block_superseded_by_a_hand_block_stays_refused(board, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect()
    child, repair = _parent_missing_child(conn, kb)
    with kb.write_txn(conn):  # David re-blocks the still-blocked child by hand (CLI block, actor user)
        kb._append_event(conn, child, "blocked", {"reason": "David holds it", "actor": "user",
                                                  "kind": "transient"})
    _as_worker(monkeypatch, conn, kb, repair)
    conn.close()

    assert _unblock(child).get("ok") is not True
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, child).status == "blocked"
    finally:
        conn.close()


def test_parent_missing_block_naming_a_different_repair_card_stays_refused(board, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect()
    child, repair = _parent_missing_child(conn, kb)
    stale = kb.create_task(conn, title=f"Recover missing dependencies for {child}", assignee="default",
                           created_by="missing-parent-detector", creator_task_id=child)
    assert stale != repair
    _as_worker(monkeypatch, conn, kb, stale)
    conn.close()

    assert _unblock(child).get("ok") is not True
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, child).status == "blocked"
    finally:
        conn.close()


def test_grant_is_rechecked_inside_the_unblock_transaction(board, monkeypatch):
    """A hand block that lands between the grant read and the write still wins."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_requeue as kr

    conn = kbc.connect()
    child, repair = _parent_missing_child(conn, kb)
    conn.close()
    real = kr.missing_parent_repair_grant
    calls = {"n": 0}

    def racing(c, worker, target):
        calls["n"] += 1
        out = real(c, worker, target)
        if calls["n"] == 1:  # the grant read: then a hand block supersedes the detector's
            with kb.write_txn(c):
                kb._append_event(c, target, "blocked", {"reason": "hand", "actor": "user"})
        return out

    monkeypatch.setattr(kr, "missing_parent_repair_grant", racing)
    conn = kbc.connect()
    _as_worker(monkeypatch, conn, kb, repair)
    conn.close()
    assert _unblock(child).get("ok") is not True
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, child).status == "blocked"
    finally:
        conn.close()


def test_repair_brief_names_the_tool_and_forbids_ssh_cli(board):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect()
    try:
        _child, repair = _parent_missing_child(conn, kb)
        body = kb.get_task(conn, repair).body
    finally:
        conn.close()
    assert "kanban_unblock" in body
    assert "across gateways" in body
    assert "ssh" in body and "hermes" in body


def test_child_on_another_gateway_is_forwarded_as_an_operator_call(board, monkeypatch):
    """A pool worker running the repair card has the child only as a replica copy: the tool forwards it."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_foreign_card as kfc
    from tools import kanban_tools_graph as ktg

    conn = kbc.connect()
    repair = kb.create_task(conn, title="Recover missing dependencies for t_feedfacecafe0001", assignee="default",
                            created_by="missing-parent-detector")
    _as_worker(monkeypatch, conn, kb, repair)
    conn.close()
    foreign = "t_feedfacecafe0001"
    monkeypatch.setattr(kfc, "refusal", lambda c, tid: f"{tid} lives on macbook" if tid == foreign else None)
    sent = []
    monkeypatch.setattr(ktg, "handle_supervisor_request",
                        lambda args, **kw: (sent.append(args), json.dumps({"ok": True, "request_id": 7}))[1])

    out = _unblock(foreign)
    assert out == {"ok": True, "request_id": 7}
    [args] = sent
    assert args["ops"][0] == {"op": "operator", "action": "unblock", "task_id": foreign,
                              "reason": args["ops"][0]["reason"]}
    assert repair in args["ops"][0]["reason"]
