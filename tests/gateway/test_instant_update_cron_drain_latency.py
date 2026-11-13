"""Compose spent update grace with the real stop phase and real drain loop."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.restart import checkpoint_cron_grace_spent, effective_stop_drain_timeout
from gateway.run import GatewayRunner
from tests.gateway import test_core_update_cron_cut as cut


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["instant", "ordinary", "uncut", "human", "withdrawn", "unspent",
                                  "mac_signal", "noncheckpoint", "fenced", "late_api", "late_withdrawal"])
async def test_cron_floor_does_not_restore_spent_update_grace(tmp_path, monkeypatch, mode):
    body = {"cron_cut": mode != "uncut", "checkpoint": mode != "noncheckpoint",
            "kind": "fenced" if mode == "fenced" else "unfenced"}
    runner = cut._runner(tmp_path, monkeypatch, body, cron=1, kanban=2, chat=int(mode == "human"))
    runner._restart_requested = True
    runner._restart_drain_timeout = 0
    runner._cron_drain_timeout = 30
    clock = cut._virtual_clock(monkeypatch)
    if mode == "instant":
        assert await runner._await_active_work_before_restart() is False
        # An unfenced checkpoint intent that cuts cron: the drainer already waited, so the gateway settles 2 s
        # (CORE_UPDATE_CHECKPOINT_KANBAN_WAIT_S), not the 15 s Kanban-only wait.
        assert 2 <= clock[0] < 2.2
    else:
        runner._core_update_kanban_cap_spent = mode != "unspent"
    intent = runner._restart_update_intent
    if mode in ("ordinary", "mac_signal"):
        runner._restart_update_intent = None
    if mode == "withdrawn":
        intent.unlink()
    if mode == "mac_signal":
        runner._stop_requested_by_signal = True
        runner._launchd_exit_timeout_s = 60
    runner._core_update_kanban_snapshot_stop_leash()
    if mode == "late_withdrawal":
        intent.unlink()
    monkeypatch.setattr(GatewayRunner, "_mark_running_sessions_resume_pending", AsyncMock(return_value=[]))
    runner._active_cron_job_count = lambda: 1
    runner._active_api_run_count = lambda: int(mode == "late_api")
    runner._drain_work_counts = lambda: (0, 1, int(mode == "late_api"), 0)
    runner._snapshot_running_agents = dict
    observed = {}

    async def drain(timeout, cron_timeout):
        observed.update(timeout=timeout, cron_timeout=cron_timeout)
        return await GatewayRunner._drain_active_agents(runner, timeout, cron_timeout)

    runner._drain_active_agents = drain
    ctx = SimpleNamespace(deferred_count=lambda: 0, elapsed=lambda: 0)
    await runner._stop_drain_active_work(effective_stop_drain_timeout(runner), ctx)
    assert observed["cron_timeout"] == (0 if mode == "instant" else 30)
    assert ctx.timed_out is True  # stop_impl must still enter interruption/settlement, never skip the work
    if mode == "instant":
        # Historical composition: retain all other measured poll/prepass/pause/
        # checkpoint/install/restart costs of attempt5. Not live delivery proof.
        composed = 197 - 30 + observed["cron_timeout"]
        assert composed <= 180
        assert clock[0] < 2.2
    else:
        assert 30 <= clock[0] < 30.2


@pytest.mark.parametrize("cron_cut, expired", [(True, True), (1, False), ("true", False)])
def test_checkpoint_wait_requires_boolean_cron_cut(tmp_path, monkeypatch, cron_cut, expired):
    # Cards alone reach the checkpoint predicate even when cron_cut is malformed.
    body = {"kind": "unfenced", "checkpoint": True, "cron_cut": cron_cut}
    runner = cut._runner(tmp_path, monkeypatch, body, cron=0, kanban=1)
    intent = runner._restart_update_intent
    assert runner._kanban_restart_wait_state(intent, now=100, since=None) == (100, False)
    assert runner._kanban_restart_wait_state(intent, now=102, since=100) == (100, expired)


@pytest.mark.parametrize("initial, updated", [(True, False), (False, True)])
def test_checkpoint_wait_rereads_same_intent_between_polls(tmp_path, monkeypatch, initial, updated):
    body = {"kind": "unfenced", "checkpoint": initial, "cron_cut": True}
    runner = cut._runner(tmp_path, monkeypatch, body, cron=0, kanban=1)
    intent = runner._restart_update_intent
    since, expired = runner._kanban_restart_wait_state(intent, now=100, since=None)
    assert (since, expired) == (100, False)
    # Keep the runner and Path: a cached first read would select the wrong wait.
    intent.write_text(json.dumps({**body, "checkpoint": updated}), encoding="utf-8")
    assert runner._kanban_restart_wait_state(intent, now=102, since=since) == (100, updated)
    assert runner._kanban_restart_wait_state(intent, now=115, since=since) == (100, True)


@pytest.mark.parametrize("text, expected", [
    ('{"kind":"unfenced","checkpoint":true,"cron_cut":true}', True),
    ('\ufeff{"kind":"unfenced","checkpoint":true,"cron_cut":true}', True),
    ('{"kind":"unfenced","checkpoint":1,"cron_cut":true}', False),
    ('{"kind":"unfenced","checkpoint":true,"cron_cut":1}', False),
    ('{"kind":"fenced","checkpoint":true,"cron_cut":true}', False),
    ('{"kind":"unfenced","cron_cut":true}', False),
    ('[]', False), ('null', False), ('invalid', False),
])
def test_post_snapshot_intent_read_is_fail_closed(tmp_path, text, expected):
    intent = tmp_path / "intent"
    intent.write_text(text)
    runner = SimpleNamespace(_stop_kanban_leash_s=40, _restart_update_intent=intent)
    assert checkpoint_cron_grace_spent(runner) is expected
    intent.unlink()
    assert checkpoint_cron_grace_spent(runner) is False
    runner._restart_update_intent = None
    assert checkpoint_cron_grace_spent(runner) is False
    intent.write_text(json.dumps({"kind": "unfenced", "checkpoint": True, "cron_cut": True}))
    runner._restart_update_intent = intent
    runner._stop_kanban_leash_s = None
    assert checkpoint_cron_grace_spent(runner) is False
