"""Shutdown bookkeeping cannot hold the owner loop or outlive its total budget."""
import asyncio
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway import run_runtime, session_acp_lifecycle as acp
from gateway.run import GatewayRunner


@pytest.mark.asyncio
async def test_slow_receipt_does_not_block_settlement(monkeypatch):
    release = threading.Event()
    entered = threading.Event()
    class DB:
        @contextmanager
        def _read_ctx(self):
            yield self

        def execute(self, *args):
            return [(acp.POLICY_PREFIX + 'slow',)]

    authority = SimpleNamespace(db=DB(), sessions={}, profile_id='test')
    runner = object.__new__(GatewayRunner)
    authority.runner = runner
    runner.session_ticket_store = SimpleNamespace(revoke=Mock())
    runner.session_api = None
    monkeypatch.setattr(run_runtime, '_authorities', lambda r: [authority])
    monkeypatch.setattr(run_runtime, 'stop_managed_turns', lambda *a: None)

    def receipt(*args):
        entered.set()
        release.wait(5)  # fails safely on old code instead of wedging pytest
        return {'policy': {'source': 'cli'}}

    monkeypatch.setattr(acp, 'local_receipt', receipt)
    started = time.monotonic()
    try:
        await run_runtime.settle_gateway_runtime(
            runner, budget=run_runtime.STOP_TAIL_RESERVE_SECONDS + .1)
        assert time.monotonic() - started < 2
        assert entered.is_set()
        runner.session_ticket_store.revoke.assert_called_once()
        # Timeout of the observer must not disguise the physical DB reader/writer:
        # the shutdown close gate consumes this count to leave SQLite open safely.
        assert runner._shutdown_executor(drain_timeout=0) == 1
    finally:
        release.set()
        runner._shutdown_executor(drain_timeout=2)


def test_sweep_stops_before_next_receipt_after_deadline(monkeypatch):
    authority = SimpleNamespace(db=Mock(), sessions={})
    authority.db._read_ctx = Mock()
    authority.db._read_ctx.return_value.__enter__ = Mock(return_value=Mock())
    authority.db._read_ctx.return_value.__exit__ = Mock(return_value=False)
    conn = authority.db._read_ctx.return_value.__enter__.return_value
    conn.execute.return_value = [(acp.POLICY_PREFIX + 'one',), (acp.POLICY_PREFIX + 'two',)]
    ended = Mock(return_value=True)
    monkeypatch.setattr(acp, 'end_idle_acp_session', ended)
    assert acp.end_idle_acp_sessions(authority, deadline=time.monotonic() - 1) == 0
    ended.assert_not_called()
    authority.db._read_ctx.assert_not_called()


@pytest.mark.asyncio
async def test_budget_is_shared_by_profiles(monkeypatch):
    clock = [100.0]
    calls = []
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])

    async def execute(fn, authority, deadline):
        calls.append((authority, deadline))
        clock[0] += 1.5

    runner = SimpleNamespace(_run_in_executor_with_context=execute)
    await acp.settle_idle_acp_sessions(runner, ['one', 'two', 'three'], 30)
    assert calls == [('one', 102.0), ('two', 102.0)]


@pytest.mark.asyncio
async def test_exhausted_settle_budget_skips_bookkeeping():
    runner = SimpleNamespace(_run_in_executor_with_context=Mock())
    await acp.settle_idle_acp_sessions(runner, ['one'], 0)
    runner._run_in_executor_with_context.assert_not_called()
