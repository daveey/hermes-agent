"""Quota deadline consumers must report the first usable route, not the longest wall."""
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_provider_walls as walls
from hermes_cli.kanban_quota_episode import decision, episode, hold_until, release_at
from hermes_cli.kanban_worker_node import _quota_held
from tests.hermes_cli.test_kanban_quota_episode import board as board, reap, routing  # noqa: PLC0414
from tests.hermes_cli.test_kanban_quota_episode_release_at import _provider_file, _walled_default_card
from tests.hermes_cli.test_kanban_quota_episode_walls import _clock, _codex_wall


def test_pickup_hold_detail_names_fallback_reset_and_then_clears(board, monkeypatch):
    clock = _clock(monkeypatch)
    routing(monkeypatch)
    monkeypatch.setattr(walls, 'live_quota_wall', lambda *_a, **_k: None)
    reset = clock[0] + 3600
    _provider_file(monkeypatch, {'anthropic': reset})
    with kbc.connect() as conn:
        tid = _walled_default_card(conn, clock, clock[0] + 6 * 86400)
        card = conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
        before = conn.total_changes
        when = datetime.fromtimestamp(reset, UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
        assert _quota_held(conn, card) == (
            'quota_hold', f'subscription_quota_reset until {when} (local dispatch would hold it)')
        assert conn.total_changes == before
        clock[0] = reset
        assert _quota_held(conn, card) is None


def test_third_requeue_notice_persists_fallback_deadline(board, monkeypatch):
    clock = _clock(monkeypatch)
    routing(monkeypatch)
    monkeypatch.setattr(walls, 'live_quota_wall', lambda *_a, **_k: None)
    reset, far = clock[0] + 3600, clock[0] + 6 * 86400
    _provider_file(monkeypatch, {'anthropic': reset})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='notice names available route', assignee='a')
        for _ in range(3):
            reap(conn, tid, provider='openai-codex', **_codex_wall(far))
        assert episode(conn, tid)['until'] == far
        notices = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='quota_episode_notice'", (tid,)).fetchall()
        assert len(notices) == 1
        when = datetime.fromtimestamp(reset, ZoneInfo('America/Los_Angeles')).strftime('%Y-%m-%d %H:%M')
        action = f'wait until {when} PT'
        assert kb._json_dict(notices[0][0])['action'] == action
        comments = conn.execute(
            "SELECT body FROM task_comments WHERE task_id=? AND body LIKE 'Quota wall:%'", (tid,)).fetchall()
        assert len(comments) == 1 and action in comments[0][0]


@pytest.mark.parametrize('later_receipt', [0, 0.5])
def test_earlier_episode_reset_survives_missing_provider_file(board, monkeypatch, later_receipt):
    """A replica carries run/episode history even when this host has no provider-wall file."""
    clock = _clock(monkeypatch)
    routing(monkeypatch)
    monkeypatch.setattr(walls, 'live_quota_wall', lambda *_a, **_k: None)
    _provider_file(monkeypatch, {})
    reset, far = clock[0] + 3600, clock[0] + 6 * 86400
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='two episode providers', assignee='a')
        reap(conn, tid, provider='anthropic', text='usage limit resets in ~1h')
        if later_receipt:
            # Repeated receipts can carry subsecond reset estimates for the same provider.
            # release_at's one-second lookahead is advisory; admission must still recheck.
            reap(conn, tid, provider='anthropic', fields={
                'failure_reason': 'rate_limit', 'failure_resets_at': reset + later_receipt})
        reap(conn, tid, provider='openai-codex', **_codex_wall(far))
        clock[0] += 301
        task = kb.get_task(conn, tid)
        assert walls.active_walls(now=clock[0]) == {}
        assert hold_until(conn, tid, clock[0]) == far
        assert decision(conn, task, now=clock[0])[0] == 'subscription_quota_reset'
        before = conn.total_changes
        assert release_at(conn, task, clock[0]) == reset
        assert conn.total_changes == before
        assert decision(conn, task, now=reset - 1)[0] == 'subscription_quota_reset'
        if later_receipt:
            assert decision(conn, task, now=reset)[0] == 'subscription_quota_reset'
        else:
            assert decision(conn, task, now=reset) == (None, ('claude-sonnet-5-5', 'anthropic'))
        assert decision(conn, task, now=reset + later_receipt) == (None, ('claude-sonnet-5-5', 'anthropic'))
        assert decision(conn, task, now=reset + 1) == (None, ('claude-sonnet-5-5', 'anthropic'))
