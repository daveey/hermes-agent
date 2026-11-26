"""Auth-churn exits write no quota wall/episode; ``quota-release --probe`` refuses a confirmed wall. [daveey]

2026-11-12: retries in the revoked-401 window recorded ``subscription_quota_reset`` episodes until
11-13/11-17 and ``anthropic`` walls on four workers; the hub's usage endpoint showed headroom and a
live one-shot answered. The only release was hand SQL on the event payload.
"""
import argparse
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_provider_walls as walls
from hermes_cli import kanban_quota_episode_release as release
from hermes_cli import kanban_quota_probe as probe
from hermes_cli.kanban_quota_episode import AUTH_CHURN_MAX_BACKOFF_SECONDS, decision, episode, hold_until
from tests.hermes_cli import test_kanban_quota_episode as episode_tests

board, reap = episode_tests.board, episode_tests.reap

NOW = 1_800_000_000
RESET = NOW + 5 * 86400


@pytest.fixture
def clock(monkeypatch):
    now = [NOW]
    monkeypatch.setattr(kb.time, 'time', lambda: now[0])
    return now


@pytest.fixture
def operator(monkeypatch):
    """``quota-release`` is operator-only: drop the worker lineage this test may run under."""
    for key in ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_WORKER_TASK', 'HERMES_DELEGATED_CHILD_CONTEXT'):
        monkeypatch.delenv(key, raising=False)


def _events(conn, tid, kind):
    return [json.loads(r[0]) for r in conn.execute(
        'SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id', (tid, kind))]


def test_auth_churn_receipt_writes_no_episode_or_wall(board, clock):
    fields = {'failure_reason': 'quota_unconfirmed', 'failure_retryable': True}
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='auth churn', assignee='a')
        for n in range(4):  # repeated churn must not grow into a wall (backoff would reach 40 min)
            reap(conn, tid, fields=fields,
                 text='SubscriptionAuthUnavailable: anthropic/claude-opus-5-5 pool emptied by auth failures')
            assert _events(conn, tid, 'quota_episode') == []
            assert walls.active_walls() == {}
            state = episode(conn, tid)
            assert state['auth_churn'] is True and state['hinted'] is False
            assert state['until'] - kb.time.time() <= AUTH_CHURN_MAX_BACKOFF_SECONDS
            clock[0] += AUTH_CHURN_MAX_BACKOFF_SECONDS + 1
        assert _events(conn, tid, 'quota_episode_notice') == []
        assert dispatch.check_respawn_guard(conn, tid) is None


def _held(conn):
    tid = kb.create_task(conn, title='held deep card', assignee='a')
    reap(conn, tid, text=f'Provider-wide quota unavailable for anthropic; resets at {RESET}',
         fields={'failure_reason': 'rate_limit', 'failure_resets_at': RESET})
    walls.record_wall('anthropic', RESET - NOW)
    assert hold_until(conn, tid) == RESET
    assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'
    return tid


def test_probe_release_on_live_headroom_clears_hold_and_wall_with_audit(board, clock, operator):
    seen = []

    def live(provider, model):
        seen.append((provider, model))
        return probe.Verdict('headroom', 'admin: five_hour 3%, seven_day 4%, Opus week 12%')

    with kbc.connect() as conn:
        tid = _held(conn)
        payload = release.release(conn, tid, reason='revoked-401 window', actor='operator', probe=live)
        assert seen == [('anthropic', 'claude-sonnet-5-5')]
        audit = _events(conn, tid, release.KIND)
        assert len(audit) == 1 and audit[0] == payload
        assert audit[0]['until'] == RESET and audit[0]['actor'] == 'operator' and audit[0]['via'] == 'probe'
        assert audit[0]['verdicts'] == {'anthropic': {
            'state': 'headroom', 'detail': 'admin: five_hour 3%, seven_day 4%, Opus week 12%'}}
        assert hold_until(conn, tid) is None
        assert dispatch.check_respawn_guard(conn, tid) is None
        assert decision(conn, kb.get_task(conn, tid)) == (None, None)
        assert 'anthropic' not in walls.active_walls()
        # The original episode stays as it was recorded (no history rewrite).
        assert _events(conn, tid, 'quota_episode')[0]['until'] == RESET


@pytest.mark.parametrize('state', ['walled', 'unknown'])
def test_probe_release_refused_when_probe_confirms_or_cannot_disprove(board, clock, operator, state):
    """The control: a live probe that confirms (or cannot read) the wall refuses, audited; nothing changes."""
    with kbc.connect() as conn:
        tid = _held(conn)
        with pytest.raises(release.ReleaseRefused, match=f'anthropic={state}'):
            release.release(conn, tid, reason='x', actor='operator',
                            probe=lambda p, m: probe.Verdict(state, 'every account Opus week 100%'))
        assert _events(conn, tid, release.KIND) == []
        refused = _events(conn, tid, release.REFUSED_KIND)
        assert len(refused) == 1 and refused[0]['verdicts']['anthropic']['state'] == state
        assert refused[0]['actor'] == 'operator' and refused[0]['until'] == RESET
        assert hold_until(conn, tid) == RESET
        assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'
        assert walls.active_walls().get('anthropic') == pytest.approx(RESET)


def test_probe_cli_flag_refuses_with_exit_2(board, clock, operator, monkeypatch, capsys):
    monkeypatch.setattr(probe, 'live_verdict', lambda p, m: probe.Verdict('walled', 'all 100%'))
    with kbc.connect() as conn:
        tid = _held(conn)
    assert release.cmd(argparse.Namespace(task_id=tid, reason='', probe=True)) == 2
    assert 'anthropic=walled' in capsys.readouterr().err
    with kbc.connect() as conn:
        assert len(_events(conn, tid, release.REFUSED_KIND)) == 1
        assert hold_until(conn, tid) == RESET


def test_probe_flag_is_registered():
    from hermes_cli.kanban_parser import build_parser
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest='cmd'))
    args = parser.parse_args(['kanban', 'quota-release', 't_x', '--probe'])
    assert args.probe is True and args.task_id == 't_x'


def test_new_wall_after_probe_release_holds_again(board, clock, operator):
    with kbc.connect() as conn:
        tid = _held(conn)
        release.release(conn, tid, reason='x', actor='op', probe=lambda p, m: probe.Verdict('headroom', ''))
        clock[0] += 60
        later = NOW + 3 * 86400
        reap(conn, tid, fields={'failure_reason': 'rate_limit', 'failure_resets_at': later})
        assert hold_until(conn, tid) == later


def test_probe_release_refuses_while_any_held_provider_is_walled(board, clock, operator):
    """Worker-2 11-12: one card held on anthropic (false, revoked window) and openai-codex (real weekly cap)."""
    codex_reset = NOW + 6 * 86400
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='two walls', assignee='a')
        reap(conn, tid, provider='openai-codex', fields={'failure_reason': 'rate_limit', 'failure_resets_at': codex_reset})
        clock[0] += 60
        reap(conn, tid, provider='anthropic', fields={'failure_reason': 'rate_limit', 'failure_resets_at': RESET})
        assert hold_until(conn, tid) == codex_reset
        verdicts = {'anthropic': probe.Verdict('headroom', 'admin 4%'), 'openai-codex': probe.Verdict('walled', '100%')}
        with pytest.raises(release.ReleaseRefused, match='openai-codex=walled'):
            release.release(conn, tid, reason='x', actor='op', probe=lambda p, m: verdicts[p])
        assert hold_until(conn, tid) == codex_reset
        assert _events(conn, tid, release.REFUSED_KIND)[0]['verdicts']['anthropic']['state'] == 'headroom'


def test_usage_verdict_reads_account_and_model_windows():
    from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
    from datetime import UTC, datetime

    def snap(*windows):
        return AccountUsageSnapshot(provider='anthropic', source='t', fetched_at=datetime.now(UTC), windows=tuple(
            AccountUsageWindow(label=label, used_percent=used, scope=scope) for label, used, scope in windows))
    capped = snap(('Current session', 10, 'account'), ('Current week', 60, 'account'), ('Opus week', 100, 'model'))
    free = snap(('Current session', 3, 'account'), ('Current week', 4, 'account'), ('Opus week', 12, 'model'))
    sonnet_ok = snap(('Current week', 50, 'account'), ('Opus week', 100, 'model'), ('Sonnet week', 10, 'model'))
    assert probe.usage_verdict([capped, free], 'claude-opus-5-5').state == 'headroom'
    assert probe.usage_verdict([capped, capped], 'claude-opus-5-5').state == 'walled'
    assert probe.usage_verdict([capped, None], 'claude-opus-5-5').state == 'unknown'
    assert probe.usage_verdict([sonnet_ok], 'claude-sonnet-5-5').state == 'headroom'
    assert probe.usage_verdict([sonnet_ok], 'claude-opus-5-5').state == 'walled'
    assert probe.usage_verdict([], 'claude-opus-5-5').state == 'unknown'


@pytest.mark.parametrize('state', ['headroom', 'walled', 'unknown'])
def test_start_time_wall_contradicted_by_live_usage_books_no_reset(state):
    from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
    from hermes_cli.kanban_quota_receipt import quota_fields
    from hermes_cli.turn_exit import credential_failure_flags, turn_exit_code
    wall = SubscriptionQuotaUnavailable('anthropic', 'claude-opus-5-5', RESET)
    wrapped = RuntimeError('agent init failed')
    wrapped.__cause__ = wall
    booked = probe.contradicted(wrapped, verdict=lambda p, m: probe.Verdict(state, 'admin: Opus week 12%'))
    assert turn_exit_code(None, kanban_worker=True, **credential_failure_flags(booked)) == 75
    if state == 'headroom':
        assert quota_fields(booked) == {'failure_reason': 'quota_unconfirmed', 'failure_retryable': True}
        assert 'resets at' not in str(booked)
    else:  # control: a confirmed or unreadable wall keeps its typed deadline
        assert booked is wrapped
        assert quota_fields(booked)['failure_resets_at'] == RESET
