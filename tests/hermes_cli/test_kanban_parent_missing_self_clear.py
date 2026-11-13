"""A parent_missing block clears itself once the parent is back (2026-11-13, David-MBP).

t_01a1d0ea6db6c158 was blocked "parent t_01a1d40d52e1672b missing from this board" while the MacBook's Board sync
was down. When sync came back the remote-parent mirror said the parent was done and ``unsatisfied_parents`` was
empty, but the child stayed blocked: the readiness pass treats every ``blocked`` event as sticky, and the repair card
had no grant to unblock it. The readiness pass now returns the detector's own block to the todo path. [daveey]"""
from __future__ import annotations

import json
import time

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_cross_gateway_parents as support

local, kanban_home, svc, live = support.local, support.kanban_home, support.svc, support.live


def _blocked_on_missing(conn, parent: str) -> str:
    child = kb.create_task(conn, title="dependent", assignee="worker")
    support._link_raw(conn, parent, child)
    kb.recompute_ready(conn)
    support._age_streak(conn, child)
    kb.recompute_ready(conn)
    t = kb.get_task(conn, child)
    assert (t.status, t.block_kind) == ("blocked", "transient")
    return child


def _repair(conn, child) -> str:
    [(rid,)] = support._repairs(conn, child)
    return rid


def test_the_parent_row_appearing_done_returns_the_child_to_ready(local):
    child = _blocked_on_missing(local, "t_late")
    repair = _repair(local, child)
    kb.recompute_ready(local)
    assert kb.get_task(local, child).status == "blocked"  # still missing: the block holds
    with kbc.write_txn(local):  # the parent's row syncs in, already done
        local.execute("INSERT INTO tasks (id, title, status, created_at) VALUES ('t_late', 'late', 'done', ?)",
                      (int(time.time()),))
    kb.recompute_ready(local)
    assert kb.get_task(local, child).status == "ready"  # before the fix: blocked forever
    [unblocked] = support._events(local, child, "unblocked")
    payload = json.loads(unblocked["payload"])
    assert payload["cleared_block"] == "parent_missing" and payload["repair_task_id"] == repair
    assert payload["parents"] == ["t_late"]
    assert len(support._events(local, child, "parent_found")) == 1
    assert kb.get_task(local, repair).status == "ready"  # the repair card is left to finish on its own
    assert kb.claim_task(local, child) is not None


def test_an_open_parent_coming_back_returns_the_child_to_todo(local):
    child = _blocked_on_missing(local, "t_late")
    with kbc.write_txn(local):
        local.execute("INSERT INTO tasks (id, title, status, created_at) VALUES ('t_late', 'late', 'running', 0)")
    kb.recompute_ready(local)
    assert kb.get_task(local, child).status == "todo"  # unblocked, but still gated on the open parent
    with kbc.write_txn(local):
        local.execute("UPDATE tasks SET status = 'done' WHERE id = 't_late'")
    kb.recompute_ready(local)
    assert kb.get_task(local, child).status == "ready"


def test_the_remote_parent_mirror_saying_done_returns_the_child_to_ready(live, svc, kanban_home):
    child = kb.create_task(live, title="child", assignee="worker")
    support._round(kanban_home, svc)
    support._link_raw(live, "t_foreign", child)
    support._stamp_replica(time.time())  # at the feed head: the Board really lacks t_foreign for now
    kb.recompute_ready(live)
    support._age_streak(live, child)
    kb.recompute_ready(live)
    assert kb.get_task(live, child).status == "blocked"
    # The owning gateway's card reaches the Board, done; the next round mirrors it (kanban_sync_remote_parents).
    support._other(svc, "p", "tasks", "t_foreign", {"title": "foreign parent", "status": "done", "created_at": 1})
    support._round(kanban_home, svc)
    assert kb.get_task(live, "t_foreign") is None and kb.parent_states(live, child) == [("t_foreign", "done")]
    assert kb.unsatisfied_parents(live, child) == []
    kb.recompute_ready(live)
    assert kb.get_task(live, child).status == "ready"  # before the fix: blocked, with nothing unsatisfied
    assert json.loads(support._events(live, child, "unblocked")[-1]["payload"])["cleared_block"] == "parent_missing"


def test_a_transient_block_from_another_source_stays_blocked(local):
    with kbc.write_txn(local):
        local.execute("INSERT INTO tasks (id, title, status, created_at) VALUES ('t_p', 'p', 'done', 0)")
    child = kb.create_task(local, title="dependent", assignee="worker", parents=["t_p"])
    with kbc.write_txn(local):
        local.execute("UPDATE tasks SET status = 'blocked', block_kind = 'transient' WHERE id = ?", (child,))
        kb._append_event(local, child, "blocked", {"kind": "transient", "source": "breaker", "reason": "x"})
    kb.recompute_ready(local)
    assert kb.get_task(local, child).status == "blocked"
    assert not support._events(local, child, "unblocked")


def test_a_later_hand_block_over_a_parent_missing_block_stays_blocked(local):
    child = _blocked_on_missing(local, "t_late")
    with kbc.write_txn(local):  # someone blocks it again by hand while it is still blocked
        kb._append_event(local, child, "blocked", {"kind": "needs_input", "reason": "hold this"})
        local.execute("INSERT INTO tasks (id, title, status, created_at) VALUES ('t_late', 'late', 'done', 0)")
    kb.recompute_ready(local)
    assert kb.get_task(local, child).status == "blocked"


def test_a_parent_missing_block_with_a_human_request_stays_blocked(local):
    child = _blocked_on_missing(local, "t_late")
    with kbc.write_txn(local):
        local.execute("UPDATE tasks SET human_request = ? WHERE id = ?", (json.dumps({"action": "x"}), child))
        local.execute("INSERT INTO tasks (id, title, status, created_at) VALUES ('t_late', 'late', 'done', 0)")
    kb.recompute_ready(local)
    assert kb.get_task(local, child).status == "blocked"


def test_a_parent_missing_block_with_one_parent_still_missing_stays_blocked(local):
    child = kb.create_task(local, title="dependent", assignee="worker")
    support._link_raw(local, "t_a", child)
    support._link_raw(local, "t_b", child)
    kb.recompute_ready(local)
    support._age_streak(local, child)
    kb.recompute_ready(local)
    assert kb.get_task(local, child).status == "blocked"
    with kbc.write_txn(local):
        local.execute("INSERT INTO tasks (id, title, status, created_at) VALUES ('t_a', 'a', 'done', 0)")
    kb.recompute_ready(local)
    assert kb.get_task(local, child).status == "blocked"  # t_b is still missing


def test_every_parent_unlinked_returns_the_detector_blocked_child_to_ready(local):
    """PR #2104 review: with no parent left ``parent_states`` is ``[]``; the child must not stay blocked on nothing."""
    child = _blocked_on_missing(local, "t_late")
    assert kb.unlink_tasks(local, "t_late", child)
    assert kb.parent_states(local, child) == []
    kb.recompute_ready(local)
    assert kb.get_task(local, child).status == "ready"  # before the fix: blocked with nothing to wait on
    payload = json.loads(support._events(local, child, "unblocked")[-1]["payload"])
    assert payload["cleared_block"] == "parent_missing" and payload["parents"] == ["t_late"]


def test_a_hand_block_with_no_parents_stays_blocked(local):
    child = kb.create_task(local, title="dependent", assignee="worker")
    with kbc.write_txn(local):
        local.execute("UPDATE tasks SET status = 'blocked', block_kind = 'transient' WHERE id = ?", (child,))
        kb._append_event(local, child, "blocked", {"kind": "transient", "source": "breaker", "reason": "x"})
    kb.recompute_ready(local)
    assert kb.get_task(local, child).status == "blocked"


def _repair_comments(conn, repair):
    return [c.body for c in kb.list_comments(conn, repair)]


def test_clearing_the_block_comments_on_the_open_repair_card(local):
    """PR #2104 review: the repair card is told the block cleared, so its worker does not chase a present parent."""
    child = _blocked_on_missing(local, "t_late")
    repair = _repair(local, child)
    [(blocked_id,)] = local.execute(
        "SELECT id FROM task_events WHERE task_id = ? AND kind = 'blocked' "
        "AND json_extract(payload, '$.source') = 'parent_missing'", (child,)).fetchall()
    with kbc.write_txn(local):
        local.execute("INSERT INTO tasks (id, title, status, created_at) VALUES ('t_late', 'late', 'done', 0)")
    kb.recompute_ready(local)
    assert kb.get_task(local, child).status == "ready"
    [body] = _repair_comments(local, repair)  # before the fix: no comment, the repair card reads as live work
    assert child in body and "parent_found" in body and str(blocked_id) in body
    kb.recompute_ready(local)
    assert len(_repair_comments(local, repair)) == 1  # once per cleared block


def test_a_finished_repair_card_gets_no_comment(local):
    child = _blocked_on_missing(local, "t_late")
    repair = _repair(local, child)
    with kbc.write_txn(local):
        local.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (repair,))
        local.execute("INSERT INTO tasks (id, title, status, created_at) VALUES ('t_late', 'late', 'done', 0)")
    kb.recompute_ready(local)
    assert kb.get_task(local, child).status == "ready"
    assert _repair_comments(local, repair) == []
