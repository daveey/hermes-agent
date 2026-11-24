"""Real serial dispatch must service timers between slow phases, not next tick."""
import time
import pytest

from hermes_cli import kanban_continuation as continuation
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from tests.hermes_cli import test_kanban_sweep_gate as helpers

home = helpers.home


@pytest.fixture(autouse=True)
def timer_only_external_isolation(monkeypatch):
    # Exercise the real timer path, not unrelated asynchronous GitHub probes or
    # credential-backed pool refresh. Neither may touch the network in this test.
    from hermes_cli import kanban_ci_progress as ci
    from hermes_cli import kanban_llm_pacing_dispatch as pacing
    from hermes_cli import kanban_wake_check as checks
    monkeypatch.setattr(ci, 'submit_sweep', lambda *a, **k: None)
    monkeypatch.setattr(pacing, 'refresh_pools', lambda *a, **k: None)
    yield
    checks._shutdown_for_tests()


@pytest.mark.parametrize('phase', ['maintenance', 'reclaim', 'capacity'])
def test_wakes_between_slow_phases_even_at_capacity(home, monkeypatch, phase):
    from hermes_cli import kanban_quota_detector as quota

    clock = [time.time()]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    with kbc.connect_closing() as conn:
        task = kb.create_task(conn, title='CI timer', assignee='worker')
        kb.claim_task(conn, task)
        parked = continuation.continue_task(
            conn, task, checkpoint='CI pending', resume_after_seconds=600,
            awaiting={'kind': 'ci', 'ref': 'https://github.com/o/r/actions/runs/1',
                      'why': 'pending checks'})
        assert parked['ok']
        assert continuation.pending_wake(conn, task)['wake_at'] == int(clock[0]) + 600
        seen = []

        def slow_guard(*args, **kwargs):
            assert kb.get_task(conn, task).status == 'scheduled'
            clock[0] += 601

        def next_phase(*args, **kwargs):
            seen.append(kb.get_task(conn, task).status)

        if phase == 'maintenance':
            monkeypatch.setattr(dispatch._guard_notice, 'sweep_remote_guard_holds', slow_guard)
            monkeypatch.setattr(quota, 'drain_owner', next_phase)
        elif phase == 'reclaim':
            monkeypatch.setattr(dispatch, 'reap_worker_zombies', slow_guard)
            monkeypatch.setattr(dispatch, 'reap_terminal_workers', lambda *a: next_phase() or [])
        else:
            budget = dispatch._tick_spawn_budget
            def slow_budget(*args, **kwargs):
                slow_guard()
                return budget(*args, **kwargs)
            monkeypatch.setattr(dispatch, '_tick_spawn_budget', slow_budget)
            monkeypatch.setattr(dispatch._kb._sync, 'worker_pickup_capped',
                                lambda conn, result, *a: next_phase() or result)
        result = dispatch.dispatch_once(conn, spawn_fn=lambda *a, **k: None, max_spawn=0)
        assert seen == ['ready'], 'the next serial phase must not postpone an overdue timer'
        assert result.woken_continuations == [task]
        assert result.spawned == []
        assert len([e for e in kb.list_events(conn, task) if e.kind == 'continuation_woken']) == 1


def test_wakes_before_next_candidate_without_spawning_or_resetting_budget(home, monkeypatch):
    from hermes_cli import kanban_db_dispatch_preclaim as preclaim
    from hermes_cli import kanban_hub_admission_claim as admission
    from hermes_cli import kanban_dispatch_priority as priority

    clock = [time.time()]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    with kbc.connect_closing() as conn:
        parked = kb.create_task(conn, title='timer', assignee='worker')
        kb.claim_task(conn, parked)
        assert continuation.continue_task(
            conn, parked, checkpoint='wait', resume_after_seconds=600,
            awaiting={'kind': 'ci', 'ref': 'https://github.com/o/r/actions/runs/2',
                      'why': 'pending'})['ok']
        candidates = [kb.create_task(conn, title=f'candidate {n}', assignee='worker') for n in range(2)]
        rows = [conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone() for tid in candidates]
        monkeypatch.setattr(admission, 'screened_lanes', lambda *a: (rows, [], ()))
        monkeypatch.setattr(preclaim, 'eligible_rows', lambda conn, rows, *a: rows)
        monkeypatch.setattr(priority, 'order_with_remote_waiters', lambda conn, rows: rows)
        seen = []

        def slow_candidate(conn, row, *args, **kwargs):
            seen.append((row['id'], kb.get_task(conn, parked).status))
            if row['id'] == candidates[0]:
                clock[0] += 601
            return False

        monkeypatch.setattr(dispatch, '_dispatch_lane_task', slow_candidate)
        result = dispatch.dispatch_once(conn, spawn_fn=lambda *a, **k: None, max_spawn=1)
        assert seen == [(candidates[0], 'scheduled'), (candidates[1], 'ready')]
        assert result.woken_continuations == [parked]
        assert not result.spawned
        assert conn.execute('SELECT count(*) FROM task_runs').fetchone()[0] == 1


def test_checkpoint_rate_limit_dry_run_and_transaction_boundary(home, monkeypatch):
    from hermes_cli.kanban_dispatch_wake_protection import WakeProtection

    clock = [100.0]
    calls = []
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(continuation, 'wake_due_continuations', lambda conn: calls.append(clock[0]) or [])
    with kbc.connect_closing() as conn:
        guard = WakeProtection(conn, dispatch.DispatchResult())
        guard.checkpoint()
        for _ in range(100):
            guard.checkpoint()
        assert calls == [100.0]
        clock[0] = 105.0
        with kb.write_txn(conn):
            guard.checkpoint()
            assert calls == [100.0]
        guard.checkpoint()
        assert calls == [100.0, 105.0]
        clock[0] = 200.0
        WakeProtection(conn, dispatch.DispatchResult(), dry_run=True).checkpoint()
        assert calls == [100.0, 105.0]
