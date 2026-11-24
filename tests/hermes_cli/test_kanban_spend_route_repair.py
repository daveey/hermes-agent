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


def _carded_stop(conn, cause=None):
    """A stop that already has its decision card. A stop with a cause gets none of its own
    (test_kanban_spend_decision_causes), so the cause arrives after the card, as a scope retain on a budget stop
    does."""
    tid = _stop(conn)
    decision.ensure_producer(conn, tid)
    if cause:
        episode = decision.pending_episode(conn, tid)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "decision_hold_restricted",
                             {"cause": cause, "episode": episode.get("uid") or str(episode["id"])})
        decision.ensure_producer(conn, tid)
    return tid


@pytest.mark.parametrize("status", ["done", "archived", "missing", "null"])
@pytest.mark.parametrize("cause", [None, "scope_stopped", "safety_refusal"])
def test_closed_or_null_producer_is_replaced_without_execution_release(kanban_home, status, cause):
    with kbc.connect_closing() as conn:
        tid = _carded_stop(conn, cause)
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
        if status in ("done", "archived"):
            assert route == {"kind": "dismissed", "task_id": old}
        elif cause and status == "missing":
            assert route == {"kind": "unavailable"}  # a restricted stop is never given a new decision card
        else:
            assert route["kind"] == "supervisor"
            assert (route["task_id"] == old) == (status == "null")
            assert kb.get_task(conn, route["task_id"]).status not in ("done", "archived")
        count = len(helpers._events(conn, tid, decision.NOTICE))
        decision.ensure_producer(conn, tid)
        assert len(helpers._events(conn, tid, decision.NOTICE)) == count
        assert kb.get_task(conn, tid).status == "triage"
        assert decision.pending(conn, tid)
        assert decision.execution_restricted(conn, tid) == bool(cause)


@pytest.mark.parametrize("filter", ["fresh", "gated", "contained", "capped", "foreign"])
@pytest.mark.parametrize("cause", [None, "scope_stopped"])
def test_sweep_repairs_missing_routes_before_release_filters(kanban_home, monkeypatch, filter, cause):
    from hermes_cli import kanban_containment_connections as containment
    from hermes_cli import kanban_wake_check as wake
    with kbc.connect_closing() as conn:
        tid = _stop(conn, cause)
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
        if notices and cause:
            assert _route(conn, tid) == {"kind": "unavailable"}
        elif notices:
            assert kb.get_task(conn, _route(conn, tid)["task_id"]).status == "ready"
        assert kb.get_task(conn, tid).status == "triage"
        assert decision.execution_restricted(conn, tid) == bool(cause)


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
        assert f"hermes kanban promote {first} --reason" in owner.body and "or archive it" in owner.body
        # Retaining the card would be a second stop with no decision.
        assert "retain this decision card" not in owner.body and "non-executing owner" not in owner.body
        assert kb.get_task(conn, first).status == "triage"


@pytest.mark.parametrize("cause", [None, "scope_stopped", "safety_refusal"])
def test_live_route_sweep_needs_no_writer_lock(kanban_home, monkeypatch, cause):
    with kbc.connect_closing() as conn:
        tid = _stop(conn, cause)
        decision.ensure_producer(conn, tid)
        count = len(helpers._events(conn, tid, decision.NOTICE))
        def forbidden(*args, **kwargs):
            pytest.fail("unchanged decision route must not wait for the SQLite writer")
        monkeypatch.setattr(kb, "write_txn", forbidden)
        decision.repair_triage_routes(conn, skip=set())
        assert len(helpers._events(conn, tid, decision.NOTICE)) == count
        assert decision.pending(conn, tid)


def test_ready_card_reaches_spawn_past_unchanged_spend_routes(
        kanban_home, monkeypatch, all_assignees_spawnable):
    from hermes_cli import kanban_db_dispatch as dispatch
    from tests.hermes_cli.test_kanban_quota_episode import routing
    routing(monkeypatch)
    with kbc.connect_closing() as conn:
        stopped = _stop(conn, "scope_stopped")
        decision.ensure_producer(conn, stopped)
        ready = kb.create_task(conn, title="Ready with free slot", assignee="default")
        real = decision.ensure_producer
        real_txn = kb.write_txn
        checking = [False]
        def no_writer_for_noop(*args, **kwargs):
            if checking[0]:
                pytest.fail("pre-admission no-op maintenance waited for the writer")
            return real_txn(*args, **kwargs)
        def checked(c, tid):
            checking[0] = True
            try:
                return real(c, tid)
            finally:
                checking[0] = False
        monkeypatch.setattr(kb, "write_txn", no_writer_for_noop)
        monkeypatch.setattr(decision, "ensure_producer", checked)
        spawned = []
        result = dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: spawned.append(task.id),
                                        max_spawn=10, max_in_progress=10)
        assert ready in spawned
        assert any(row[0] == ready for row in result.spawned)
        assert kb.get_task(conn, stopped).status == "triage"
        assert decision.pending(conn, stopped)


def test_repair_rechecks_after_writer_acquisition(kanban_home, monkeypatch):
    from contextlib import contextmanager
    with kbc.connect_closing() as conn:
        tid = _stop(conn)
        original = kb.write_txn
        calls = []
        @contextmanager
        def raced(c, **kwargs):
            # Another writer supplied a live route after the read-only check.
            monkeypatch.setattr(kb, 'write_txn', original)
            decision.ensure_producer(c, tid)
            calls.append('other_writer')
            with original(c, **kwargs):
                yield
        monkeypatch.setattr(kb, 'write_txn', raced)
        decision.ensure_producer(conn, tid)
        assert calls == ['other_writer']
        assert len(helpers._events(conn, tid, decision.NOTICE)) == 1


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
