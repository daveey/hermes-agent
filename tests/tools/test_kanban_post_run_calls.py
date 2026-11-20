"""Kanban calls a worker makes after its own run ended (agent-6ab88d2b1c). [daveey]

Incident t_01a1a57922c2ba77 run 158177: the worker parked with kanban_block(kind='dependency'), then
unlinked that same parent (which promoted the card back to READY), then tried kanban_block(kind='time')
and kanban_schedule. Both were refused with "unknown id or not in running/ready/triage" although the
card was READY. The real cause was that its run had already ended.

Contract:
- a run-ending success (block, schedule, complete, continue, request_review) says so: ``run_ended`` +
  ``next``;
- link/unlink by a worker whose run has ended are refused with ``code=run_already_ended`` and change
  nothing (edges and status stay as the park left them);
- a later lifecycle call is refused with ``code=run_already_ended`` naming the run, how it ended and the
  card's current status (not "unknown id");
- an orchestrator (no HERMES_KANBAN_TASK) and a worker whose run is live keep their graph tools.
"""
from __future__ import annotations

import json

import pytest

from tests.tools import test_kanban_tools as _kt_tests

worker_env = _kt_tests.worker_env


def _conn():
    from hermes_cli import kanban_db_connect as kbc
    return kbc.connect()


def _state(tid):
    from hermes_cli import kanban_db as kb
    with _conn() as conn:
        return kb.get_task(conn, tid).status, kb.parent_ids(conn, tid)


def _new_task(title):
    from hermes_cli import kanban_db as kb
    with _conn() as conn:
        return kb.create_task(conn, title=title)


def _dependency_park(tid):
    """Link the worker's own live card under a fresh parent, then park on it."""
    from tools import kanban_tools as kt
    parent = _new_task("upstream")
    linked = json.loads(kt._handle_link({"parent_id": parent, "child_id": tid}))
    assert linked["ok"] is True, linked
    parked = json.loads(kt._handle_block({"reason": "waiting on upstream", "kind": "dependency"}))
    assert parked["ok"] is True and parked["status"] == "todo", parked
    return parent, parked


def _assert_run_already_ended(out: dict, run_id: int, status: str):
    assert out.get("code") == "run_already_ended", out
    assert out.get("run_id") == run_id and out.get("status") == status, out
    assert out.get("run_outcome") == "blocked", out
    assert f"run {run_id}" in out["error"] and status in out["error"], out
    assert "unknown id" not in out["error"], out


def _run_id():
    import os
    return int(os.environ["HERMES_KANBAN_RUN_ID"])


# ── the incident sequence ──────────────────────────────────────────────────────────────────────

def test_block_success_says_the_run_ended(worker_env):
    _parent, parked = _dependency_park(worker_env)
    assert parked["run_ended"] is True, parked
    assert "This run is over" in parked["next"] and "todo" in parked["next"], parked


def test_unlink_after_dependency_park_is_refused_and_changes_nothing(worker_env):
    from tools import kanban_tools as kt
    parent, _ = _dependency_park(worker_env)
    out = json.loads(kt._handle_unlink({"parent_id": parent, "child_id": worker_env}))
    _assert_run_already_ended(out, _run_id(), "todo")
    assert _state(worker_env) == ("todo", [parent])


def test_link_after_dependency_park_is_refused_and_changes_nothing(worker_env):
    from tools import kanban_tools as kt
    parent, _ = _dependency_park(worker_env)
    other = _new_task("another upstream")
    out = json.loads(kt._handle_link({"parent_id": other, "child_id": worker_env}))
    _assert_run_already_ended(out, _run_id(), "todo")
    assert _state(worker_env) == ("todo", [parent])


@pytest.mark.parametrize("call", [
    ("_handle_block", {"reason": "SCHEDULED_UNTIL=2099-01-01T00:00:00Z wait", "kind": "time",
                       "clock_only": True, "awaiting": {"kind": "clock", "why": "fixture window"}}),
    ("_handle_schedule", {"reason": "SCHEDULED_UNTIL=2099-01-01T00:00:00Z wait", "clock_only": True, "awaiting": {"kind": "clock", "why": "fixture window"}}),
    ("_handle_continue", {"checkpoint": "resume later"}),
    ("_handle_request_review", {"summary": "please review"}),
    ("_handle_request_changes", {"reason": "please fix"}),
])
def test_lifecycle_after_dependency_park_names_the_ended_run(worker_env, call):
    from tools import kanban_tools as kt
    parent, _ = _dependency_park(worker_env)
    name, args = call
    out = json.loads(getattr(kt, name)(dict(args)))
    _assert_run_already_ended(out, _run_id(), "todo")
    assert _state(worker_env) == ("todo", [parent])


def test_incident_sequence_end_to_end(worker_env):
    """block(dependency) -> unlink -> block(time) -> schedule: the park survives every later call."""
    from tools import kanban_tools as kt
    parent, _ = _dependency_park(worker_env)
    for fn, args in (
        (kt._handle_unlink, {"parent_id": parent, "child_id": worker_env}),
        (kt._handle_block, {"reason": "SCHEDULED_UNTIL=2099-01-01T00:00:00Z", "kind": "time", "clock_only": True, "awaiting": {"kind": "clock", "why": "fixture window"}}),
        (kt._handle_schedule, {"reason": "SCHEDULED_UNTIL=2099-01-01T00:00:00Z", "clock_only": True, "awaiting": {"kind": "clock", "why": "fixture window"}}),
    ):
        _assert_run_already_ended(json.loads(fn(dict(args))), _run_id(), "todo")
    assert _state(worker_env) == ("todo", [parent])


def test_refusal_reports_the_successor_run_status(worker_env):
    """A worker whose run was reclaimed and re-claimed sees the card's live status, not 'unknown id'."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt
    old_run = _run_id()
    with _conn() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET status='released', outcome='reclaimed', ended_at=1 WHERE id=?",
                         (old_run,))
            conn.execute("UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL WHERE id=?",
                         (worker_env,))
        kb.claim_task(conn, worker_env)
        new_run = kb._current_run_id(conn, worker_env)
    assert new_run != old_run
    out = json.loads(kt._handle_schedule({"reason": "SCHEDULED_UNTIL=2099-01-01T00:00:00Z", "clock_only": True, "awaiting": {"kind": "clock", "why": "fixture window"}}))
    assert out.get("code") == "run_already_ended" and out.get("run_id") == old_run, out
    assert out.get("run_outcome") == "reclaimed" and out.get("status") == "running", out
    parent = _new_task("upstream")
    out = json.loads(kt._handle_link({"parent_id": parent, "child_id": worker_env}))
    assert out.get("code") == "run_already_ended", out
    with _conn() as conn:
        assert kb.get_task(conn, worker_env).status == "running"
        assert kb.parent_ids(conn, worker_env) == []
        assert kb._current_run_id(conn, worker_env) == new_run


# ── successful terminal tools say the run ended ────────────────────────────────────────────────

@pytest.mark.parametrize("name,args,status", [
    ("_handle_schedule", {"reason": "SCHEDULED_UNTIL=2099-01-01T00:00:00Z wait", "clock_only": True, "awaiting": {"kind": "clock", "why": "fixture window"}}, "scheduled"),
    ("_handle_block", {"reason": "SCHEDULED_UNTIL=2099-01-01T00:00:00Z wait", "kind": "time",
                       "clock_only": True, "awaiting": {"kind": "clock", "why": "fixture window"}}, "scheduled"),
    ("_handle_complete", {"summary": "done"}, "done"),
    ("_handle_continue", {"checkpoint": "resume later"}, "ready"),
    ("_handle_request_review", {"summary": "please review"}, "review"),
])
def test_terminal_success_signals_run_ended(worker_env, name, args, status):
    from tools import kanban_tools as kt
    out = json.loads(getattr(kt, name)(dict(args)))
    assert out.get("ok") is True, out
    assert out.get("status", status) == status, out
    assert out["run_ended"] is True, out
    assert "This run is over" in out["next"], out


# ── supported paths stay supported ─────────────────────────────────────────────────────────────

def test_live_worker_links_and_unlinks_its_own_card(worker_env):
    from tools import kanban_tools as kt
    parent = _new_task("upstream")
    assert json.loads(kt._handle_link({"parent_id": parent, "child_id": worker_env}))["ok"] is True
    out = json.loads(kt._handle_unlink({"parent_id": parent, "child_id": worker_env}))
    assert out["ok"] is True and out["unlinked"] is True, out
    assert _state(worker_env) == ("running", [])


def test_live_worker_heartbeat_result_has_no_run_ended(worker_env):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_heartbeat({"note": "alive"}))
    assert out["ok"] is True and "run_ended" not in out, out


def test_orchestrator_edits_a_parked_card_graph(worker_env, monkeypatch):
    from tools import kanban_tools as kt
    parent, _ = _dependency_park(worker_env)
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    out = json.loads(kt._handle_unlink({"parent_id": parent, "child_id": worker_env}))
    assert out["ok"] is True, out
    other = _new_task("new upstream")
    out = json.loads(kt._handle_link({"parent_id": other, "child_id": worker_env}))
    assert out["ok"] is True, out
    assert _state(worker_env)[1] == [other]


def test_own_blocked_card_can_still_be_closed_by_its_worker(worker_env):
    """kanban_complete keeps its own-park close (parked_by_own_run): the run-ended fence skips it."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt
    with _conn() as conn:
        assert kb.block_task(conn, worker_env, reason="David: approve", kind="needs_input",
                             expected_run_id=_run_id())
    out = json.loads(kt._handle_complete({"summary": "resolved after all"}))
    assert out.get("ok") is True, out
    assert out["run_ended"] is True, out
    assert _state(worker_env)[0] == "done"


def test_continue_keeps_its_own_next_and_adds_run_ended(worker_env):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_continue({"checkpoint": "resume later"}))
    assert out["next"].startswith("Checkpoint saved;"), out
    assert "This run is over; the card is ready." in out["next"], out


def test_orchestrator_park_of_a_running_card_has_no_run_ended(worker_env, monkeypatch):
    """The caller is not the worker whose run ended: no 'your run is over' signal."""
    from tools import kanban_tools as kt
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    out = json.loads(kt._handle_schedule({"task_id": worker_env, "clock_only": True, "awaiting": {"kind": "clock", "why": "fixture window"},
                                          "reason": "SCHEDULED_UNTIL=2099-01-01T00:00:00Z wait"}))
    assert out.get("ok") is True and out["status"] == "scheduled", out
    assert "run_ended" not in out and "next" not in out, out


def test_unbound_run_id_leaves_graph_tools_as_before(worker_env, monkeypatch):
    """No parseable HERMES_KANBAN_RUN_ID: the fence cannot name a run, so it does not apply."""
    from tools import kanban_tools as kt
    from tools import kanban_tools_run_end as kre
    parent, _ = _dependency_park(worker_env)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "not-a-number")
    with _conn() as conn:
        assert kre.ended_run(conn) is None
    out = json.loads(kt._handle_unlink({"parent_id": parent, "child_id": worker_env}))
    assert out.get("ok") is True, out


def test_ended_run_ignores_another_tasks_scope(worker_env):
    from tools import kanban_tools_run_end as kre
    _dependency_park(worker_env)
    with _conn() as conn:
        assert kre.ended_run(conn, worker_env)["run_id"] == _run_id()
        assert kre.ended_run(conn, "t_someone_else") is None


def test_parked_worker_may_still_link_two_other_cards(worker_env):
    """Replay: 3 real post-completion links wired cards the worker created; its own handoff is untouched."""
    from tools import kanban_tools as kt
    _dependency_park(worker_env)
    a, b = _new_task("created a"), _new_task("created b")
    out = json.loads(kt._handle_link({"parent_id": a, "child_id": b}))
    assert out.get("ok") is True, out


def test_parked_worker_may_not_link_its_own_card_as_a_parent(worker_env):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt
    _dependency_park(worker_env)
    other = _new_task("downstream")
    out = json.loads(kt._handle_link({"parent_id": worker_env, "child_id": other}))
    _assert_run_already_ended(out, _run_id(), "todo")
    with _conn() as conn:
        assert kb.parent_ids(conn, other) == []


def test_failed_annotation_keeps_the_committed_success(worker_env, monkeypatch):
    """The park is committed before run_ended is read: a failed read must not turn it into an error."""
    import sqlite3
    from tools import kanban_tools as kt
    from tools import kanban_tools_run_end as kre

    def boom(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(kre, "with_success_fields", boom)
    out = json.loads(kt._handle_schedule({"reason": "SCHEDULED_UNTIL=2099-01-01T00:00:00Z wait", "clock_only": True, "awaiting": {"kind": "clock", "why": "fixture window"}}))
    assert out.get("ok") is True and out["status"] == "scheduled", out
    assert "run_ended" not in out, out
    assert _state(worker_env)[0] == "scheduled"


def test_non_worker_lifecycle_call_skips_the_run_end_reads(worker_env, monkeypatch):
    """Review note: an orchestrator call must not pay two extra board opens for a fence that cannot apply."""
    from tools import kanban_tools as kt
    from tools import kanban_tools_run_end as kre
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID")

    def no_read(*_a, **_k):
        raise AssertionError("run-end read for a non-worker")
    monkeypatch.setattr(kre, "refusal", no_read)
    monkeypatch.setattr(kre, "with_success_fields", no_read)
    out = json.loads(kt._handle_schedule({"task_id": worker_env, "clock_only": True, "awaiting": {"kind": "clock", "why": "fixture window"},
                                          "reason": "SCHEDULED_UNTIL=2099-01-01T00:00:00Z wait"}))
    assert out.get("ok") is True, out
