"""Losing neutral receipts retire even before the full alarm event feed arrives."""
import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as dispatch
from hermes_cli import kanban_neutral_release as neutral
from tests.hermes_cli import test_kanban_failure_transport as support
from tests.hermes_cli import test_kanban_quota_release as quota_support

live = support.live


@pytest.mark.parametrize('reason,outcome', [('rate_limited', 'rate_limited'), ('owner_restart', 'reclaimed')])
def test_alarm_winner_retires_receipt(live, reason, outcome):
    tid, rid = support._running(live)
    dead = dispatch._DeadWorker(reason, 1, 'neutral',
                                'owner_restart_lost' if reason == 'owner_restart' else reason,
                                {'retry_status': 'ready'}, **{reason: True})
    dead.run_id = rid
    assert dead.run_outcome == outcome
    with kbc.write_txn(live):
        row = live.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
        assert neutral.queue(live, row, dead)
        live.execute("UPDATE task_runs SET ended_at=2,outcome='reclaimed' WHERE id=?", (rid,))
    assert neutral.drain(live) == []
    if reason == 'rate_limited':
        # No event yet: the outcome mismatch alone is sufficient authoritative evidence.
        assert not neutral.pending(live)
    else:
        assert neutral.pending(live)
        with kbc.write_txn(live):
            kb._append_event(live, tid, 'reclaimed', {}, run_id=rid)
        assert neutral.drain(live) == []
        assert not neutral.pending(live)
    statements = []
    live.set_trace_callback(statements.append)
    try:
        assert neutral.drain(live) == []
    finally:
        live.set_trace_callback(None)
    assert not any(s.upper().startswith('BEGIN') for s in statements)


def test_idle_reclaim_phase_drains_ended_quota(live, monkeypatch):
    from hermes_cli import kanban_quota_release as quota
    monkeypatch.setattr(quota.time, 'time', lambda: 10000)
    tid, _ = quota_support.queued(live)
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET status='ready',worker_pid=NULL,current_run_id=NULL WHERE id=?", (tid,))
    assert not live.execute("SELECT 1 FROM tasks WHERE status='running'").fetchone()
    assert neutral.pending(live)
    # Keep host/process maintenance outside this fixture; exercise the real idle gate and drain.
    monkeypatch.setattr(dispatch, 'reap_worker_zombies', lambda: None)
    monkeypatch.setattr(dispatch, 'reap_terminal_workers', lambda conn: [])
    monkeypatch.setattr(dispatch._worker_scope, 'stopped_scope_units', lambda *a, **kw: [])
    monkeypatch.setattr(kb, '_kanban_observer_consumed', lambda *a: True)
    monkeypatch.setattr(kb, '_fire_kanban_lifecycle_hook', lambda *a, **kw: None)
    dispatch._run_reclaim_phase(live, dispatch.DispatchResult(), stale_timeout_seconds=900,
                                failure_limit=2, reconcile_orphans=False, dry_run=True)
    assert not neutral.pending(live)
    assert live.execute("SELECT COUNT(*) FROM task_events WHERE kind='quota_episode'").fetchone()[0] == 1
