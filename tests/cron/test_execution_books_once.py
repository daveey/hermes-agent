"""One cron execution books one outcome, whichever terminal paths observe it.

Hub incident (2026-11-12, job kanban-hub-pin-sweep): a drain made the restart-safe worker exit
after adopting execution 367fcebc; the gateway waiter booked the unknown outcome through
``record_unknown_worker_outcome`` and, 41 s later, the run's own bookkeeping booked "cron fire
ownership was lost" for the SAME execution. The first booking carried no ``execution_id``, so
``mark_job_run``'s per-execution dedupe (``canonical_completions``) could not see it and the
single fire left ``failure_streak == 2`` and spent two repeat slots.
"""

from datetime import datetime, timedelta, UTC

import pytest

import cron.scheduler as sched
from cron.jobs import claim_job_for_fire, create_job, get_job, mark_job_run, update_job
from cron.scheduler_worker_failure import record_unknown_worker_outcome

LOST = "RuntimeError: Cron job 'x' was stopped: cron fire ownership was lost"


@pytest.fixture
def fired(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(sched, "_deliver_crash_failure", lambda *_a, **_kw: (None, "suppressed"))
    monkeypatch.setattr(
        sched, "get_execution",
        lambda *_a, **_kw: {"status": "unknown", "error": "Restart-safe cron worker exited with status 1"})
    job = create_job("sweep", "every 60m")
    # A prior success, so the streak starts at 0 like the incident's 11:44Z run.
    assert mark_job_run(job["id"], True)
    update_job(job["id"], {"next_run_at": (datetime.now(UTC) - timedelta(seconds=5)).isoformat()})
    claimed = claim_job_for_fire(job["id"], return_job=True)
    assert claimed
    claimed["execution_id"] = "367fcebc0a04427c93bc4b5340a633e1"
    return claimed, get_job(job["id"])["repeat"]["completed"]


def test_worker_death_then_ownership_lost_books_one_failure(fired):
    claimed, completed = fired
    assert record_unknown_worker_outcome(claimed)
    # The gateway-side run_job finishes later and books the same execution without a claim owner.
    assert mark_job_run(claimed["id"], False, LOST, execution_id=claimed["execution_id"])
    job = get_job(claimed["id"])
    assert job["failure_streak"] == 1, job
    assert job["repeat"]["completed"] == completed + 1, job
    assert job["canonical_completions"][-1] == claimed["execution_id"]


def test_ownership_lost_then_worker_death_books_one_failure(fired):
    claimed, completed = fired
    assert mark_job_run(claimed["id"], False, LOST, execution_id=claimed["execution_id"])
    record_unknown_worker_outcome(claimed)
    job = get_job(claimed["id"])
    assert job["failure_streak"] == 1, job
    assert job["repeat"]["completed"] == completed + 1, job
