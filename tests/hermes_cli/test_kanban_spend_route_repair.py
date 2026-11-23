"""Spend ownership repair is independent of execution-release eligibility. [daveey]"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_spend_decision as decision
from hermes_cli import kanban_triage_sweep as sweep
from tests.hermes_cli import test_kanban_spend_budget as helpers

kanban_home = helpers.kanban_home


def _stop(conn, cause=None):
    tid = kb.create_task(conn, title="Retained work", assignee="default")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        kb._append_event(conn, tid, decision.EVENT, {"reason": "stop", **({"cause": cause} if cause else {})})
    return tid


def _route(conn, tid):
    return helpers._events(conn, tid, decision.NOTICE)[-1].payload["producer"]


@pytest.mark.parametrize("status", ["done", "archived", "missing", "null"])
@pytest.mark.parametrize("cause", [None, "scope_stopped", "safety_refusal"])
def test_closed_or_null_producer_is_replaced_without_execution_release(kanban_home, status, cause):
    with kbc.connect_closing() as conn:
        tid = _stop(conn, cause)
        decision.ensure_producer(conn, tid)
        old = _route(conn, tid)["task_id"]
        with kb.write_txn(conn):
            if status == "null":
                episode = decision.pending_episode(conn, tid)
                kb._append_event(conn, tid, decision.NOTICE, {
                    "episode": episode.get("uid") or str(episode["id"]), "producer": None})
            elif status == "missing":
                conn.execute("DELETE FROM tasks WHERE id=?", (old,))
            else:
                conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, old))
        decision.ensure_producer(conn, tid)
        route = _route(conn, tid)
        assert route["kind"] == "supervisor"
        assert route["task_id"] != old
        assert kb.get_task(conn, route["task_id"]).status not in ("done", "archived")
        count = len(helpers._events(conn, tid, decision.NOTICE))
        decision.ensure_producer(conn, tid)
        assert len(helpers._events(conn, tid, decision.NOTICE)) == count
        assert kb.get_task(conn, tid).status == "triage"
        assert decision.pending(conn, tid)
        assert decision.execution_restricted(conn, tid) == bool(cause)


@pytest.mark.parametrize("filter", ["fresh", "gated", "contained", "capped", "foreign"])
def test_sweep_repairs_missing_routes_before_release_filters(kanban_home, monkeypatch, filter):
    from hermes_cli import kanban_containment_connections as containment
    from hermes_cli import kanban_wake_check as wake
    with kbc.connect_closing() as conn:
        tid = _stop(conn, "scope_stopped")
        kb.add_comment(conn, tid, "diagnostic", "frequent fresh diagnostic does not defer decision ownership")
        if filter == "gated":
            monkeypatch.setattr(sweep, "_gate_exclusion", lambda: "AND 0 ")
        if filter == "contained":
            monkeypatch.setattr(containment, "held", lambda c, t: t == tid)
        if filter == "capped":
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "triage_sweep_capped", {})
        if filter == "foreign":
            monkeypatch.setattr(wake, "foreign_ids", lambda c: {tid})
        assert sweep.sweep_stale_triage(conn) == []
        notices = helpers._events(conn, tid, decision.NOTICE)
        assert bool(notices) == (filter != "foreign")
        if notices:
            assert kb.get_task(conn, _route(conn, tid)["task_id"]).status == "ready"
        assert kb.get_task(conn, tid).status == "triage"
        assert decision.execution_restricted(conn, tid)


def test_failed_publication_retries_and_does_not_starve_other_stops(kanban_home, monkeypatch):
    real = decision.producer
    with kbc.connect_closing() as conn:
        first, second = _stop(conn), _stop(conn)
        def fail_one(c, tid, note):
            if tid == first:
                raise RuntimeError("injected publication failure")
            return real(c, tid, note)
        monkeypatch.setattr(decision, "producer", fail_one)
        sweep.sweep_stale_triage(conn)
        assert not helpers._events(conn, first, decision.NOTICE)
        assert _route(conn, second)["kind"] == "supervisor"
        monkeypatch.setattr(decision, "producer", real)
        sweep.sweep_stale_triage(conn)
        owner = kb.get_task(conn, _route(conn, first)["task_id"])
        assert "retain this decision card" in owner.body
        assert "instead of completing" in owner.body
        assert kb.get_task(conn, first).status == "triage"


def test_resolved_episode_does_not_reopen_closed_producer(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _stop(conn)
        decision.ensure_producer(conn, tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (_route(conn, tid)["task_id"],))
        assert kb.promote_task(conn, tid, actor="operator", reason="bounded new phase")[0]
        count = len(helpers._events(conn, tid, decision.NOTICE))
        decision.ensure_producer(conn, tid)
        assert len(helpers._events(conn, tid, decision.NOTICE)) == count
