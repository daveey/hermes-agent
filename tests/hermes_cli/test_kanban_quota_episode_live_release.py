"""A hinted quota episode the live OAuth pool disproves stops holding; operators can release one. [daveey]

t_01a1cfac4046a296: during a revoked-token outage the worker raised ``SubscriptionQuotaUnavailable ... resets
at <the earliest reset of the accounts genuinely at 100%>``. The hint became a hinted episode that ``decision``
trusted until its deadline, so 10+ ready cards waited 1-6 days while anthropic had room again.
"""
import argparse

import pytest

from agent.credential_pool import CredentialPool, PooledCredential
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_provider_walls as walls
from hermes_cli import kanban_quota_episode_release as release
from hermes_cli import kanban_queue_position as queue
from hermes_cli.kanban_db_dispatch_clock import guard_hold_until
from hermes_cli.kanban_quota_episode import MAX_BACKOFF_SECONDS, episode, hold_until
from tests.hermes_cli.test_kanban_quota_episode import board as board, routing  # noqa: PLC0414 -- the self-alias is load-bearing: it suppresses F811 for the pytest fixture parameter shadowing this import

NOW = 1_800_000_000
DAYS = 86400


def _entry(i, *, exhausted_until=None):
    status = {'last_status': 'exhausted', 'last_status_at': NOW - 60, 'last_error_code': 429,
              'last_error_reset_at': exhausted_until} if exhausted_until else {'last_status': 'ok'}
    return PooledCredential(provider='anthropic', id=f'acct{i}', label=f'acct{i}', auth_type='oauth', priority=i,
                            source='test', access_token=f'sk-ant-oat-{i}', **status)


@pytest.fixture
def pool(monkeypatch):
    """The anthropic OAuth pool the live serving check reads; ``seen`` records the (provider, model) it asked for."""
    entries, seen = [], []

    def load(provider, **_kw):
        seen.append(provider)
        return CredentialPool(provider, [e for e in entries if e.provider == provider],
                              allowed_auth_types={'oauth'})
    monkeypatch.setattr('agent.credential_pool.load_pool', load)
    monkeypatch.setattr(kb.time, 'time', lambda: NOW)
    for key in ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_WORKER_TASK', 'HERMES_DELEGATED_CHILD_CONTEXT'):
        monkeypatch.delenv(key, raising=False)
    return entries, seen


def _reap(conn, tid, *, provider='anthropic', model='claude-opus-5-5', route_provider='anthropic'):
    """One rate_limited run whose receipt says the subscription resets in 3 days (a hinted episode)."""
    task = kb.claim_task(conn, tid, claimer=kb._claimer_id())
    pid = 71001
    conn.execute('UPDATE tasks SET worker_pid=? WHERE id=?', (pid, tid))
    kb._append_event(conn, tid, 'run_route', {'model': model, 'provider': provider, 'source': 'pin',
                     'tier_reason': 'pinned', 'lane': 'ready'}, run_id=task.current_run_id)
    fields = {'route_provider': route_provider, 'route_model': model} if route_provider else {}
    kb._append_event(conn, tid, 'worker_result', {
        'pid': pid, 'claim_lock': task.claim_lock, 'exit_code': 75,
        'last_output': f'SubscriptionQuotaUnavailable: Subscription quota unavailable for anthropic/{model}; '
                       f'resets at {NOW + 3 * DAYS}.0', **fields}, run_id=task.current_run_id)
    conn.commit()
    dispatch._record_worker_exit(pid, 75 << 8)
    dispatch.detect_crashed_workers(conn)
    assert kb.get_task(conn, tid).status == 'ready'
    state = episode(conn, tid)
    assert state['hinted'] and state['until'] == NOW + 3 * DAYS
    return state


def _released(conn, tid):
    return [kb._json_dict(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='quota_episode_released'", (tid,))]


def test_hinted_episode_with_a_non_exhausted_pool_entry_is_admitted(board, pool):
    """The live pool disproves the outage hint (PR #1409's ``_disproved``): only the doubling backoff holds."""
    entries, seen = pool
    entries += [_entry(1, exhausted_until=NOW + 3 * DAYS), _entry(2)]  # one account at 100%, one with room
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='stale outage hint', assignee='a')
        _reap(conn, tid)
        until = hold_until(conn, tid, NOW + 1)
        assert until is not None and until <= NOW + MAX_BACKOFF_SECONDS  # never the 3-day hint
        assert hold_until(conn, tid, until + 1) is None
        assert guard_hold_until(conn, tid, 'subscription_quota_reset', until + 1) is None
        assert _released(conn, tid) == []  # the automatic path writes no boundary: backoff pacing survives
    assert 'anthropic' in seen


def test_hinted_episode_with_every_pool_entry_exhausted_still_holds(board, pool):
    entries, _ = pool
    entries += [_entry(1, exhausted_until=NOW + 3 * DAYS), _entry(2, exhausted_until=NOW + 4 * DAYS)]
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='genuine wall', assignee='a')
        _reap(conn, tid)
        assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'
        assert hold_until(conn, tid, NOW + 600) == NOW + 3 * DAYS
        assert _released(conn, tid) == [] and episode(conn, tid) is not None


@pytest.mark.parametrize('why', ['empty_pool', 'model_cooldown'])
def test_a_pool_with_no_usable_entry_still_holds(board, pool, why):
    """An empty pool is unknown (not capacity); an entry cooled for the walled model cannot serve it."""
    entries, _ = pool
    if why == 'model_cooldown':
        entry = _entry(1)
        entry.model_cooldowns = {'claude-opus-5-5': NOW + 3 * DAYS}
        entries.append(entry)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title=why, assignee='a')
        _reap(conn, tid)
        assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'


def test_unknown_provider_episode_still_holds(board, pool):
    entries, _ = pool
    entries.append(_entry(1))  # anthropic has room, but nobody recorded which provider hit the wall
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='unknown provider', assignee='a')
        state = _reap(conn, tid, provider=None, route_provider=None)
        assert state['providers'] == []
        assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'
        assert _released(conn, tid) == []


def test_quota_release_makes_the_card_admissible_and_shows_in_the_queue_note(board, pool, capsys):
    entries, _ = pool
    entries.append(_entry(1, exhausted_until=NOW + 3 * DAYS))  # the live pool still says walled
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='operator release', assignee='a')
        _reap(conn, tid)
        assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'
        dispatch.dispatch_once(conn, spawn_fn=lambda *_a: pytest.fail('held card spawned'))
    walls.record_wall('openai-codex', 2 * DAYS)  # another provider's wall is never touched
    assert release.cmd(argparse.Namespace(task_id=tid, reason='revoked-token outage hint')) == 0
    assert 'Released' in capsys.readouterr().out
    assert set(walls.active_walls()) == {'openai-codex'}
    with kbc.connect() as conn:
        assert dispatch.check_respawn_guard(conn, tid) is None
        assert episode(conn, tid) is None
        events = _released(conn, tid)
        assert [(e['reason'], e['via']) for e in events] == [('revoked-token outage hint', 'operator')]
        place = queue.position(conn, kb.get_task(conn, tid))
        note = queue.describe(place)
        assert 'quota hold released (revoked-token outage hint, via operator)' in note
        assert place['lane'] != 'respawn_guard'
        assert any('QUOTA RELEASED' in c.body for c in kb.list_comments(conn, tid))
        assert release.cmd(argparse.Namespace(task_id=tid, reason='')) == 2  # nothing left to release


def test_drop_wall_keeps_a_wall_that_outlasts_the_released_hint(tmp_path):
    path = tmp_path / 'provider_walls.json'
    walls.record_wall('anthropic', 3 * DAYS, now=NOW, path=path)
    assert walls.drop_wall('anthropic', not_after=NOW + 2 * DAYS, path=path) is False  # other evidence: stays
    assert walls.drop_wall('anthropic', not_after=NOW + 3 * DAYS, path=path) is True
    assert walls.active_walls(now=NOW, path=path) == {}
    assert walls.drop_wall('anthropic', not_after=NOW + 3 * DAYS, path=path) is False


def test_quota_release_refuses_worker_lineage(board, pool, monkeypatch):
    monkeypatch.setenv('HERMES_KANBAN_TASK', 't_worker')
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='self release', assignee='a')
        _reap(conn, tid)
        with pytest.raises(release.ReleaseRefused, match='operator-only'):
            release.release(conn, tid, reason='x', actor='w')
        assert _released(conn, tid) == []
