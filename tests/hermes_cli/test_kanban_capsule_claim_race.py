"""Capsule admission is rechecked inside the claim transaction, not only before it.

A row (or the registration/owner) that changes between ``_dispatch_lane_task``'s pre-claim admission and
``claim_task``/``claim_review_task`` must still be refused before any claim, run, worktree, spawn or
failure increment. The mutation is injected deterministically through a second real connection to the
same native temp board, at the last point before the claim (``kanban_gateway_drain.dispatch_paused``'s
per-row call); no admission validation is replaced. An owner change cannot land mid-tick (``set_dispatch_owner``
waits for the tick lock), so it is covered by the pre-claim tests in test_kanban_capsule_admission.py.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_gateway_drain as drain
from hermes_cli.kanban_capabilities import CapabilityMismatchError
from tests.hermes_cli.test_kanban_capsule_admission import OWNER, _admitted, _admitted_review, _untouched
from tests.hermes_cli import test_kanban_capsule_admission as _admission

env = _admission.env  # shared fixture


def _mutate_before_claim(monkeypatch, mutate):
    """Run ``mutate`` once, after the pre-claim admission passed and immediately before the claim."""
    real = drain.dispatch_paused
    fired = []

    def paused(home=None):
        if home is not None and not fired:  # claim_if_admitting's per-row call, not the tick-level one
            fired.append(True)
            mutate()
        return real(home)

    monkeypatch.setattr(drain, "dispatch_paused", paused)
    return fired


def _set_column(env, tid, column, value):
    other = kbc.connect(env.db)  # a separate writer on the same board, committed before the claim
    try:
        with kb.write_txn(other):
            other.execute(f"UPDATE tasks SET {column} = ? WHERE id = ?", (value, tid))
    finally:
        other.close()


def _refusal_events(env, tid):
    return [r[0] for r in env.conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'capsule_admission_refused'", (tid,))]


def _mutations(env, handle, tid):
    escape = env.tmp / "escape" / tid
    return {
        "workspace": (lambda: _set_column(env, tid, "workspace_path", str(escape)), "capsule_workspace_mismatch",
                      escape),
        "assignee": (lambda: _set_column(env, tid, "assignee", "other"), "capsule_profile_mismatch", None),
        "project": (lambda: _set_column(env, tid, "project_id", env.other_project.id), "capsule_project_mismatch",
                    None),
        "closed": (handle.close, "capsule_unregistered", None),
    }


def _assert_refused_without_effects(env, tid, result, code, status, escape):
    assert result.spawned == []
    assert result.capsule_refused == [(tid, code)]
    assert env.calls == []
    _untouched(env, tid, status=status)
    assert not (env.repo / ".worktrees" / tid).exists()
    if escape is not None:
        assert not escape.exists()
    payloads = _refusal_events(env, tid)
    assert len(payloads) == 1 and code in payloads[0]


@pytest.mark.parametrize("case", ["workspace", "assignee", "project", "closed"])
def test_ready_row_mutated_between_admission_and_claim_is_refused(env, monkeypatch, case):
    handle, tid = _admitted(env)
    mutate, code, escape = _mutations(env, handle, tid)[case]
    fired = _mutate_before_claim(monkeypatch, mutate)
    result = kbd.dispatch_once(env.conn, spawn_fn=env.spawn, dispatcher_id=OWNER)
    assert fired == [True]
    _assert_refused_without_effects(env, tid, result, code, "ready", escape)


@pytest.mark.parametrize("case", ["workspace", "assignee", "project", "closed"])
def test_claim_time_capsule_refusal_is_not_also_reported_as_board_deferral(env, monkeypatch, case):
    handle, tid = _admitted(env)
    mutate, code, _escape = _mutations(env, handle, tid)[case]
    _mutate_before_claim(monkeypatch, mutate)
    result = kbd.dispatch_once(env.conn, spawn_fn=env.spawn, dispatcher_id=OWNER)
    assert result.capsule_refused == [(tid, code)]
    assert [d for d in result.claim_deferred if d[0] == tid] == []  # no 'claim_not_acquired' shadow


@pytest.mark.parametrize("case", ["workspace", "assignee", "closed"])
def test_review_row_mutated_between_admission_and_claim_is_refused(env, monkeypatch, case):
    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: True)
    handle, tid = _admitted_review(env)
    mutate, code, escape = _mutations(env, handle, tid)[case]
    fired = _mutate_before_claim(monkeypatch, mutate)
    result = kbd.dispatch_once(env.conn, spawn_fn=env.spawn, dispatcher_id=OWNER)
    assert fired == [True]
    _assert_refused_without_effects(env, tid, result, code, "review", escape)


def test_unmutated_row_still_claims_through_the_recheck(env, monkeypatch):
    _, tid = _admitted(env)
    fired = _mutate_before_claim(monkeypatch, lambda: None)
    result = kbd.dispatch_once(env.conn, spawn_fn=env.spawn, dispatcher_id=OWNER)
    assert fired == [True]
    assert [t for t, _a, _w in result.spawned] == [tid]
    assert result.capsule_refused == []
    assert env.calls == [(tid, str(env.repo / ".worktrees" / tid))]


def test_review_lane_dispatches_registered_capsule(env, monkeypatch):
    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: True)
    _, tid = _admitted_review(env)
    result = kbd.dispatch_once(env.conn, spawn_fn=env.spawn, dispatcher_id=OWNER)
    assert [t for t, _a, _w in result.spawned] == [tid]
    assert result.capsule_refused == []
    assert kb.get_task(env.conn, tid).status == "running"
    from hermes_cli.kanban_review_model import _routable_models, implementer_models
    task = kb.get_task(env.conn, tid)
    model, provider = env.routes[tid]
    assert model in _routable_models(task.assignee)
    assert model not in implementer_models(env.conn, task)
    assert provider in {"anthropic", "openai-codex"}
    assert (task.model_override, task.provider_override) == (None, None)


def test_review_reassignment_cannot_carry_host_repo(env):
    _, tid = _admitted(env)
    for reviewer in ("builder", "other"):  # registered or not: no create scope, no attestation
        with pytest.raises(CapabilityMismatchError) as err:
            kb.request_review(env.conn, tid, reviewer=reviewer)
        assert "host_repo" in list(err.value.missing)
    task = kb.get_task(env.conn, tid)
    assert task.status == "ready" and task.assignee == "builder"


def test_symlinked_worktrees_root_is_refused_before_effects(env):
    _, tid = _admitted(env)
    elsewhere = env.tmp / "elsewhere-worktrees"
    elsewhere.mkdir()
    (env.repo / ".worktrees").symlink_to(elsewhere, target_is_directory=True)
    result = kbd.dispatch_once(env.conn, spawn_fn=env.spawn, dispatcher_id=OWNER)
    assert result.capsule_refused == [(tid, "capsule_workspace_mismatch")]
    assert env.calls == []
    assert list(elsewhere.iterdir()) == []
    task = kb.get_task(env.conn, tid)
    assert task.status == "ready" and task.claim_lock is None and task.current_run_id is None
    assert task.consecutive_failures == 0


def test_symlink_swapped_in_before_claim_is_refused(env, monkeypatch):
    _, tid = _admitted(env)
    elsewhere = env.tmp / "elsewhere-worktrees"
    elsewhere.mkdir()
    fired = _mutate_before_claim(
        monkeypatch, lambda: (env.repo / ".worktrees").symlink_to(elsewhere, target_is_directory=True))
    result = kbd.dispatch_once(env.conn, spawn_fn=env.spawn, dispatcher_id=OWNER)
    assert fired == [True]
    assert result.capsule_refused == [(tid, "capsule_workspace_mismatch")]
    assert env.calls == [] and list(elsewhere.iterdir()) == []
    assert Path(env.repo / ".worktrees").is_symlink()
