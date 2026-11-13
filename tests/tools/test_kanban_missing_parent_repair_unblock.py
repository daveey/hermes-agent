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


def _rehold(conn, kb, child, events):
    """Release the parent_missing child, then re-hold it with no new ``blocked`` event (e.g. the failure breaker)."""
    if "unblocked" in events:
        assert kb.unblock_task(conn, child)
    with kb.write_txn(conn):
        if "gave_up" in events:
            kb._append_event(conn, child, "gave_up", {"kind": "transient", "reason_code": "worker_failures_exhausted",
                                                      "sticky": True})
        conn.execute("UPDATE tasks SET status = 'blocked', block_kind = 'transient' WHERE id = ?", (child,))


@pytest.mark.parametrize("events", [("unblocked", "gave_up"), ("unblocked",), ("gave_up",)],
                         ids=["unblocked-then-breaker", "unblocked-only", "breaker-only"])
def test_hold_placed_after_the_parent_missing_block_stays_refused(board, monkeypatch, events):
    """Review 5888194553: an unblock or a breaker trip after the parent_missing block ends the repair card's grant.

    The child was released and is now held by something that writes no ``blocked`` event (the failure breaker
    writes ``gave_up``), so the newest ``blocked`` event still names the repair card. That hold is a policy hold.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_requeue as kr

    conn = kbc.connect()
    child, repair = _parent_missing_child(conn, kb)
    _rehold(conn, kb, child, events)
    assert kb.get_task(conn, child).status == "blocked"
    assert kr.missing_parent_repair_grant(conn, repair, child) is None
    before = len(_unblocked(conn, kb, child))
    _as_worker(monkeypatch, conn, kb, repair)
    conn.close()

    assert _unblock(child).get("ok") is not True
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, child).status == "blocked"
        assert len(_unblocked(conn, kb, child)) == before
    finally:
        conn.close()


def test_child_that_later_got_a_human_request_stays_refused(board, monkeypatch):
    """Review 5887767012 nit 3: a human_request on the child is David's to answer, never the repair card's."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect()
    child, repair = _parent_missing_child(conn, kb)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET human_request = ? WHERE id = ?",
                     (json.dumps({"action": "Pick A or B. Unblock = build it; archive = drop it."}), child))
    _as_worker(monkeypatch, conn, kb, repair)
    conn.close()

    assert _unblock(child).get("ok") is not True
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, child).status == "blocked"
        assert not _unblocked(conn, kb, child)
    finally:
        conn.close()


def _reclaim_and_reclaim(conn, kb, tid):
    """The dispatcher reclaims the run this worker holds and a fresh run claims the card."""
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready', claim_lock = NULL, claim_expires = NULL, "
                     "current_run_id = NULL WHERE id = ?", (tid,))
    kb.claim_task(conn, tid)


@pytest.mark.parametrize("case", ["reclaimed", "not-running", "unbound"])
def test_repair_run_that_no_longer_owns_the_card_is_refused(board, monkeypatch, case):
    """Review 5887767012 nit 1: the tool path needs the owner-wake run check; worker-context checks don't cover it.

    ``_is_dispatcher_owned_worker`` only tells a dispatcher worker from a delegate child or an in-process cron. A
    reclaimed earlier run of the repair card passes it, so the unblock txn checks the run itself.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    conn = kbc.connect()
    child, repair = _parent_missing_child(conn, kb)
    _as_worker(monkeypatch, conn, kb, repair)
    if case == "reclaimed":
        old = kb._current_run_id(conn, repair)
        _reclaim_and_reclaim(conn, kb, repair)
        assert kb._current_run_id(conn, repair) != old
        assert kb.get_task(conn, repair).status == "running"
    elif case == "not-running":  # the run id still matches, but the card left running
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (repair,))
    else:  # no HERMES_KANBAN_RUN_ID, against a running row whose run id is unset
        monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET current_run_id = NULL WHERE id = ?", (repair,))
    conn.close()
    assert kt._is_dispatcher_owned_worker() is True

    out = _unblock(child)
    assert out.get("ok") is not True, out
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, child).status == "blocked"
        assert not _unblocked(conn, kb, child)
    finally:
        conn.close()


def test_block_naming_this_repair_card_on_another_child_is_refused(board, monkeypatch):
    """The grant reaches only the repair card's own creator child, even when another block names the card."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect()
    _child, repair = _parent_missing_child(conn, kb, "mine")
    other, _ = _parent_missing_child(conn, kb, "someone else's")
    with kb.write_txn(conn):
        kb._append_event(conn, other, "blocked", {"reason": "re-detected", "kind": "transient",
                                                  "source": "parent_missing", "repair_task_id": repair})
    _as_worker(monkeypatch, conn, kb, repair)
    conn.close()

    assert _unblock(other).get("ok") is not True
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, other).status == "blocked"
        assert not _unblocked(conn, kb, other)
    finally:
        conn.close()


def test_card_the_detector_did_not_file_gets_no_grant(board, monkeypatch):
    """A card the child created and its block names, but not filed by the missing-parent detector, is refused."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect()
    child, _repair = _parent_missing_child(conn, kb)
    lookalike = kb.create_task(conn, title=f"Recover missing dependencies for {child}", assignee="default",
                               creator_task_id=child)
    with kb.write_txn(conn):
        kb._append_event(conn, child, "blocked", {"reason": "re-detected", "kind": "transient",
                                                  "source": "parent_missing", "repair_task_id": lookalike})
    _as_worker(monkeypatch, conn, kb, lookalike)
    conn.close()

    assert _unblock(child).get("ok") is not True
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, child).status == "blocked"
        assert not _unblocked(conn, kb, child)
    finally:
        conn.close()


def test_later_block_naming_the_repair_card_but_not_from_the_detector_is_refused(board, monkeypatch):
    """Only the detector's own ``source: parent_missing`` block grants; a later hold that merely names the card
    (a hand block that copied the old payload's fields) is a hand hold."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect()
    child, repair = _parent_missing_child(conn, kb)
    with kb.write_txn(conn):
        kb._append_event(conn, child, "blocked", {"reason": "David holds it", "actor": "user", "kind": "transient",
                                                  "repair_task_id": repair})
    _as_worker(monkeypatch, conn, kb, repair)
    conn.close()

    assert _unblock(child).get("ok") is not True
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, child).status == "blocked"
        assert not _unblocked(conn, kb, child)
    finally:
        conn.close()


@pytest.mark.parametrize("status", ["todo", "ready", "running", "done", "archived"])
def test_grant_needs_the_child_to_still_be_blocked(board, status):
    """The grant releases a held child only: a child that left ``blocked`` without an ``unblocked`` event (an
    archive, a completion, a direct status move) still has the parent_missing block as its newest event, and the
    repair card must get no grant over it."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_requeue as kr

    conn = kbc.connect()
    try:
        child, repair = _parent_missing_child(conn, kb)
        assert kr.missing_parent_repair_grant(conn, repair, child) == repair
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, child))
        assert kr.missing_parent_repair_grant(conn, repair, child) is None
    finally:
        conn.close()
