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
    ('serving', False, False),  # worker-3: the provider-wide file held only openai-codex
    ('serving', True, True),    # a standing provider-wide wall is independent evidence
    ('walled', False, True),
    ('empty', False, True),     # an empty or unreadable pool proves nothing
])
def test_hinted_episode_wall_holds_only_while_the_live_pool_confirms_it(
        board, tmp_path, monkeypatch, model, kind, provider_wall, held):
    from hermes_cli import kanban_provider_walls as pw

    monkeypatch.setattr(kb.time, 'time', lambda: NOW)
    _pool(tmp_path, monkeypatch, kind)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='false wall', assignee='a')
        reset = _false_wall(conn, tid, model)
        state = quota.episode(conn, tid)
        assert state is not None
        assert state['hinted'] and state['providers'] == ['anthropic'] and state['until'] == reset
        pw.walls_path().write_text(json.dumps({'openai-codex': NOW + 6 * 86400,
                                               **({'anthropic': reset} if provider_wall else {})}))
        if held:
            assert quota.hold_until(conn, tid) == reset
            assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'
        else:
            assert quota.hold_until(conn, tid) is None
            assert not quota.active(conn, tid)
            assert dispatch.check_respawn_guard(conn, tid) is None
