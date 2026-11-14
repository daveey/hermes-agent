"""Timed review waits permit one exact-current-head repair, never merge approval."""
import pytest

from hermes_cli import kanban_awaited_review as kar
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch_pr_state as prs
from tests.hermes_cli import test_kanban_active_pr_awaited_review as base

kanban_home = base.kanban_home
clock = base.clock
pytestmark = pytest.mark.usefixtures("kanban_home")
REPAIR = {"verdict": "request_changes", "reviewed_sha": base.HEAD, "reviewed_pr": base.URL}


def _owner(conn, clock, monkeypatch, *, kind="scheduled", via="awaiting", metadata=None, finish=True):
    rev = kb.create_task(conn, title="exact-head review", assignee="reviewer")
    tid, run_id = base._owner(conn, monkeypatch, synced=True)
    base._comment(conn, tid, run_id, f"Opened {base.URL}", monkeypatch)
    # Missing comment provenance keeps the generic same-second handoff closed.
    with kb.write_txn(conn):
        conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='commented'", (tid,))
    kwargs = ({"awaiting": {"kind": "card", "ref": rev}} if via == "awaiting" else
              {"wake_check": f"hermes kanban wait-check task {rev}"})
    if kind == "scheduled":
        assert kb.schedule_task(conn, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z review",
                                expected_run_id=run_id, **kwargs)
    else:
        assert base.kc.continue_task(conn, tid, checkpoint="waiting on exact review", resume_after_seconds=60,
                                     expected_run_id=run_id, **kwargs)["ok"]
    clock[0] += 60
    if finish:
        base._finish(conn, rev, metadata if metadata is not None else REPAIR)
    base._wake(conn, tid)
    return tid, rev


@pytest.mark.parametrize("via", ["wake_check", "awaiting"])
@pytest.mark.parametrize("kind", ["scheduled", "continued"])
def test_timed_request_changes_resumes_repair_once(clock, monkeypatch, via, kind):
    with kbc.connect() as conn:
        tid, rev = _owner(conn, clock, monkeypatch, kind=kind, via=via)
        assert prs.active_pr_reason(conn, tid, clock[0] + 10) is None
        # Guard reads are idempotent, and do not rewrite the review as approval.
        assert prs.active_pr_reason(conn, tid, clock[0] + 10) is None
        row = conn.execute("SELECT metadata FROM task_runs WHERE task_id=?", (rev,)).fetchone()
        assert kb._json_dict(row["metadata"])["verdict"] == "request_changes"
        kb.claim_task(conn, tid)
        assert prs.active_pr_reason(conn, tid, clock[0] + 10) == "active_pr"
        assert kb.reclaim_task(conn, tid, reason="worker died", signal_fn=lambda *_: None)
        assert prs.active_pr_reason(conn, tid, clock[0] + 10) == "active_pr"


@pytest.mark.parametrize("metadata", [
    REPAIR | {"reviewed_sha": "b" * 40},
    REPAIR | {"reviewed_sha": "a" * 39},
    REPAIR | {"reviewed_sha": "g" * 40},
    REPAIR | {"reviewed_sha": base.HEAD + "\n"},
    REPAIR | {"reviewed_sha": None},
    REPAIR | {"reviewed_pr": base.OTHER},
    {"verdict": "request_changes", "reviewed_sha": base.HEAD},
    REPAIR | {"verdict": "uncertain"},
])
def test_invalid_repair_evidence_keeps_hold(clock, monkeypatch, metadata):
    with kbc.connect() as conn:
        tid, _ = _owner(conn, clock, monkeypatch, metadata=metadata)
        assert prs.active_pr_reason(conn, tid, clock[0] + 10) == "active_pr"


@pytest.mark.parametrize("mutation", ["unfinished", "stale", "unclaimed_review", "unclaimed_owner",
                                     "foreign_park", "crashed", "reclaimed", "timed_out", "unended",
                                     "unknown_head", "moved_on_reread", "uncovered_pr"])
def test_repair_requires_authenticated_current_park_and_review(clock, monkeypatch, mutation):
    with kbc.connect() as conn:
        tid, rev = _owner(conn, clock, monkeypatch, finish=mutation != "unfinished")
        if mutation == "unknown_head":
            monkeypatch.setattr(kar, "pr_head", lambda *a, **kw: None)
        elif mutation == "moved_on_reread":
            monkeypatch.setattr(kar, "pr_head", lambda *a, **kw: "b" * 40 if kw else base.HEAD)
        sql_cases = {
            "stale": ("UPDATE task_runs SET ended_at=ended_at-3600 WHERE task_id=?", (rev,)),
            "unclaimed_review": ("DELETE FROM task_events WHERE task_id=? AND kind='claimed'", (rev,)),
            "unclaimed_owner": ("DELETE FROM task_events WHERE task_id=? AND kind='claimed'", (tid,)),
            "foreign_park": (
                "UPDATE task_events SET run_id=(SELECT MAX(id) FROM task_runs WHERE task_id=?) "
                "WHERE task_id=? AND kind='scheduled'", (rev, tid)),
            "unended": ("UPDATE task_runs SET ended_at=NULL WHERE task_id=?", (tid,)),
            **{outcome: ("UPDATE task_runs SET outcome=? WHERE task_id=?", (outcome, tid))
               for outcome in ("crashed", "reclaimed", "timed_out")},
        }
        with kb.write_txn(conn):
            if mutation in sql_cases:
                conn.execute(*sql_cases[mutation])
        urls = [base.URL, base.OTHER] if mutation == "uncovered_pr" else [base.URL]
        assert kar.resumes_awaited_review(conn, tid, urls) is False


@pytest.mark.parametrize("metadata", [REPAIR | {"reviewed_pr": base.OTHER},
                                      REPAIR | {"reviewed_sha": "b" * 40},
                                      REPAIR | {"verdict": "unknown"}])
def test_valid_sibling_does_not_outvote_invalid_repair(clock, monkeypatch, metadata):
    from tests.hermes_cli.test_kanban_active_pr_awaited_head import _rewrite_park

    with kbc.connect() as conn:
        tid, _ = _owner(conn, clock, monkeypatch, via="wake_check")
        second = kb.create_task(conn, title="second review", assignee="reviewer")
        base._finish(conn, second, metadata)
        _rewrite_park(conn, tid, lambda p: p | {"awaiting": {"kind": "card", "ref": second}})
        assert kar.resumes_awaited_review(conn, tid, [base.URL]) is False


def test_synced_park_uses_remapped_event_fk_not_payload_run_id(clock, monkeypatch):
    with kbc.connect() as conn:
        tid, _ = _owner(conn, clock, monkeypatch)
        run = kar._park(conn, tid)[0]
        original_uid = run["uid"]
        # Mirror ingestion remaps relational FKs while retaining JSON source IDs.
        with kb.write_txn(conn):
            conn.execute("PRAGMA defer_foreign_keys=ON")
            conn.execute("UPDATE task_runs SET id=id+1000 WHERE id=?", (run["id"],))
            conn.execute("UPDATE task_events SET run_id=run_id+1000 WHERE run_id=?", (run["id"],))
        assert kar._park(conn, tid)[0]["uid"] == original_uid
        assert kar.resumes_awaited_review(conn, tid, [base.URL]) is True

