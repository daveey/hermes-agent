"""Only the owning worker's PR publication can supersede its checkpoint."""
import json
import time

import pytest

from hermes_cli import kanban_continuation as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch_pr_state as prs
from tests.hermes_cli.test_kanban_park_invariants import kanban_home

__all__ = ["kanban_home"]
pytestmark = pytest.mark.usefixtures("kanban_home")
URL = "https://github.com/example/project/pull/12"
NEW_URL = "https://github.com/example/project/pull/13"


def _worker_comment(conn, tid, run_id, monkeypatch, body):
    with monkeypatch.context() as env:
        env.setenv("HERMES_KANBAN_TASK", tid)
        env.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
        return kb.add_comment(conn, tid, "a", body)


@pytest.mark.parametrize("writer", ["chat", "foreign_worker", "foreign_run"])
def test_foreign_pr_comment_after_continued_does_not_rearm(monkeypatch, writer):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    monkeypatch.setattr(prs, "pr_state", lambda url: "OPEN")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="finish owned PR", assignee="a")
        kb.claim_task(conn, tid)
        run_id = conn.execute("SELECT id FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0]
        _worker_comment(conn, tid, run_id, monkeypatch, f"Opened {URL}")
        monkeypatch.setattr(time, "time", lambda: now + 1)
        assert kc.continue_task(conn, tid, checkpoint="merge existing PR", resume_after_seconds=60)["ok"]
        monkeypatch.setattr(time, "time", lambda: now + 2)
        if writer == "chat":
            monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
            kb.add_comment(conn, tid, "a", f"FYI sibling PR {NEW_URL}")
        else:
            other = kb.create_task(conn, title="sibling", assignee="a")
            kb.claim_task(conn, other)
            other_run = conn.execute("SELECT id FROM task_runs WHERE task_id=?", (other,)).fetchone()[0]
            # Normal cross-card writes omit writer_run_id; a mismatched run must
            # also be rejected even if an imported event explicitly carries it.
            cid = _worker_comment(conn, other if writer == "foreign_worker" else tid,
                                  other_run, monkeypatch, f"FYI {NEW_URL}")
            if writer == "foreign_worker":
                with monkeypatch.context() as env:
                    env.setenv("HERMES_KANBAN_TASK", other)
                    env.setenv("HERMES_KANBAN_RUN_ID", str(other_run))
                    kb.add_comment(conn, tid, "a", f"FYI {NEW_URL}")
            else:
                assert cid
        assert prs.active_pr_reason(conn, tid, now + 62) is None


@pytest.mark.parametrize("provenance", [
    "own", "missing_event", "malformed_event", "invalid_json", "unknown_run", "boolean_run", "duplicate_event",
])
def test_own_or_unreadable_pr_comment_after_continued_still_guards(monkeypatch, provenance):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    monkeypatch.setattr(prs, "pr_state", lambda url: "OPEN")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="finish owned PR", assignee="a")
        kb.claim_task(conn, tid)
        run_id = conn.execute("SELECT id FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0]
        _worker_comment(conn, tid, run_id, monkeypatch, f"Opened {URL}")
        monkeypatch.setattr(time, "time", lambda: now + 1)
        assert kc.continue_task(conn, tid, checkpoint="merge existing PR", resume_after_seconds=60)["ok"]
        monkeypatch.setattr(time, "time", lambda: now + 2)
        cid = _worker_comment(conn, tid, run_id, monkeypatch, f"New publication {NEW_URL}")
        event = conn.execute("SELECT id FROM task_events WHERE task_id=? AND kind='commented' ORDER BY id DESC LIMIT 1",
                             (tid,)).fetchone()[0]
        mutations = {
            "missing_event": ("DELETE FROM task_events WHERE id=?", (event,)),
            "malformed_event": ("UPDATE task_events SET payload=? WHERE id=?",
                                (json.dumps({"comment_id": cid, "writer_run_id": []}), event)),
            "unknown_run": ("UPDATE task_events SET payload=? WHERE id=?",
                            (json.dumps({"comment_id": cid, "writer_run_id": -1}), event)),
            "boolean_run": ("UPDATE task_events SET payload=? WHERE id=?",
                            (json.dumps({"comment_id": cid, "writer_run_id": True}), event)),
            "invalid_json": ("UPDATE task_events SET payload='{' WHERE id=?", (event,)),
            "duplicate_event": (
                "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'commented', ?, ?)",
                (tid, json.dumps({"comment_id": cid}), now + 2),
            ),
        }
        if provenance in mutations:
            conn.execute(*mutations[provenance])
        assert prs.active_pr_reason(conn, tid, now + 62) == "active_pr"
