"""Regression tests for bounded cron post-run cleanup.

A cron worker must release its in-memory dispatch guard even when SQLite or an
agent resource finalizer stops returning after the model turn has ended.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

from cron.scheduler import _run_cron_cleanup_with_timeout, _teardown_cron_agent, run_job
from cron.scheduler_detached_worker import defer_teardown_to_running_worker


_RUNTIME = {
    "api_key": "test-key",
    "base_url": "https://example.invalid/v1",
    "provider": "openrouter",
    "api_mode": "chat_completions",
}


class HangingSessionDB:
    def __init__(self, release: threading.Event):
        self.release = release
        self.entered = threading.Event()
        self.finished = threading.Event()

    def get_compression_tip(self, _session_id):
        self.entered.set()
        self.release.wait()
        self.finished.set()

    def end_session(self, *_args, **_kwargs):
        return None

    def close(self):
        return None


class HangingAgent:
    def __init__(self, release: threading.Event):
        self.release = release
        self.entered = threading.Event()
        self.finished = threading.Event()

    def close(self):
        self.entered.set()
        self.release.wait()
        self.finished.set()


def _run_while_cleanup_blocked(operation, cleanup):
    """Check ordering, not speed: the operation must return before cleanup is released.

    Imports and runner descheduling can exceed a short wall-clock budget without
    violating that contract. The generous waits here only guard against deadlocks.
    """
    result = Future()

    def run():
        try:
            result.set_result(operation())
        except BaseException as exc:
            result.set_exception(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        assert cleanup.entered.wait(timeout=30.0), "cleanup was never entered"
        value = result.result(timeout=30.0)
        assert not cleanup.finished.is_set(), "cleanup was not held until the operation returned"
        return value
    finally:
        # Unblock even a regressed, synchronous finalizer, and keep patches alive
        # until the operation has finished unwinding.
        cleanup.release.set()
        worker.join(timeout=30.0)
        assert not worker.is_alive(), "operation did not finish after releasing cleanup"
        if cleanup.entered.is_set():
            assert cleanup.finished.wait(timeout=30.0), "released cleanup did not finish"


def test_run_job_bounds_sessiondb_finalization(tmp_path):
    release = threading.Event()
    fake_db = HangingSessionDB(release)
    job = {"id": "cleanup-sessiondb-hang", "name": "test", "prompt": "hello"}

    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state_registry.acquire", return_value=fake_db), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=_RUNTIME), \
         patch("run_agent.AIAgent") as mock_agent_cls, \
         patch("cron.scheduler._cron_cleanup_timeout_seconds", return_value=0.02) as cleanup_timeout:
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "ok"}
        mock_agent_cls.return_value = mock_agent

        success, _output, final_response, error = _run_while_cleanup_blocked(
            lambda: run_job(job), fake_db)
        cleanup_timeout.assert_called_with()

    assert success is True
    assert final_response == "ok"
    assert error is None


def test_agent_teardown_is_bounded():
    release = threading.Event()
    agent = HangingAgent(release)

    with patch("cron.scheduler._run_cron_cleanup_with_timeout",
               wraps=_run_cron_cleanup_with_timeout) as bounded_cleanup:
        _run_while_cleanup_blocked(
            lambda: _teardown_cron_agent(agent, "cleanup-agent-hang", timeout_seconds=0.02), agent)
        bounded_cleanup.assert_called_once()
        assert bounded_cleanup.call_args.kwargs["timeout_seconds"] == 0.02


def test_detached_worker_teardown_waits_for_future():
    """A timed-out worker keeps its agent and SessionDB until its Future completes."""
    future = Future()
    fake_db = MagicMock()
    agent = MagicMock()

    with patch("cron.scheduler._finalize_cron_session") as finalize, \
         patch("cron.scheduler._teardown_cron_agent") as teardown_agent:
        assert defer_teardown_to_running_worker(
            future, fake_db, agent, "detached-worker", "detached worker", "cron_detached-worker") is True
        finalize.assert_not_called()
        teardown_agent.assert_not_called()

        future.set_result({"final_response": "late"})

        finalize.assert_called_once_with(fake_db, agent, "detached-worker", "detached worker",
                                         "cron_detached-worker", workdir=None)
        teardown_agent.assert_called_once_with(agent, "detached-worker")
    assert defer_teardown_to_running_worker(
        future, fake_db, agent, "detached-worker", "detached worker", "cron_detached-worker") is False


def test_dispatch_guard_releases_after_sessiondb_finalization_hang(tmp_path):
    """A second scheduler tick can fire the same job after cleanup times out."""
    import cron.scheduler as sched

    release = threading.Event()
    fake_db = HangingSessionDB(release)
    job = {
        "id": "cleanup-guard-hang",
        "name": "cleanup-guard-hang",
        "prompt": "hello",
        "schedule": "every 5m",
        "enabled": True,
        "next_run_at": "2020-01-01T00:00:00",
        "deliver": "local",
    }
    sched._parallel_pools.clear()
    sched._parallel_pool_max_workers.clear()
    sched._running_job_ids.clear()

    try:
        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
             patch("hermes_cli.env_loader.load_hermes_dotenv"), \
             patch("hermes_cli.env_loader.reset_secret_source_cache"), \
             patch("hermes_state_registry.acquire", return_value=fake_db), \
             patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=_RUNTIME), \
             patch("run_agent.AIAgent") as mock_agent_cls, \
             patch("cron.scheduler._cron_cleanup_timeout_seconds", return_value=0.02), \
             patch.object(sched, "get_due_jobs", return_value=[job]), \
             patch.object(sched, "advance_next_runs"), \
             patch.object(sched, "save_job_output", return_value="/tmp/out"), \
             patch.object(sched, "mark_job_run"), \
             patch.object(sched, "_deliver_result", return_value=None):
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent

            assert sched.tick(verbose=False) == 1
            assert "cleanup-guard-hang" not in sched.get_running_job_ids()
            assert sched.tick(verbose=False) == 1
    finally:
        release.set()
        sched._running_job_ids.discard(sched._inflight_key("cleanup-guard-hang"))
        sched._shutdown_parallel_pool()
