"""Live OAuth pool reads in the dispatcher of a multiplexing gateway, and their failure signal [daveey].

mini BB gateway (``gateway.multiplex_profiles: true``), 2026-11-12 14:30-14:57Z: the dispatcher tick runs in an
empty ``Context`` with no profile secret scope, so ``load_pool('anthropic')`` raised ``UnscopedSecretError``
(its singleton seeder reads ``ANTHROPIC_TOKEN`` via ``get_secret``, which fails closed under multiplexing).
``live_pool_serving`` swallowed that at DEBUG and answered False, so a disproved anthropic episode kept
holding a card and ``prune_disproved_walls`` kept a false anthropic wall, while a fresh process said True.
"""
import contextvars
import time
import json
import logging
from pathlib import Path

import pytest

from agent import secret_scope
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_provider_walls as pw
from tests.hermes_cli.test_kanban_quota_episode import board as board  # noqa: PLC0414 -- the self-alias suppresses F811 for the pytest fixture parameter

MODEL = 'claude-opus-5-5'
FAR = int(time.time()) + 3 * 86400  # within a quota window, not an entitlement bench


def _pool(tmp_path, monkeypatch, kind):
    from agent.credential_pool import PooledCredential
    from hermes_cli.auth import write_credential_pool

    monkeypatch.setenv('HERMES_SHARED_AUTH_DIR', str(tmp_path / 'shared'))
    rows = []
    for i in range(3):
        row: dict[str, object] = dict(id=f'acct{i}', label=f'account{i}', source='manual:device_code',
                                      auth_type='oauth', access_token=f'sk-ant-oat-fixture-{i}',
                                      refresh_token=f'fixture-refresh-{i}', priority=i)
        if kind == 'walled' or i:
            row['model_cooldowns'] = {MODEL: FAR + i}
        rows.append(PooledCredential.from_dict('anthropic', row).to_dict())
    write_credential_pool('anthropic', rows)


@pytest.fixture
def multiplexing(monkeypatch):
    """This process is a profile multiplexer; the caller runs like a dispatcher tick (empty Context)."""
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', True)
    return lambda fn, *args: contextvars.Context().run(fn, *args)


def test_live_pool_reads_see_the_launch_pool_in_a_multiplexing_dispatcher_tick(
        board, tmp_path, monkeypatch, multiplexing):
    _pool(tmp_path, monkeypatch, 'serving')
    assert multiplexing(pw.live_pool_serving, 'anthropic', MODEL) is True
    assert multiplexing(pw.live_quota_room, 'anthropic', MODEL) is True
    assert multiplexing(pw.live_quota_wall, 'anthropic', MODEL) is None


def test_live_quota_wall_reports_an_exhausted_pool_in_a_multiplexing_dispatcher_tick(
        board, tmp_path, monkeypatch, multiplexing):
    """``live_quota_wall`` fails open: an unreadable pool read as 'no wall' and sent fresh cards into it."""
    _pool(tmp_path, monkeypatch, 'walled')
    assert multiplexing(pw.live_quota_wall, 'anthropic', MODEL) == float(FAR)
    assert multiplexing(pw.live_pool_serving, 'anthropic', MODEL) is False


def test_multiplexing_dispatcher_prunes_a_wall_the_launch_pool_disproves(
        board, tmp_path, monkeypatch, multiplexing):
    now = time.time()
    _pool(tmp_path, monkeypatch, 'serving')
    pw.walls_path().write_text(json.dumps({'anthropic': now + 5 * 86400}))

    def reclaim():
        with kbc.connect() as conn:
            dispatch._run_reclaim_phase(conn, dispatch.DispatchResult(), stale_timeout_seconds=3600,
                                        failure_limit=2, reconcile_orphans=False)
    multiplexing(reclaim)
    assert 'anthropic' not in pw.active_walls()


def test_a_bound_turn_scope_is_left_alone(board, tmp_path, monkeypatch, multiplexing):
    """Inside a routed turn the caller's own scope stands: the read never swaps in the launch profile's."""
    _pool(tmp_path, monkeypatch, 'serving')
    seen = []
    import agent.credential_pool as cp
    real = cp.load_pool

    def spy(provider, **kw):
        seen.append(secret_scope.current_secret_scope())
        return real(provider, **kw)
    monkeypatch.setattr(cp, 'load_pool', spy)

    def turn():
        token = secret_scope.set_secret_scope({'MARKER': 'turn'}, profile_home=str(board))
        try:
            return pw.live_pool_serving('anthropic', MODEL)
        finally:
            secret_scope.reset_secret_scope(token)
    assert multiplexing(turn) is True
    assert seen == [{'MARKER': 'turn'}]


@pytest.fixture
def failing_pool(monkeypatch):
    import agent.credential_pool as cp

    def boom(provider, **_kw):
        raise RuntimeError(f'cannot read {provider}')
    monkeypatch.setattr(cp, 'load_pool', boom)
    clock = [1000.0]
    monkeypatch.setattr(pw.time, 'monotonic', lambda: clock[0])
    pw._READ_FAILURE_WARNED.clear()
    return clock


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == pw.logger.name and r.levelno == logging.WARNING]


def test_a_pool_read_failure_warns_once_per_provider_per_window(failing_pool, caplog):
    caplog.set_level(logging.DEBUG, logger=pw.logger.name)
    assert pw.live_pool_serving('anthropic', MODEL) is False
    assert pw.live_quota_room('anthropic', MODEL) is False
    assert pw.live_quota_wall('anthropic', MODEL) is None
    warned = _warnings(caplog)
    assert len(warned) == 1
    assert 'anthropic' in warned[0] and 'RuntimeError' in warned[0]

    assert pw.live_pool_serving('openai-codex', 'gpt-6-astra') is False  # another provider has its own window
    assert len(_warnings(caplog)) == 2

    failing_pool[0] += pw.READ_FAILURE_WARN_SECONDS + 1
    pw.live_pool_serving('anthropic', MODEL)
    assert len(_warnings(caplog)) == 3
