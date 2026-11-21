"""A clear retires ready-card hold evidence even while capacity prevents admission."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_diagnostics as kd
from hermes_cli import kanban_shared_blockers as ksb
from tests.hermes_cli import test_kanban_shared_blockers as shared

kanban_home = shared.kanban_home
spawns = shared.spawns


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_clear_retires_ready_hold_before_capacity_and_dispatches_next_tick(kanban_home, spawns, monkeypatch, lane):
    _, spawn = spawns
    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: True)
    with kbc.connect_closing() as conn:
        if lane == "review":
            from tests.hermes_cli.test_kanban_model_tier import _review_source
            held = kb.create_task(conn, title="held", assignee="default")
            _review_source(conn, held)
            assert kb.claim_review_task(conn, held) is not None
            assert kb.block_task(conn, held, reason="GitHub Actions refuses jobs: billing",
                                 kind="capability", blocker_id=shared.BID)
        else:
            held = shared._blocked_behind(conn, "held")
        probe = shared._blocked_behind(conn, "probe")
        shared._age_runs(conn, 60)
        kb.unblock_task(conn, held)
        kb.unblock_task(conn, probe)
        kb.claim_task(conn, probe)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (lane, held))
        # Actual dispatcher guard writes the history, not a fabricated diagnostic event.
        first = kbd.dispatch_once(conn, spawn_fn=spawn)
        assert (held, ksb.GUARD_REASON) in first.respawn_guarded, first
        assert not ksb.release_cleared_blockers(conn)
        assert kb.complete_task(conn, probe, result="wall gone")
        busy = kb.create_task(conn, title="unrelated worker", assignee="default")
        kb.claim_task(conn, busy)
        capped = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=1)
        assert capped.at_capacity and not capped.spawned
        assert kb.get_task(conn, held).status == lane
        marks = conn.execute("SELECT * FROM task_events WHERE task_id = ? AND kind = ?",
                             (held, ksb.CLEARED_EVENT)).fetchall()
        assert len(marks) == 1
        events = kb.list_events(conn, held)
        assert kd._ready_hold_reason(kb.get_task(conn, held), events, {}) is None
        assert not ksb.release_cleared_blockers(conn)  # one marker per clear, no tick spam
        assert kb.complete_task(conn, busy, result="slot available")
        admitted = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=1)
        assert [tid for tid, *_ in admitted.spawned] == [held]
        assert not admitted.respawn_guarded


def test_historical_guard_diagnostic_does_not_claim_current_hold():
    task = {"id": "t_history", "status": "ready", "assignee": "default", "created_at": 1}
    events = [{"kind": "respawn_guarded", "created_at": 10,
               "payload": {"reason": "shared_blocker"}}]
    diags = kd.compute_task_diagnostics(task, events, [], now=4000)
    held = next(d for d in diags if d.kind == "held_in_ready")
    assert "last observed" in held.title.lower()
    assert "current" in held.detail.lower() and "not verified" in held.detail.lower()
    assert "not stranded" not in held.detail.lower()


@pytest.mark.parametrize("reverse", [False, True])
def test_same_second_clear_outranks_guard_regardless_of_list_order(reverse):
    events = [{"id": 11, "created_at": 100, "kind": "respawn_guarded"},
              {"id": 12, "created_at": 100, "kind": ksb.CLEARED_EVENT}]
    if reverse:
        events.reverse()
    assert kd._ready_hold_reason({}, events, {}) is None
    # Later assertion is still a real hold; a historical imported high id cannot win.
    events.append({"id": 1, "created_at": 101, "kind": "respawn_guarded"})
    assert kd._ready_hold_reason({}, events, {}) == "respawn_guarded"
