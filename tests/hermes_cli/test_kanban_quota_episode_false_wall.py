"""A hinted episode wall the live OAuth pool now disproves stops holding the card.

worker-3, 2026-11-12 11:32: three healthy anthropic accounts were 401-benched for minutes, so
``quota_unavailable`` judged the pool from four walled accounts only and the workers exited with
``SubscriptionQuotaUnavailable ... resets at <5 days away>``. Each card stored that deadline in its
``quota_episode`` and kept being held (``subscription_quota_reset``) although anthropic was serving.
"""
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_quota_episode as quota
from tests.hermes_cli.test_kanban_quota_episode import board as board  # noqa: PLC0414 -- the self-alias suppresses F811 for the pytest fixture parameter

NOW = 1_800_000_000
MODEL = 'claude-opus-5-5'


def _pool(tmp_path, monkeypatch, kind):
    from agent.credential_pool import PooledCredential
    from hermes_cli.auth import write_credential_pool

    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_SHARED_AUTH_DIR', str(tmp_path / 'shared'))
    rows = []
    for i in range(3):
        row: dict[str, object] = dict(id=f'acct{i}', label=f'account{i}', source='manual:device_code', auth_type='oauth',
                   access_token=f'sk-ant-oat01-fixture-{i}', refresh_token=f'fixture-refresh-{i}', priority=i)
        if kind == 'walled' or (kind == 'serving' and i):
            row['model_cooldowns'] = {MODEL: NOW + (i + 1) * 86400}
        rows.append(PooledCredential.from_dict('anthropic', row).to_dict())
    write_credential_pool('anthropic', rows if kind != 'empty' else [])


def _false_wall(conn, tid, model):
    task = kb.claim_task(conn, tid, claimer=kb._claimer_id())
    assert task is not None
    conn.execute('UPDATE tasks SET worker_pid=? WHERE id=?', (71001, tid))
    concrete = not model.startswith('class:')
    kb._append_event(conn, tid, 'run_route', {'model': model, 'provider': 'anthropic' if concrete else None,
                     'source': 'pin' if concrete else 'class', 'tier_reason': 'default', 'lane': 'ready'},
                     run_id=task.current_run_id)
    reset = NOW + 5 * 86400
    kb._append_event(conn, tid, 'worker_result', {
        'pid': 71001, 'claim_lock': task.claim_lock, 'exit_code': 75,
        'last_output': f'SubscriptionQuotaUnavailable: anthropic/{MODEL}; resets at {reset}',
        'failure_reason': 'rate_limit', 'failure_retryable': True, 'failure_resets_at': float(reset),
        'route_provider': 'anthropic', 'route_model': MODEL}, run_id=task.current_run_id)
    conn.commit()
    dispatch._record_worker_exit(71001, 75 << 8)
    dispatch.detect_crashed_workers(conn)
    assert kb.get_task(conn, tid).status == 'ready'
    return reset


@pytest.mark.parametrize('model', [MODEL, 'class:deep'])
@pytest.mark.parametrize('kind,provider_wall,held', [
    ('serving', 'reaped', False),       # the file exactly as this host's own reap left it
    ('serving', 'independent', True),   # a later provider-wide wall from another source still holds
    ('walled', 'reaped', True),
    ('empty', 'reaped', True),          # an empty or unreadable pool proves nothing
])
def test_hinted_episode_wall_holds_only_while_the_live_pool_confirms_it(
        board, tmp_path, monkeypatch, model, kind, provider_wall, held):
    from hermes_cli import kanban_provider_walls as pw

    monkeypatch.setattr(kb.time, 'time', lambda: NOW)
    monkeypatch.setattr(pw.time, 'time', lambda: NOW)
    monkeypatch.setenv('HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS', '300')
    _pool(tmp_path, monkeypatch, kind)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='false wall', assignee='a')
        reset = _false_wall(conn, tid, model)
        state = quota.episode(conn, tid)
        assert state is not None
        assert state['hinted'] and state['providers'] == ['anthropic'] and state['until'] == reset
        if kind == 'serving':
            # The reap never mirrors a hint the live pool disproves into the provider-wide file.
            assert 'anthropic' not in pw.active_walls(now=NOW)
        if provider_wall == 'independent':
            pw.record_wall('anthropic', reset + 86400 - NOW, now=NOW)
        if held:
            assert quota.hold_until(conn, tid) == reset
            assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'
        else:
            # The disproved hint is rejected; the run's doubling backoff (first run: 300 s) still paces it.
            assert quota.hold_until(conn, tid) == NOW + 300
            assert dispatch.check_respawn_guard(conn, tid) == 'rate_limit_cooldown'
            assert quota.hold_until(conn, tid, NOW + 301) is None
            assert not quota.active(conn, tid, NOW + 301)


def _short_hint_run(conn, tid, pid, reset):
    task = kb.claim_task(conn, tid, claimer=kb._claimer_id())
    assert task is not None
    conn.execute('UPDATE tasks SET worker_pid=? WHERE id=?', (pid, tid))
    kb._append_event(conn, tid, 'run_route', {'model': MODEL, 'provider': 'anthropic', 'source': 'pin',
                     'tier_reason': 'default', 'lane': 'ready'}, run_id=task.current_run_id)
    kb._append_event(conn, tid, 'worker_result', {
        'pid': pid, 'claim_lock': task.claim_lock, 'exit_code': 75,
        'last_output': 'rate limited; retry after 5s', 'failure_reason': 'rate_limit',
        'failure_retryable': True, 'failure_resets_at': float(reset),
        'route_provider': 'anthropic', 'route_model': MODEL}, run_id=task.current_run_id)
    conn.commit()
    dispatch._record_worker_exit(pid, 75 << 8)
    dispatch.detect_crashed_workers(conn)


@pytest.mark.parametrize('legacy', [False, True])
def test_a_disproved_short_hint_keeps_the_doubling_backoff(board, tmp_path, monkeypatch, legacy):
    """Rejecting a hint the pool disproves must not drop the backoff, or a 'retry after 5s' x N storm
    respawns at once. Episodes stored before ``backoff_until`` existed recompute it from the run."""
    from hermes_cli import kanban_provider_walls as pw

    monkeypatch.setattr(kb.time, 'time', lambda: NOW)
    monkeypatch.setenv('HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS', '300')
    _pool(tmp_path, monkeypatch, 'serving')
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='short hints', assignee='a')
        for i in range(4):
            _short_hint_run(conn, tid, 72000 + i, NOW + 5)
        if legacy:
            rows = conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND kind='quota_episode'",
                                (tid,)).fetchall()
            assert rows
            for row in rows:
                payload = json.loads(row['payload'])
                payload.pop('backoff_until', None)
                conn.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(payload), row['id']))
            conn.commit()
        state = quota.episode(conn, tid)
        assert state is not None and state['hinted'] and state['count'] == 4
        pw.walls_path().write_text(json.dumps({}))
        # 4th rate_limited run: backoff 300 * 2**3 = 2400 s; the 5 s hint has long elapsed.
        assert quota.hold_until(conn, tid, NOW + 60) == NOW + 2400
        # Held on the backoff, not on the rejected hint.
        assert dispatch.check_respawn_guard(conn, tid) == 'rate_limit_cooldown'
        assert quota.hold_until(conn, tid, NOW + 2401) is None


@pytest.mark.parametrize('kind,anthropic_in,anthropic_kept', [
    ('serving', 5 * 86400, False),  # a days-long mirror of a false hint: the pool serves, so it goes
    ('serving', 600, True),         # within the backoff cap: a rate-limit wall, left to expire
    ('walled', 5 * 86400, True),    # the pool confirms the wall
    ('empty', 5 * 86400, True),     # an empty pool proves nothing
])
def test_reclaim_phase_prunes_a_provider_wall_the_live_pool_disproves(
        board, tmp_path, monkeypatch, kind, anthropic_in, anthropic_kept):
    """A false hint already mirrored into provider_walls.json (an older reap, another card) keeps every
    card on the host retiering around anthropic; the dispatcher drops it once the live pool serves."""
    from hermes_cli import kanban_provider_walls as pw

    monkeypatch.setattr(kb.time, 'time', lambda: NOW)
    monkeypatch.setattr(pw.time, 'time', lambda: NOW)
    _pool(tmp_path, monkeypatch, kind)
    pw.walls_path().write_text(json.dumps({'openai-codex': NOW + 6 * 86400, 'anthropic': NOW + anthropic_in}))
    with kbc.connect() as conn:
        dispatch._run_reclaim_phase(conn, dispatch.DispatchResult(), stale_timeout_seconds=3600,
                                    failure_limit=2, reconcile_orphans=False)
    walls = pw.active_walls(now=NOW)
    assert walls.get('openai-codex') == NOW + 6 * 86400  # no codex OAuth pool here: nothing disproves it
    assert ('anthropic' in walls) is anthropic_kept
