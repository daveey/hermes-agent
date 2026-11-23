"""A desk disposition belongs to a stop episode, never to the last comment. [daveey]"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_spend_decision as decision
from hermes_cli.kanban_triage_sweep import sweep_stale_triage
from tests.hermes_cli.test_kanban_spend_route_repair import _route, _stop
from tests.hermes_cli import test_kanban_spend_budget as helpers

kanban_home = helpers.kanban_home


def _wrappers(conn, tid):
    return list(conn.execute("SELECT id,idempotency_key FROM tasks WHERE created_by='kanban-spend-budget' "
                             "AND idempotency_key LIKE ?", (f"spend-budget-decision:{tid}:%",)))


@pytest.mark.parametrize("status", ["archived", "done"])
@pytest.mark.parametrize("cause", [None, "scope_stopped", "safety_refusal"])
def test_archived_wrapper_not_refiled_same_episode(kanban_home, status, cause):
    with kbc.connect_closing() as conn:
        tid = _stop(conn, cause)
        decision.ensure_producer(conn, tid)
        sup = _route(conn, tid)["task_id"]
        if status == "archived":
            assert kb.archive_task(conn, sup)
        else:
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='done' WHERE id=?", (sup,))
        kb.add_comment(conn, tid, "desk", "Keep stopped; no further spend")
        for _ in range(3):
            sweep_stale_triage(conn)
        assert len(_wrappers(conn, tid)) == 1
        assert _route(conn, tid) == {"kind": "dismissed", "task_id": sup}
        assert decision.pending(conn, tid)
        assert kb.get_task(conn, tid).status == "triage"
        assert decision.execution_restricted(conn, tid) == bool(cause)


def test_dismissal_receipt_survives_wrapper_removal(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _stop(conn)
        decision.ensure_producer(conn, tid)
        sup = _route(conn, tid)["task_id"]
        assert kb.archive_task(conn, sup)
        decision.ensure_producer(conn, tid)
        with kb.write_txn(conn):
            conn.execute("DELETE FROM tasks WHERE id=?", (sup,))
        assert decision.dismissed(conn, tid)
        decision.ensure_producer(conn, tid)
        assert _route(conn, tid) == {"kind": "dismissed", "task_id": sup}
        assert _wrappers(conn, tid) == []
        assert decision.pending(conn, tid)


def test_new_episode_refiles_after_archive(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _stop(conn)
        decision.ensure_producer(conn, tid)
        first = _route(conn, tid)["task_id"]
        assert kb.archive_task(conn, first)
        decision.ensure_producer(conn, tid)
        assert _route(conn, tid)["kind"] == "dismissed"
        assert kb.promote_task(conn, tid, actor="operator", reason="new bounded scope")[0]
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
            kb._append_event(conn, tid, decision.EVENT, {"reason": "second budget stop"})
        decision.ensure_producer(conn, tid)
        assert _route(conn, tid)["kind"] == "supervisor"
        assert _route(conn, tid)["task_id"] != first
        assert len(_wrappers(conn, tid)) == 2


def test_key_stable_across_comments(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _stop(conn)
        episode = decision.pending_episode(conn, tid)
        key = f"spend-budget-decision:{tid}:{episode.get('uid') or str(episode['id'])}"
        kb.add_comment(conn, tid, "desk", "Context added before publication")
        decision.ensure_producer(conn, tid)
        assert _wrappers(conn, tid)[0]["idempotency_key"] == key
        sup = _route(conn, tid)["task_id"]
        # Lost notice, including an old event-id key, must reuse the administrative identity.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET idempotency_key=? WHERE id=?", (f"spend-budget-decision:{tid}:legacy", sup))
            conn.execute("DELETE FROM task_events WHERE task_id=? AND kind=?", (tid, decision.NOTICE))
        kb.add_comment(conn, tid, "desk", "Another comment is not another stop")
        decision.ensure_producer(conn, tid)
        assert _route(conn, tid)["task_id"] == sup
        assert len(_wrappers(conn, tid)) == 1
