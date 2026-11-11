"""Quota post-feed receipts survive wall I/O failure and later ended runs."""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as dispatch
from hermes_cli import kanban_neutral_release as neutral, kanban_quota_release as quota
from tests.hermes_cli import test_kanban_failure_transport as support

live = support.live


def queued(live):
    tid, rid = support._running(live)
    dead = dispatch._DeadWorker('rate_limited', 1, 'quota', 'rate_limited',
                                {'retry_status': 'ready'}, rate_limited=True)
    dead.run_id = rid
    with kbc.write_txn(live):
        row = live.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
        assert neutral.queue(live, row, dead)
        live.execute("UPDATE task_runs SET ended_at=10000,outcome='rate_limited' WHERE id=?", (rid,))
        kb._append_event(live, tid, 'rate_limited', {}, run_id=rid)
        kb._append_event(live, tid, 'run_route', {'provider': 'fixture', 'model': 'fixture'}, run_id=rid)
    return tid, rid


def test_wall_failure_keeps_task_gated_then_retries_fixed_deadline(live, monkeypatch, tmp_path):
    monkeypatch.setattr(kb, '_resolve_rate_limit_cooldown_seconds', lambda: 3600)
    monkeypatch.setattr(quota.time, 'time', lambda: 10000)
    tid, _rid = queued(live)
    original = quota.persist_wall
    def fail(*a, **kw):
        raise OSError('fixture wall write failure')
    monkeypatch.setattr(quota, 'persist_wall', fail)
    assert neutral.drain(live) == []
    assert quota.pending_task(live, tid)
    for lane in ('ready', 'review'):
        assert dispatch.check_respawn_guard(live, tid, lane=lane) == 'rate_limit_cooldown'
    state = json.loads(live.execute('SELECT state FROM kanban_quota_effects').fetchone()[0])
    assert state['until'] == 13600
    assert live.execute("SELECT COUNT(*) FROM task_events WHERE kind='quota_episode'").fetchone()[0] == 1
    monkeypatch.setattr(quota.time, 'time', lambda: 11000)
    path = tmp_path / 'walls.json'
    monkeypatch.setattr(quota, 'persist_wall', lambda p, until: original(p, until, path=path))
    assert len(neutral.drain(live)) == 1
    assert json.loads(path.read_text()) == {'fixture': 13600}
    assert not quota.pending_task(live, tid)
    assert neutral.drain(live) == []
    assert live.execute("SELECT COUNT(*) FROM task_events WHERE kind='quota_episode'").fetchone()[0] == 1


def test_expired_wall_does_not_retry_io(monkeypatch):
    monkeypatch.setattr(quota.time, 'time', lambda: 20000)
    monkeypatch.setattr(quota, 'persist_wall', lambda *a: pytest.fail('expired write'))
    quota.finish({'write_wall': True, 'provider': 'fixture', 'until': 19000})


def test_delayed_quota_effect_is_anchored_to_captured_run(live, monkeypatch):
    monkeypatch.setattr(quota.time, 'time', lambda: 10000)
    tid, rid = queued(live)
    with kbc.write_txn(live):
        live.execute("INSERT INTO task_runs(task_id,started_at,ended_at,outcome,status) VALUES (?,10001,10002,'completed','done')", (tid,))
    assert len(neutral.drain(live)) == 1
    event = live.execute("SELECT run_id,payload FROM task_events WHERE kind='quota_episode'").fetchone()
    assert event['run_id'] == rid
    assert json.loads(event['payload'])['count'] == 1
    assert not quota.pending_task(live, tid)


def test_captured_episode_foreign_and_unended_controls(live):
    from hermes_cli.kanban_quota_episode import episode
    tid, rid = support._running(live)
    assert episode(live, tid, run_id=rid) is None
    assert episode(live, 'foreign', run_id=rid) is None
