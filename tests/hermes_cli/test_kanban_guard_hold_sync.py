"""Guard maintenance has one home owner; executor preclaim guards still run.

Real gateway stores, pickup and sync; the Board transport is in-memory.
"""
from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_dispatch_guard_notice as notice
from hermes_cli import kanban_model_assignment as assignment
from hermes_cli import kanban_worker_node as wn
from hermes_cli import kanban_worker_placement as placement
from tests.hermes_cli import test_kanban_worker_node as worker_node

gws = worker_node.gws
svc = worker_node.svc


def held_copies(gws, monkeypatch):
    home, worker = gws
    home.enter()
    target = kb.create_task(home.conn, title="guard hold ownership", assignee="default", runs_on=["linux"])
    now = int(time.time()) + 2
    monkeypatch.setattr(time, "time", lambda: now)
    with kb.write_txn(home.conn):
        kb._append_event(home.conn, target, "respawn_guarded", {"reason": "owner_restart_cooldown"})
    monkeypatch.setattr(time, "time", lambda: now + 4000)
    monkeypatch.setenv("HERMES_KANBAN_GUARD_HOLD_NOTIFY_SECONDS", "1800")
    monkeypatch.setenv("HERMES_KANBAN_GUARD_HOLD_CAP_LONG_SECONDS", "3600")
    home.round()
    worker.round()
    worker.enter()
    assert wn.pickup(worker.conn, budget=1) == [target]
    copied = kb.get_task(worker.conn, target)
    assert copied is not None and copied.status == "ready"
    assert notice.guard_hold_started_at(worker.conn, target) == now
    return target


@pytest.mark.parametrize("home_first", [False, True])
def test_one_notice_and_fix_card_before_sync(gws, svc, monkeypatch, home_first):
    target = held_copies(gws, monkeypatch)
    home, worker = gws
    for gateway in ((home, worker) if home_first else (worker, home)):
        gateway.enter()
        notice.record_guard_hold(gateway.conn, target, "owner_restart_cooldown")
        notice.record_guard_hold(gateway.conn, target, "owner_restart_cooldown")
    # Neither side saw the other's notice while deciding. Only now publish.
    home.round()
    worker.round()
    rows = svc.board(worker_node.BOARD)["rows"]
    notes = [r for r in rows["task_comments"].values()
             if r.get("task_id") == target and r.get("body", "").startswith("[respawn-guard]")]
    fixes = [r for r in rows["tasks"].values() if r.get("idempotency_key") == f"guard-hold:{target}"]
    assert (len(notes), len(fixes)) == (1, 1)
    assert fixes[0]["created_on"] == home.name


@pytest.mark.parametrize("home_first", [False, True])
def test_cap_is_home_owned_but_executor_guard_still_refuses(gws, monkeypatch, home_first):
    target = held_copies(gws, monkeypatch)
    home, worker = gws
    monkeypatch.setattr(assignment, "dispatch_guard", lambda *a, **kw: "owner_restart_cooldown")
    results = {}
    for gateway in ((home, worker) if home_first else (worker, home)):
        gateway.enter()
        result = dispatch.DispatchResult()
        row = gateway.conn.execute("SELECT * FROM tasks WHERE id=?", (target,)).fetchone()
        def no_spawn(*args, **kwargs):
            pytest.fail("guarded copy must not spawn")
        assert not dispatch._dispatch_lane_task(
            gateway.conn, row, "default", result, lane="ready", dry_run=False,
            ttl_seconds=None, board=worker_node.BOARD, failure_limit=2,
            spawn_fn=no_spawn, per_profile_cap=None, per_profile_running={},
        )
        results[gateway.name] = result
    assert results[worker.name].respawn_guarded == [(target, "owner_restart_cooldown")]
    assert results[worker.name].auto_blocked == []
    assert kb.get_task(worker.conn, target).status == "ready"
    assert results[home.name].auto_blocked == [target]
    assert kb.get_task(home.conn, target).status == "triage"
    assert notice.guard_hold_reason(worker.conn, target) == "owner_restart_cooldown"


@pytest.mark.parametrize("reset", [False, True])
def test_pinned_executor_hold_gets_home_maintenance_in_full_tick(gws, svc, monkeypatch, reset):
    home, worker = gws
    home.enter()
    target = kb.create_task(home.conn, title="executor-only guard", assignee="default",
                            runs_on=[f"machine:{worker.name}"])
    home.round()
    worker.round()
    worker.enter()
    assert wn.pickup(worker.conn, budget=1) == [target]
    now = int(time.time()) + 2
    monkeypatch.setattr(time, "time", lambda: now)
    # Executor-origin state, not a mocked guard result, travels home through sync.
    with kb.write_txn(worker.conn):
        worker.conn.execute("UPDATE tasks SET last_failure_error=? WHERE id=?", ("invalid api key", target))
    result = worker.tick()
    assert result.respawn_guarded == [(target, "blocker_auth")]
    assert result.spawned == []
    worker.round()
    home.round()
    assert kb.get_task(home.conn, target).last_failure_error == "invalid api key"
    monkeypatch.setenv("HERMES_KANBAN_GUARD_HOLD_NOTIFY_SECONDS", "1800")
    monkeypatch.setenv("HERMES_KANBAN_GUARD_HOLD_CAP_SECONDS", "7200")
    monkeypatch.setattr(time, "time", lambda: now + 2000)
    if reset:
        worker.enter()
        with kb.write_txn(worker.conn):
            kb._append_event(worker.conn, target, "unblocked", {"reason": "new lifecycle"})
        worker.round()
        home.round()
    dry = home.tick(dry_run=True)
    assert dry.auto_blocked == []
    assert home.conn.execute("SELECT count(*) FROM task_comments WHERE task_id=?", (target,)).fetchone()[0] == 0
    result = home.tick()
    assert (target, "labels_mismatch") in result.skipped_ineligible
    assert all(tid != target for tid, *_ in result.spawned)
    home.round()
    rows = svc.board(worker_node.BOARD)["rows"]
    fixes = [r for r in rows["tasks"].values() if r.get("idempotency_key") == f"guard-hold:{target}"]
    assert len(fixes) == (0 if reset else 1)
    if reset:
        assert kb.get_task(home.conn, target).status == "ready"
        return
    assert fixes[0]["created_on"] == home.name
    monkeypatch.setattr(time, "time", lambda: now + 8000)
    capped = home.tick(max_spawn=0)
    assert capped.auto_blocked == [target]
    assert kb.get_task(home.conn, target).status == "triage"


def test_home_maintains_guard_hold_on_worker_queued_policy_card(gws, svc, monkeypatch):
    """A worker-first card is fenced out of the home's execution lanes (placement-policy refusal),
    so its guard hold is home maintenance: the full tick sweeps it instead of aborting."""
    home, _worker = gws
    home.enter()
    target = kb.create_task(home.conn, title="queued for worker capacity", assignee="default",
                            placement_policy="worker-first-v1")
    assert wn.dispatch_refusals(home.conn, "ready")[target] == "waiting_for_worker_capacity"
    now = int(time.time()) + 2
    monkeypatch.setattr(time, "time", lambda: now)
    monkeypatch.setenv("HERMES_KANBAN_OWNER_RESTART_COOLDOWN_SECONDS", "10000")
    monkeypatch.setenv("HERMES_KANBAN_GUARD_HOLD_CAP_LONG_SECONDS", "3600")
    # Executor-shaped evidence: a restart-reclaimed run and its hold event (synced home in production).
    with kb.write_txn(home.conn):
        home.conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at, metadata) "
            "VALUES (?, 'reclaimed', 'reclaimed', ?, ?, ?)",
            (target, now - 2, now, '{"owner_restart": true}'),
        )
        kb._append_event(home.conn, target, "respawn_guarded", {"reason": "owner_restart_cooldown"})
    monkeypatch.setattr(time, "time", lambda: now + 4000)
    result = home.tick(max_spawn=0)
    assert result.auto_blocked == [target]
    assert result.spawned == []
    task = kb.get_task(home.conn, target)
    assert task is not None and task.status == "triage"
    assert home.conn.execute("SELECT count(*) FROM task_runs WHERE task_id=?", (target,)).fetchone()[0] == 1


@pytest.mark.parametrize("prefilter", [True, False], ids=["both_fences", "claim_gate_only"])
def test_full_tick_with_spawn_budget_never_runs_worker_queued_policy_card(gws, svc, monkeypatch, prefilter):
    """The guard-hold test above ticks with max_spawn=0, where no spawn is trivial. Here the home has budget and
    spends it on a plain control card in the same tick, yet the worker-first card stays queued: no spawn, no run
    row (locally or on the Board) and its policy fence intact. ``claim_gate_only`` drops the advisory prefilter
    (``dispatch_refusals``) so the claim gate's own policy check must hold the line alone."""
    home, _worker = gws
    home.enter()
    target = kb.create_task(home.conn, title="queued for worker capacity", assignee="default",
                            placement_policy="worker-first-v1")
    control = kb.create_task(home.conn, title="ordinary home card", assignee="default")
    # Admit local capacity so the policy fence, not host pressure or fixture shape, is what keeps the card off.
    monkeypatch.setattr(placement, "local_admission", lambda conn: True)
    if not prefilter:
        monkeypatch.setattr(wn, "dispatch_refusals", lambda conn, status: {})
    result = home.tick(max_spawn=4)
    assert [tid for tid, *_ in result.spawned] == [control]  # the budget was real and was spent
    if prefilter:
        assert (target, "waiting_for_worker_capacity") in result.skipped_ineligible
    else:
        assert (target, "waiting_for_worker_capacity") in result.claim_deferred
    task = kb.get_task(home.conn, target)
    assert task is not None and task.status == "ready"
    assert task.placement_policy == "worker-first-v1"
    assert task.claim_lock is None and task.current_run_id is None
    row = home.conn.execute("SELECT runs_on, lease_holder FROM tasks WHERE id=?", (target,)).fetchone()
    assert (row["runs_on"], row["lease_holder"]) == (None, None)
    assert home.conn.execute("SELECT count(*) FROM task_runs WHERE task_id=?", (target,)).fetchone()[0] == 0
    home.round()
    board_runs = [r for r in svc.board(worker_node.BOARD)["rows"].get("task_runs", {}).values()
                  if r.get("task_id") in (target, control)]
    assert [r["task_id"] for r in board_runs] == [control]  # the Board sees runs; none for the queued card
    assert placement.local_refusals(home.conn, "ready") == {target: "waiting_for_worker_capacity"}


@pytest.mark.parametrize("cooldown", [120, 10000])
def test_home_revalidates_synced_cooldown_without_executor_budget(gws, monkeypatch, cooldown):
    home, worker = gws
    home.enter()
    target = kb.create_task(home.conn, title="cooldown evidence", assignee="default",
                            runs_on=[f"machine:{worker.name}"])
    home.round()
    worker.round()
    worker.enter()
    assert wn.pickup(worker.conn, budget=1) == [target]
    now = int(time.time()) + 2
    monkeypatch.setattr(time, "time", lambda: now)
    monkeypatch.setenv("HERMES_KANBAN_OWNER_RESTART_COOLDOWN_SECONDS", str(cooldown))
    monkeypatch.setenv("HERMES_KANBAN_GUARD_HOLD_CAP_LONG_SECONDS", "3600")
    with kb.write_txn(worker.conn):
        worker.conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at, metadata) "
            "VALUES (?, 'reclaimed', 'reclaimed', ?, ?, ?)",
            (target, now - 2, now, '{"owner_restart": true}'),
        )
    assert worker.tick().respawn_guarded == [(target, "owner_restart_cooldown")]
    worker.round()
    home.round()
    assert home.conn.execute("SELECT outcome FROM task_runs WHERE task_id=?", (target,)).fetchone()[0] == "reclaimed"
    monkeypatch.setattr(time, "time", lambda: now + 4000)
    # No executor lane visit after the cooldown: capacity cannot turn old evidence into a hold.
    assert worker.tick(max_spawn=0).spawned == []
    result = home.tick(max_spawn=0)
    assert result.auto_blocked == ([target] if cooldown == 10000 else [])
    task = kb.get_task(home.conn, target)
    assert task is not None and task.status == ("triage" if cooldown == 10000 else "ready")
    assert home.conn.execute("SELECT count(*) FROM task_comments WHERE task_id=? AND body LIKE '[respawn-guard]%'",
                             (target,)).fetchone()[0] == 0
