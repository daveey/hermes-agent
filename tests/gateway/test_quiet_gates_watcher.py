"""Quiet keeps the dispatcher on its NORMAL branch (health publishes, no drain gate) and skips only the
autonomous ``_decomp`` agent invocation made while a claim-quiet lease is live."""
from __future__ import annotations

import logging

import pytest

from agent import claim_quiet as cq
from gateway import kanban_watchers as watchers
from gateway.kanban_dispatcher_health import DispatcherHealth
from gateway.kanban_watchers_dispatcher import _KanbanDispatcher, _resolve_dispatcher_settings
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.gateway import test_kanban_auto_decompose_min_age as _ad
from tests.gateway import test_kanban_external_drain_hold as _hold

home = _hold.home  # fixtures
board = _ad.board
env = _ad.env  # the auto-decompose board fixture depends on this name


def test_health_publishes_and_claims_stay_on_normal_branch_under_quiet(home, monkeypatch, caplog):
    ready, spawned = _hold._board(monkeypatch)
    suppress, published, results = [], [], []
    real_tick, real_publish = _KanbanDispatcher.tick_once, DispatcherHealth.publish

    def tick(self, *, suppress_spawn=False, **kw):
        suppress.append(suppress_spawn)
        out = real_tick(self, suppress_spawn=suppress_spawn, **kw)
        results.extend(out or [])
        return out

    def publish(self, scopes):
        published.append(list(scopes))
        return real_publish(self, scopes)

    monkeypatch.setattr(_KanbanDispatcher, "tick_once", tick)
    monkeypatch.setattr(DispatcherHealth, "publish", publish)
    monkeypatch.setattr(watchers, "_HEALTH_WINDOW", 1)
    caplog.set_level(logging.INFO, logger="gateway")
    with cq.acquire(max_s=60, reason="measure") as owner:
        _hold._run_ticks(monkeypatch, [False, False])
        assert spawned == [] and _hold._status(ready) == "ready"
    assert suppress == [False, False]  # never suppress_spawn / gate='drain' for quiet
    assert len(published) == 2 and all(published)  # maintained scopes still published each tick
    assert any(res.quiet_deferred == [(ready, owner.lease.episode_id)] for _slug, res in results)
    assert not any(res.core_update_drain_hold for _slug, res in results)
    # quiet explains the zero spawns: even with a 1-tick window there is no "stuck" warning
    assert not _hold._lines(caplog, _hold.STUCK)
    _hold._run_ticks(monkeypatch, [False])  # lease released: claims resume on the same branch
    assert spawned == [ready]


def _triage(count=1):
    return [_ad._triage_card(title=f"triage {i}", age_seconds=300) for i in range(count)]


def test_auto_decompose_skips_attempt_under_quiet_then_runs(board):
    dispatcher, specified = board
    cards = _triage(2)
    with cq.acquire(max_s=60, reason="measure"):
        assert dispatcher.auto_decompose_tick(3) == 0
    assert specified == []  # no agent/aux invocation at all
    with kbc.connect_closing() as conn:
        assert all(kb.get_task(conn, t).status == "triage" for t in cards)
    assert dispatcher.auto_decompose_tick(3) == 2 and sorted(specified) == sorted(cards)


def test_auto_decompose_rechecks_at_invocation_not_at_tick_start(board, monkeypatch):
    """Quiet starts after the sweep began (after card listing): the actual ``_decomp`` call still declines."""
    from hermes_cli import kanban_decompose as decomp
    dispatcher, specified = board
    _triage(1)
    real_list = decomp.list_triage_ids
    owners = []

    def list_then_quiet(**kw):
        ids = real_list(**kw)
        owners.append(cq.acquire(max_s=60, reason="measure"))
        return ids

    monkeypatch.setattr(decomp, "list_triage_ids", list_then_quiet)
    try:
        assert dispatcher.auto_decompose_tick(3) == 0
        assert specified == []
    finally:
        for owner in owners:
            owner.close()


def test_auto_decompose_does_not_block_other_maintenance(board):
    """Only the agent invocation is skipped: the tick itself returns normally and the next dispatch tick runs."""
    dispatcher, _specified = board
    _triage(1)
    with cq.acquire(max_s=60, reason="measure"):
        assert dispatcher.auto_decompose_tick(3) == 0
        assert dispatcher.tick_once() is not None
