"""A finished Actions run may list no jobs (cancelled before any was created): its own result wakes the card."""
import pytest

from hermes_cli import kanban_ci_progress as kcp
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_external_watch as kew
from hermes_cli import kanban_wake_check as kwc
from tests.hermes_cli import test_kanban_external_watch as h

kanban_home, clock = h.kanban_home, h.clock


def _no_jobs(repo, run_id):
    return {"total_count": 0, "jobs": []}


@pytest.mark.parametrize("conclusion,outcome", [
    ("cancelled", "cancelled"), ("failure", "failed"), ("success", "succeeded"),
])
def test_completed_run_without_jobs_wakes_with_its_result(kanban_home, clock, conclusion, outcome):
    reads = []

    def fetch_run(repo, run_id):
        reads.append(run_id)
        return {"status": "completed", "conclusion": conclusion}

    with kbc.connect_closing() as conn:
        tid = h.park(conn, 9101)
        woke = kcp.sweep(conn, fetch=_no_jobs, fetch_run=fetch_run, fetch_runners=lambda repo: [])
        assert reads == ["9101"]
        assert woke == [tid]
        assert h.status(conn, tid) == "ready"
        assert h.events(conn, tid, kew.WATCH_EVENT)[-1]["job"] == outcome
        assert h.events(conn, tid, kwc.CONDITION_MET_EVENT)[-1]["external_result"]["outcome"] == outcome


@pytest.mark.parametrize("run_status", ["queued", "in_progress", "pending", None])
def test_unfinished_run_without_jobs_stays_queued(kanban_home, clock, run_status):
    with kbc.connect_closing() as conn:
        tid = h.park(conn, 9102)
        woke = kcp.sweep(conn, fetch=_no_jobs, fetch_runners=lambda repo: [],
                         fetch_run=lambda repo, run_id: {"status": run_status, "conclusion": None})
        assert woke == []
        assert h.status(conn, tid) == "scheduled"
        rec = h.events(conn, tid, kew.WATCH_EVENT)[-1]
        assert (rec["job"], rec["detail"]) == ("queued", "no jobs created yet")
        assert not h.events(conn, tid, kwc.CONDITION_MET_EVENT)


def test_completed_run_without_jobs_and_no_known_result_stays_parked(kanban_home, clock):
    with kbc.connect_closing() as conn:
        tid = h.park(conn, 9103)
        woke = kcp.sweep(conn, fetch=_no_jobs, fetch_runners=lambda repo: [],
                         fetch_run=lambda repo, run_id: {"status": "completed", "conclusion": None})
        assert woke == []
        assert h.status(conn, tid) == "scheduled"
        assert h.events(conn, tid, kew.WATCH_EVENT)[-1]["job"] == "unknown"
