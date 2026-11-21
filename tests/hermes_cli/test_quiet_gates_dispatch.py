"""Claim-quiet execution gates in the Kanban dispatcher (real dispatch/admission code, fake spawn only).

Automatic NATIVE-LOCAL claims are refused inside ``claim_task``'s admission transaction while a verified
``agent.claim_quiet`` lease is live; operator ``claim_task``, remote capsule destinations and unresolved
destinations are never gated; the dispatcher keeps its normal branch (health publishes, bookkeeping runs).
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent import claim_quiet as cq
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from tests.hermes_cli import test_kanban_capsule_admission as _admission
from tests.hermes_cli.test_kanban_capsule_admission import OWNER, _admitted

env = _admission.env  # capsule fixture (registered remote transport)


@pytest.fixture
def board(tmp_path, monkeypatch):
    """Real default board in a scratch home; native spawns are recorded, never executed."""
    from agent import core_update_drain as drain

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(drain, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(drain, "_canonical_root", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.profiles.list_profiles", lambda **kw: [SimpleNamespace(name="default")])
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda name: name == "default")
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda: "unknown")
    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: False)
    from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml
    from hermes_cli.kanban_model_assignment import _assignee_config
    assert not _assignee_config("default").get("model_routing", {}).get("enabled")
    (tmp_path / "config.yaml").write_text(_managed_yaml())
    assert _assignee_config("default")["model_routing"]["enabled"] is True
    spawned: list[str] = []
    monkeypatch.setattr(kbd, "_default_spawn", lambda task, ws, *, board=None: spawned.append(task.id) or None)
    with kbc.connect_closing(board="default") as conn:
        tid = kb.create_task(conn, title="ready card", assignee="default")
    return SimpleNamespace(home=tmp_path, spawned=spawned, tid=tid)


def _state(tid):
    with kbc.connect_closing(board="default") as conn:
        task = kb.get_task(conn, tid)
        runs = conn.execute("SELECT count(*) FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0]
        events = conn.execute("SELECT count(*) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]
        return (task.status, task.claim_lock, task.current_run_id, task.consecutive_failures,
                task.updated_at if hasattr(task, "updated_at") else None, runs, events)


def _dispatch(**kw):
    with kbc.connect_closing(board="default") as conn:
        return kbd.dispatch_once(conn, board="default", **kw)


def test_automatic_local_claim_deferred_then_resumes(board):
    before = _state(board.tid)
    with cq.acquire(max_s=60, reason="measure") as owner:
        result = _dispatch()
        assert board.spawned == [] and result.spawned == []
        assert result.quiet_deferred == [(board.tid, owner.lease.episode_id)]
        assert result.quiet_deadlines == {owner.lease.episode_id: owner.lease.expires_at}
        assert result.capsule_refused == [] and result.claim_deferred == [] and result.respawn_guarded == []
        assert _state(board.tid) == before  # no claim/run/failure/event/timestamp change
        for _ in range(2):  # repeated ticks stay write-free
            _dispatch()
        assert _state(board.tid) == before and board.spawned == []
    result = _dispatch()  # lease released: the very same card is claimed normally
    assert board.spawned == [board.tid] and result.quiet_deferred == []
    assert _state(board.tid)[0] == "running"


def test_review_lane_claim_deferred_too(board, monkeypatch):
    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: True)
    assert [tid for tid, *_ in _dispatch().spawned] == [board.tid]
    board.spawned.clear()  # observe only the later quiet review attempt
    with kbc.connect_closing(board="default") as conn:
        task = kb.get_task(conn, board.tid)
        assert task.current_run_id is not None
        kb._append_event(conn, board.tid, "worker_result",
                         {"route_model": "claude-sonnet-5-5", "route_provider": "anthropic"},
                         run_id=task.current_run_id)
        assert kb.request_review(conn, board.tid, summary="r", expected_run_id=task.current_run_id) is True
        from hermes_cli.kanban_review_model import implementer_models
        assert implementer_models(conn, kb.get_task(conn, board.tid)) == {"claude-sonnet-5-5"}
    with cq.acquire(max_s=60, reason="measure") as owner:
        result = _dispatch()
    assert board.spawned == [] and result.quiet_deferred == [(board.tid, owner.lease.episode_id)]
    assert _state(board.tid)[0] == "review"


def test_manual_operator_claim_proceeds_under_quiet(board):
    with cq.acquire(max_s=60, reason="measure"), kbc.connect_closing(board="default") as conn:
        claimed = kb.claim_task(conn, board.tid)  # operator/API claim: never quiet-gated
    assert claimed is not None and claimed.status == "running"


def test_expired_lease_does_not_defer(board, monkeypatch):
    from tests.agent.test_claim_quiet import FakeClock
    clock = FakeClock()
    monkeypatch.setattr(cq, "SystemClock", lambda: clock)
    owner = cq.acquire(max_s=60, reason="measure", clock=clock)
    try:
        clock.advance(61)  # deadline passed, nothing released: the reader fails open
        result = _dispatch()
    finally:
        owner.close()
    assert result.quiet_deferred == [] and board.spawned == [board.tid]


def test_unresolved_custom_spawn_is_not_credited(board):
    calls = []
    with cq.acquire(max_s=60, reason="measure"):
        result = _dispatch(spawn_fn=lambda task, ws, board=None: calls.append(task.id))
    assert result.quiet_deferred == [] and calls == [board.tid]  # destination unproven: not gated, not explained


def test_remote_capsule_destination_not_gated(env):
    _handle, tid = _admitted(env)
    with cq.acquire(max_s=60, reason="measure"):
        result = kbd.dispatch_once(env.conn, spawn_fn=env.spawn, dispatcher_id=OWNER)
    assert result.quiet_deferred == [] and [t for t, _a, _w in result.spawned] == [tid]
    assert env.calls and env.calls[0][0] == tid


def test_capacity_evidence_names_quiet_claim_not_update_drain(board):
    from hermes_cli import kanban_capacity_evidence as evidence

    with cq.acquire(max_s=60, reason="measure") as owner:
        result = _dispatch()
        with kbc.connect_closing(board="default") as conn:
            value = evidence.read(conn, kb.get_task(conn, board.tid))
    restrictions = [r for r in value["native_restrictions"] if r["native_code"] == "quiet_claim"]
    assert restrictions == [{"source_field": "quiet_deferred", "native_code": "quiet_claim",
                             "evidence_ref": restrictions[0]["evidence_ref"],
                             "episode_id": owner.lease.episode_id, "deadline_at": owner.lease.expires_at}]
    assert all(r["code"] != "capacity.update_drain" for r in value["reasons"] + value["machine_reasons"])
    assert result.quiet_deferred


def test_stuck_diagnostics_recognize_quiet_narrowly():
    ep = "episode-1"
    quiet = kbd.DispatchResult(quiet_deferred=[("t_a", ep)], quiet_deadlines={ep: 5.0})
    assert kbd.all_quiet_deferred([quiet])
    assert "t_a=quiet_claim(episode-1)" in kbd.stuck_warning(6, [quiet])
    assert "quiet_claim=1" in kbd.describe_suppression([quiet])
    # An unrelated hold/failure/cap on any board keeps the ordinary stuck accounting.
    for other in (kbd.DispatchResult(claim_deferred=[("t_b", "lease_held")]),
                  kbd.DispatchResult(capsule_refused=[("t_b", "capsule_unregistered")]),
                  kbd.DispatchResult(owner_down=[("t_b", "p", "failed")]),
                  kbd.DispatchResult(admission_deferred=["t_b"]),
                  kbd.DispatchResult(at_capacity="max_in_progress=1/1"),
                  kbd.DispatchResult(memory_pressure="critical")):
        assert not kbd.all_quiet_deferred([quiet, other])
    assert not kbd.all_quiet_deferred([kbd.DispatchResult()])
    assert not kbd.all_quiet_deferred([kbd.DispatchResult(quiet_deferred=[("t_a", ep)], spawned=[("t_a", "p", "w")])])
    assert not kbd.all_quiet_deferred([kbd.DispatchResult(quiet_deferred=[("t_a", ep)], skipped_locked=True)])
    assert not kbd.all_quiet_deferred([None])


def _truthy(f) -> Any:
    """A set value for any DispatchResult field: what a real tick would record there."""
    default = f.default_factory() if f.default_factory is not dataclasses.MISSING else f.default
    if isinstance(default, bool):
        return True
    if isinstance(default, int):
        return 1
    if isinstance(default, dict):
        return {"t_b": ("x", 1)}
    if isinstance(default, list):
        return [("t_b", "x")]
    return "x"


def test_every_dispatch_result_field_is_classified_for_quiet():
    """A new DispatchResult field must be placed in QUIET_COMPATIBLE_FIELDS or QUIET_HOLD_FIELDS (#1806 review)."""
    from hermes_cli import kanban_db_dispatch_stuck as stuck
    names = {f.name for f in dataclasses.fields(kbd.DispatchResult)}
    assert not stuck.QUIET_COMPATIBLE_FIELDS & stuck.QUIET_HOLD_FIELDS
    assert names - stuck.QUIET_COMPATIBLE_FIELDS - stuck.QUIET_HOLD_FIELDS == set(), "classify the new field"
    assert (stuck.QUIET_COMPATIBLE_FIELDS | stuck.QUIET_HOLD_FIELDS) - names == set(), "drop the removed field"


def test_quiet_with_any_hold_field_keeps_stuck_accounting():
    ep = "episode-1"
    quiet = kbd.DispatchResult(quiet_deferred=[("t_a", ep)], quiet_deadlines={ep: 5.0})
    # The two holds the hand-kept denylist missed: a non-spawnable lane card and a crashed worker.
    assert not kbd.all_quiet_deferred([quiet, kbd.DispatchResult(skipped_nonspawnable=["t_b"])])
    assert not kbd.all_quiet_deferred([quiet, kbd.DispatchResult(crashed=["t_b"])])
    assert not kbd.all_quiet_deferred([dataclasses.replace(quiet, crashed=["t_b"])])
    from hermes_cli import kanban_db_dispatch_stuck as stuck
    by_name = {f.name: f for f in dataclasses.fields(kbd.DispatchResult)}
    for name in sorted(stuck.QUIET_HOLD_FIELDS):
        row = dataclasses.replace(quiet, **{name: _truthy(by_name[name])})
        assert not kbd.all_quiet_deferred([row]), name
        assert not kbd.all_quiet_deferred([quiet, kbd.DispatchResult(**{name: _truthy(by_name[name])})]), name
    for name in sorted(stuck.QUIET_COMPATIBLE_FIELDS - {"skipped_locked", "skipped_owned"}):
        row = dataclasses.replace(quiet, **{name: _truthy(by_name[name])})
        assert kbd.all_quiet_deferred([row]), name


# Hard-coded on purpose: the loop above takes its expectations from the module's own sets, so moving a hold
# (e.g. clock_held) into QUIET_COMPATIBLE_FIELDS would pass it while quiet then masks that hold (#2570 M1e).
LITERAL_HOLD_FIELDS = (
    "clock_held", "rate_limited", "no_progress_held", "core_update_drain_hold", "at_capacity",
    "spawned", "skipped_unassigned", "skipped_nonspawnable", "skipped_per_profile_capped", "owner_down",
    "skipped_ineligible", "claim_deferred", "admission_deferred", "capsule_refused", "pinned_quota_held",
    "auto_blocked", "respawn_guarded", "crashed", "timed_out", "stale", "reclaimed", "reconciled_orphans",
    "released_leftover_claims", "ownership_error", "memory_pressure",
)


@pytest.mark.parametrize("name", LITERAL_HOLD_FIELDS)
def test_quiet_with_literal_hold_field_keeps_stuck_accounting(name):
    from hermes_cli import kanban_db_dispatch_stuck as stuck
    assert name in stuck.QUIET_HOLD_FIELDS and name not in stuck.QUIET_COMPATIBLE_FIELDS
    ep = "episode-1"
    quiet = kbd.DispatchResult(quiet_deferred=[("t_a", ep)], quiet_deadlines={ep: 5.0})
    field = {f.name: f for f in dataclasses.fields(kbd.DispatchResult)}[name]
    assert kbd.all_quiet_deferred([dataclasses.replace(quiet, **{name: _truthy(field)})]) is False
    assert kbd.all_quiet_deferred([quiet, kbd.DispatchResult(**{name: _truthy(field)})]) is False


def test_non_dataclass_row_fails_closed():
    """A row that is not a DispatchResult dataclass never reads as all-quiet, and never raises (#2570 M1d)."""
    row = SimpleNamespace(quiet_deferred=[("t_a", "episode-1")], quiet_deadlines={"episode-1": 5.0},
                          skipped_locked=False, skipped_owned=False)
    assert kbd.all_quiet_deferred([row]) is False
    quiet = kbd.DispatchResult(quiet_deferred=[("t_a", "episode-1")])
    assert kbd.all_quiet_deferred([quiet, row]) is False
