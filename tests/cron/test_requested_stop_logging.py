"""A stop someone asked for is not a failed job (cron-7c04faf754, third source).

Removing a running job (``remove_job``) or cancelling its run (``hermes cron cancel`` / Desktop
Stop) writes the execution's cancel marker; the run then stops at its next cancel check. That stop
is expected, so ``run_job`` logs one WARNING without a traceback, never the
``Job '<name>' failed:`` ERROR the self-heal detector files as a defect. A claim lost with no
cancel marker is a real ownership loss and keeps the ERROR with its traceback.

Composed through the real ``run_job`` with a fake agent, the real job store, execution ledger and
cancel markers.
"""

from __future__ import annotations

import logging
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cron import executions, jobs, scheduler
from cron.cancellation import CancelRequest, cancel_run

_RUNTIME = {
    "api_key": "test-key",
    "base_url": "https://example.invalid/v1",
    "provider": "openrouter",
    "api_mode": "chat_completions",
}


@pytest.fixture
def store(tmp_path):
    with jobs.use_cron_store(tmp_path / "cron"):
        yield tmp_path


def _run_and_stop(tmp_path, stop, claim_lost=None):
    """Start a real run of a real job; the fake agent's turn performs ``stop`` mid-run."""
    from gateway import session_cron
    from hermes_state import SessionDB

    job = jobs.create_job(prompt="hello", schedule="every 1h", name="stop-probe", deliver="local")
    execution_id = executions.create_execution(job["id"], source="test")["id"]
    executions.mark_execution_running(execution_id)
    claim_lost = threading.Event() if claim_lost is None else claim_lost
    fence = scheduler._FireOwnership(job, claim_lost, None, CancelRequest(execution_id))

    class _Agent:
        def __init__(self, *args, session_id=None, session_db=None, **kwargs):
            self.session_id = session_id

        def interrupt(self, message=None):
            pass

        def run_conversation(self, user_message, conversation_history=None, task_id=None):
            stop(job["id"], claim_lost)
            return {"final_response": "ok"}

    db = SessionDB(db_path=tmp_path / "state.db")
    token = session_cron._execution.set(
        (SimpleNamespace(db=db), f"cron_{job['id']}_owned", job["id"], execution_id))
    try:
        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
             patch("hermes_cli.env_loader.load_hermes_dotenv"), \
             patch("hermes_cli.env_loader.reset_secret_source_cache"), \
             patch("hermes_state_registry.acquire", return_value=db), \
             patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=_RUNTIME), \
             patch("run_agent.AIAgent", _Agent):
            return scheduler.run_job(
                dict(job, execution_id=execution_id), cancel_event=fence.cancel_event,
                execution_id=execution_id)
    finally:
        session_cron._execution.reset(token)
        db.close()


def _failed_errors(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR and "failed:" in r.getMessage()]


def _stop_warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING
            and "stopped on request" in r.getMessage()]


def _remove_mid_run(job_id, claim_lost):
    assert jobs.remove_job(job_id)
    claim_lost.set()  # removal also drops the fire claim; the heartbeat latches the loss


def _cancel_mid_run(job_id, claim_lost):
    assert cancel_run(job_id, reason="operator asked")["cancelled"]


@pytest.mark.parametrize("stop, reason", [
    (_remove_mid_run, "job removed while running"),
    (_cancel_mid_run, "operator asked"),
], ids=["remove_job", "cancel_run"])
def test_a_requested_stop_logs_one_warning_not_a_failed_job(store, caplog, stop, reason):
    caplog.set_level(logging.DEBUG, logger="cron.scheduler")

    success, _output, _final, error = _run_and_stop(store, stop)

    assert success is False
    assert reason in (error or "")
    assert _failed_errors(caplog) == []
    warnings = _stop_warnings(caplog)
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
    assert warnings[0].exc_info is None
    assert reason in warnings[0].getMessage()


def test_a_lost_claim_without_a_cancel_marker_still_logs_a_failed_job(store, caplog):
    caplog.set_level(logging.DEBUG, logger="cron.scheduler")

    success, _output, _final, _error = _run_and_stop(store, lambda job_id, lost: lost.set())

    assert success is False
    failed = _failed_errors(caplog)
    assert len(failed) == 1 and failed[0].exc_info is not None
    assert _stop_warnings(caplog) == []


def test_this_gateways_shutdown_wins_over_a_cancel_marker(store, caplog):
    """Both fired: an operator cancel wrote the marker, then this gateway's shutdown latched the
    claim and booked the interrupted outcome. The shutdown class wins (``_abort_if_fire_claim_lost``),
    so the run logs the shutdown WARNING, not "stopped on request", and leaves the shutdown's
    bookkeeping as written."""
    from cron.cancellation import SHUTDOWN_INTERRUPT_REASON, ShutdownAwareCancel

    caplog.set_level(logging.DEBUG, logger="cron.scheduler")
    seen = {}
    shutdown_fired = threading.Event()

    def _cancel_then_shutdown(job_id, latch):
        seen["job_id"] = job_id
        assert cancel_run(job_id, reason="operator asked")["cancelled"]
        # Gateway shutdown: flag the run interrupted, write the interrupted outcome, latch the claim.
        shutdown_fired.set()
        assert jobs.mark_job_run(job_id, False, SHUTDOWN_INTERRUPT_REASON, shutdown_interrupted=True)
        latch.set()

    latch = ShutdownAwareCancel(threading.Event(), shutdown_fired.is_set)
    success, _output, _final, error = _run_and_stop(store, _cancel_then_shutdown, claim_lost=latch)

    assert success is False
    assert (error or "").startswith("CronShutdownInterrupted:"), error
    assert _failed_errors(caplog) == []
    assert _stop_warnings(caplog) == []
    shutdown = [r for r in caplog.records if r.levelno == logging.WARNING
                and "stopped by gateway shutdown" in r.getMessage()]
    assert len(shutdown) == 1, [r.getMessage() for r in caplog.records]
    assert shutdown[0].exc_info is None
    job = jobs.get_job(seen["job_id"])
    assert job is not None
    assert job["last_status"] == "error"
    assert job["last_failure"]["kind"] == jobs.LIFECYCLE_INTERRUPT_KIND
    assert not job.get("failure_streak")
