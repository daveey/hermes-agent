"""A spent Kanban update grace must not be followed by a second full drain (<=60s end to end).

Drives the real ``_stop_impl`` with its phases recorded: the drain gets 0s, the stop leash is one
fixed 40s snapshot taken at stop entry, and settle gets what that leash leaves.
"""
import asyncio
import time

import pytest


from gateway import run_runtime, run_shutdown_restart_wait as wait
from gateway.run import GatewayRunner
from gateway.run_shutdown import _effective_watchdog_leash
from gateway.restart import effective_stop_drain_timeout
from tests.gateway.restart_test_helpers import make_restart_runner


def _runner(tmp_path, monkeypatch, *, drain=0, total=1, managed=1, intent=True, spent=True):
    runner, _ = make_restart_runner()
    path = tmp_path / "intent"
    if intent:
        path.touch()
    runner._restart_requested = True
    runner._restart_update_intent = path
    runner._restart_drain_timeout = drain
    runner._core_update_kanban_cap_spent = spent
    runner._awaitable_work_count = lambda: total
    monkeypatch.setattr(wait, "kanban_managed_count", lambda _: managed)
    return runner, path


def _record(runner, monkeypatch, *, mutate=None):
    seen = {}

    async def noop(self, *a, **k):
        return None

    async def begin(self, ctx):
        ctx.started_at = time.monotonic()
        seen["leash_at_begin"] = _effective_watchdog_leash(self)
        if mutate:
            mutate()

    async def drain(self, timeout, ctx):
        seen["drain"] = timeout
        ctx.timed_out = True

    async def interrupt(self, ctx):
        seen["interrupted"] = True

    async def settle(r, budget=None, reason=None):
        seen["settle_budget"] = budget
        seen["leash_at_settle"] = _effective_watchdog_leash(r)

    for name in ("_stop_finalize_agents_and_adapters", "_stop_release_runtime_state", "_stop_persist_exit_state"):
        monkeypatch.setattr(GatewayRunner, name, noop)
    monkeypatch.setattr(GatewayRunner, "_stop_quiesce_and_close_session_dbs", lambda *a, **k: None)
    monkeypatch.setattr(GatewayRunner, "_stop_begin_teardown", begin)
    monkeypatch.setattr(GatewayRunner, "_stop_drain_active_work", drain)
    monkeypatch.setattr(GatewayRunner, "_stop_interrupt_remaining_work", interrupt)
    monkeypatch.setattr(run_runtime, "settle_gateway_runtime", settle)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [0, 120])
async def test_stop_after_spent_kanban_grace_uses_one_short_leash(tmp_path, monkeypatch, configured):
    runner, _ = _runner(tmp_path, monkeypatch, drain=configured)
    seen = _record(runner, monkeypatch)
    await GatewayRunner._stop_impl(runner)
    assert seen["drain"] == 0.0  # no second graceful wait
    assert seen["interrupted"]  # Stop + uncounted booking path still runs
    assert seen["leash_at_begin"] == 40.0
    # callers subtract ctx.elapsed() themselves: the snapshot is stop-relative, not shrinking
    assert seen["leash_at_settle"] == 40.0
    assert seen["settle_budget"] <= 40.0
    # 15s after-turn + 40s stop + 5s poll allowance
    assert wait.CORE_UPDATE_KANBAN_WAIT_S + seen["leash_at_settle"] + 5 <= 60.0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ordinary", "human", "withdrawn", "cap_not_spent"])
async def test_stop_keeps_normal_budgets_for_other_work(tmp_path, monkeypatch, mode):
    runner, _ = _runner(
        tmp_path, monkeypatch, drain=120, total=2 if mode == "human" else 1,
        intent=mode != "withdrawn", spent=mode != "cap_not_spent")
    if mode == "ordinary":
        runner._restart_update_intent = None
    seen = _record(runner, monkeypatch)
    await GatewayRunner._stop_impl(runner)
    assert seen["drain"] == 120
    assert seen["leash_at_begin"] == 180.0


@pytest.mark.asyncio
async def test_classification_is_snapshotted_at_stop_entry(tmp_path, monkeypatch):
    runner, path = _runner(tmp_path, monkeypatch, drain=120)

    def flip():  # intent withdrawn / human work appears after the watchdog was armed
        path.unlink()
        runner._awaitable_work_count = lambda: 5

    seen = _record(runner, monkeypatch, mutate=flip)
    await GatewayRunner._stop_impl(runner)
    assert seen["drain"] == 0.0
    assert seen["leash_at_settle"] == 40.0


def test_cancelled_restart_clears_snapshot_and_spent_flag(tmp_path, monkeypatch):
    runner, _ = _runner(tmp_path, monkeypatch)
    runner._stop_kanban_leash_s = 40.0
    runner._cancel_restart()
    assert runner._core_update_kanban_cap_spent is False
    assert runner._stop_kanban_leash_s is None


@pytest.mark.asyncio
async def test_real_settle_wait_fits_the_short_leash(monkeypatch):
    """settle_gateway_runtime clamps its 45s task wait to leash - elapsed - tail reserve."""
    waited = {}

    async def fake_wait(tasks, timeout):
        waited["timeout"] = timeout
        return set(), set(tasks)

    class T:
        def cancelled(self):
            return False

    monkeypatch.setattr(run_runtime, "_authorities", lambda r: ["a"])
    monkeypatch.setattr(run_runtime, "_authority_tasks", lambda a: [T()])
    monkeypatch.setattr(run_runtime, "stop_managed_turns", lambda *a: None)
    monkeypatch.setattr(asyncio, "wait", fake_wait)
    import gateway.session_llm_oneshot as oneshot
    monkeypatch.setattr(oneshot, "stop", lambda a: None)
    try:
        await run_runtime.settle_gateway_runtime(object(), 40.0 - 2.0, "r")
    except Exception as exc:  # the tail after the wait needs real authorities; only the wait matters
        waited.setdefault("tail_error", repr(exc))
    assert waited["timeout"] <= 40.0 - 2.0 - run_runtime.STOP_TAIL_RESERVE_SECONDS
