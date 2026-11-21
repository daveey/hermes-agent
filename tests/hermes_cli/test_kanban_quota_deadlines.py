"""Quota deadlines survive real worker receipts and dead-submitter reaping."""
import json
import os
import time

import pytest

from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
from gateway import session_kanban
from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch
from hermes_cli.kanban_quota_receipt import quota_fields
from pathlib import Path
from tests.hermes_cli.test_kanban_managed_result_pid import _dead_pid


@pytest.fixture
def conn(tmp_path, monkeypatch):
    from hermes_cli import kanban_db_connect
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_CRASH_GRACE_SECONDS', '0')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    dispatch._recent_worker_exits.clear()
    kb.init_db()
    with kanban_db_connect.connect() as connection:
        yield connection


def _claim(conn, lane, *, authored=False):
    assert str(kb.kanban_db_path()).startswith(str(kb.kanban_home()))
    tid = kb.create_task(conn, title='quota', assignee='w')
    if lane == 'review':
        if authored:
            from tests.hermes_cli.test_kanban_model_tier import _review_source
            _review_source(conn, tid)
        else:
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
            conn.commit()
    host = kb._claimer_id().split(':', 1)[0]
    claim = kb.claim_review_task if lane == 'review' else kb.claim_task
    task = claim(conn, tid, claimer=f'{host}:owner')
    kb._append_event(conn, tid, 'run_route', {'lane': lane, 'model': 'class:deep'},
                     run_id=task.current_run_id)
    dispatch._set_worker_pid(conn, tid, _dead_pid())
    with kb.write_txn(conn):
        kb._append_event(conn, tid, 'worker_bound', {'pid': os.getpid(), 'claim_lock': task.claim_lock,
                         'started_at': 'x'}, run_id=task.current_run_id)
    context = {'db': str(kb.kanban_db_path()), 'task_id': tid, 'run_id': task.current_run_id,
               'claim_lock': task.claim_lock}
    return tid, task, {'policy': {'kanban_json': json.dumps(context)}}


@pytest.mark.parametrize('lane', ['ready', 'review'])
@pytest.mark.parametrize('reason', ['rate_limit', 'billing', 'upstream_rate_limit'])
def test_turn_reset_survives_real_reap(conn, monkeypatch, lane, reason):
    now = int(time.time())
    reset = now + 7200
    tid, task, frame = _claim(conn, lane)
    result = {'failed': True, 'completed': False, 'failure_reason': reason,
              'failure_retryable': True, 'failure_resets_at': reset}
    monkeypatch.setattr(session_kanban, '_run_task_turns', lambda *_: result)
    session_kanban._run_worker_turns(object(), frame, [])
    assert tid not in dispatch.detect_crashed_workers(conn)
    run = conn.execute('SELECT outcome FROM task_runs WHERE id=?', (task.current_run_id,)).fetchone()
    assert run[0] == 'rate_limited'
    row = kb.get_task(conn, tid)
    assert row.status == lane
    assert not row.consecutive_failures
    monkeypatch.setattr(dispatch.time, 'time', lambda: now + 600)
    assert dispatch.check_respawn_guard(conn, tid, lane=lane) == 'subscription_quota_reset'
    monkeypatch.setattr(dispatch.time, 'time', lambda: reset + 1)
    assert dispatch.check_respawn_guard(conn, tid, lane=lane) is None


@pytest.mark.parametrize('reason', ['billing', 'upstream_rate_limit', 'rate_limit', 'auth'])
@pytest.mark.parametrize('reset', [None, True, float('nan'), float('inf'), '123'])
def test_no_unproven_quota_deadline(reason, reset):
    assert quota_fields({'failure_reason': reason, 'failure_resets_at': reset}) == {}


@pytest.mark.parametrize('lane', ['ready', 'review'])
@pytest.mark.parametrize('model', ['claude-opus-5-5', 'claude-sonnet-5-5'])
def test_wrapped_startup_quota_retains_lane_and_retry_budget(conn, monkeypatch, lane, model):
    from agent.managed_worker import _construct_agent
    from hermes_cli.kanban_model_tier import _prior_failure, resolve_model_tier
    from tests.hermes_cli.test_kanban_model_tier import _write_cfg
    home = kb.kanban_home()
    _write_cfg(home)
    profile = home / 'profiles' / 'w'
    profile.mkdir(parents=True)
    _write_cfg(profile)
    now = int(time.time())
    tid, task, frame = _claim(conn, lane, authored=True)
    if lane == 'review':
        assert resolve_model_tier(conn, task, lane=lane) == (
            'claude-opus-5-5', 'anthropic', 'review_unpinned')
    def factory(**kwargs):
        raise RuntimeError('init') from SubscriptionQuotaUnavailable('anthropic', model, now + 7200)
    monkeypatch.setattr('gateway.session_worker_construct.worker_route', lambda *_: {})
    with pytest.raises(RuntimeError) as caught:
        _construct_agent(frame, factory, None)
    session_kanban.record_worker_failure(frame, caught.value)
    assert tid not in dispatch.detect_crashed_workers(conn)
    row = kb.get_task(conn, tid)
    assert row.status == lane
    assert not row.consecutive_failures
    # Classifying the outcome must not require allocating a route on the now-walled pool.
    assert _prior_failure(conn, tid, row.current_run_id) is None
    if lane == 'ready':
        tier = resolve_model_tier(conn, row, lane=lane, cfg={'default_model': 'class:standard'})
        assert not tier[2].startswith('retry_after')
    monkeypatch.setattr(dispatch.time, 'time', lambda: now + 600)
    assert dispatch.check_respawn_guard(conn, tid, lane=lane) == 'subscription_quota_reset'
    # A later triage sweep cannot turn quota recovery into a new crash/review run.
    monkeypatch.setattr(dispatch.time, 'time', lambda: now + 4000)
    assert dispatch.sweep_stale_triage(conn) == []
    assert kb.get_task(conn, tid).status == lane
    assert dispatch.check_respawn_guard(conn, tid, lane=lane) == 'subscription_quota_reset'


@pytest.mark.parametrize('entry', ['pinned', 'pattern', 'retry', 'standard'])
def test_tier_selected_quota_does_not_consume_crash_budget(conn, monkeypatch, entry):
    from hermes_cli.kanban_model_tier import apply_model_tier
    now = int(time.time())
    tid, task, frame = _claim(conn, 'ready')
    cfg = {'default_model': 'class:standard', 'strong_model': 'class:deep',
           'default_model_keep_patterns': ['security']}
    # Isolated enabled class routes: admission only selects a class the host can route.
    route = dict(provider='anthropic', billing='subscription', weight=1, eligible=True, funding_verified=True,
                 capabilities={'tools': True, 'streaming': True})
    routing_cfg = {'model_routing': {'enabled': True, 'routes': {
        'standard': [dict(route, model='claude-sonnet-5-5')], 'deep': [dict(route, model='claude-opus-5-5')]}}}
    monkeypatch.setattr('hermes_cli.config_effective.load_user_config_effective', lambda *a, **k: routing_cfg)
    if entry == 'pinned':
        # Historical raw selector seed: ignored, the neutral default decides.
        task.model_override, task.provider_override = 'claude-opus-5-5', 'anthropic'
    elif entry == 'pattern':
        task.title = 'security review'
    elif entry == 'retry':
        # A real pre-existing failed attempt selects deep, not the new quota attempt.
        with kb.write_txn(conn):
            conn.execute("INSERT INTO task_runs (task_id, profile, started_at, ended_at, outcome, status) "
                         "VALUES (?, 'w', ?, ?, 'crashed', 'crashed')", (tid, now - 20, now - 10))
    reason = apply_model_tier(conn, task, lane='ready', cfg=cfg)
    expected = {'pinned': 'default', 'pattern': 'pattern:security',
                'retry': 'retry_after_run_crashed', 'standard': 'default'}
    assert reason == expected[entry]
    assert task.model_override == ('class:deep' if entry in {'pattern', 'retry'} else 'class:standard')
    session_kanban.record_start_failure(
        frame, SubscriptionQuotaUnavailable('anthropic', 'claude-sonnet-5-5', now + 7200))
    assert tid not in dispatch.detect_crashed_workers(conn)
    row = kb.get_task(conn, tid)
    assert row.status == 'ready' and not row.consecutive_failures
    assert conn.execute('SELECT outcome FROM task_runs WHERE id=?',
                        (task.current_run_id,)).fetchone()[0] == 'rate_limited'
    monkeypatch.setattr(dispatch.time, 'time', lambda: now + 600)
    assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'


def test_codex_startup_known_retry_deadline(conn, monkeypatch):
    from hermes_cli.auth_codex import _codex_quota_exhausted_error
    now = int(time.time())
    monkeypatch.setattr(time, 'time', lambda: now)
    tid, _task, frame = _claim(conn, 'ready')
    exc = _codex_quota_exhausted_error(7200)
    session_kanban.record_start_failure(frame, exc)
    assert tid not in dispatch.detect_crashed_workers(conn)
    monkeypatch.setattr(time, 'time', lambda: now + 600)
    assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'
    monkeypatch.setattr(time, 'time', lambda: now + 7201)
    assert dispatch.check_respawn_guard(conn, tid) is None


@pytest.mark.parametrize('retry', [None, 0, -1, True, float('inf'), float('nan')])
def test_auth_without_valid_quota_wait_has_no_deadline(retry):
    from hermes_cli.auth import AuthError
    from hermes_cli.auth_constants import CODEX_RATE_LIMITED_CODE
    assert quota_fields(AuthError('quota', code=CODEX_RATE_LIMITED_CODE, retry_after=retry)) == {}
    assert quota_fields(AuthError('login', code='invalid_grant', retry_after=7200)) == {}
