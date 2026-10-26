"""An explicit checkpoint resumes owned PR work; guard notices cannot rearm holds."""
import time

import pytest

from hermes_cli import kanban_continuation as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_dispatch_pr_state as prs
from tests.hermes_cli.test_kanban_park_invariants import kanban_home

__all__ = ["kanban_home"]  # register the shared isolated-board fixture
pytestmark = pytest.mark.usefixtures("kanban_home")

URL = "https://github.com/example/project/pull/12"


@pytest.fixture(autouse=True)
def open_pr(monkeypatch):
    monkeypatch.setattr(prs, "pr_state", lambda url: "OPEN")


def test_immediate_checkpoint_with_open_pr_dispatches(monkeypatch, all_assignees_spawnable):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    spawned = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="finish owned PR", assignee="a")
        kb.claim_task(conn, tid)
        kb.add_comment(conn, tid, "a", f"Draft work remains: {URL}")
        result = kc.continue_task(conn, tid, checkpoint="Continue implementation of existing PR")
        assert result["ok"] and result["status"] == "ready"
        monkeypatch.setattr(time, "time", lambda: now + 1)
        kb.add_comment(conn, tid, "kanban-dispatcher", f"[respawn-guard] Held on {URL}")
        dispatched = kbd.dispatch_once(conn, spawn_fn=lambda task, *a, **k: spawned.append(task.id) or 1)
        assert tid not in dict(dispatched.respawn_guarded)
        assert spawned == [tid]
        assert "Continue implementation of existing PR" in kb.build_worker_context(conn, tid)


def test_guard_notice_cannot_supersede_real_handoff(monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="review fixes", assignee="a")
        kb.add_comment(conn, tid, "a", f"PR {URL}")
        monkeypatch.setattr(time, "time", lambda: now + 1)
        kb._append_event(conn, tid, "unblocked", {"reason": "finish this PR"})
        monkeypatch.setattr(time, "time", lambda: now + 2)
        kb.add_comment(conn, tid, "kanban-dispatcher", f"[respawn-guard] Held on {URL}")
        assert kbd.check_respawn_guard(conn, tid) is None
        # An actual later work report still forms a new barrier.
        kb.add_comment(conn, tid, "a", f"Waiting for external owner: {URL}")
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


@pytest.mark.parametrize("author,body,expected", [
    ("a", "Focused checks passed", None),
    ("a", f"New PR report {URL}", "active_pr"),
    ("kanban-dispatcher", f"New PR report {URL}", "active_pr"),
    ("kanban-dispatcher", f"[respawn-guard] Held on {URL}", None),
    ("a", f"[respawn-guard] Held on {URL}", "active_pr"),
])
@pytest.mark.parametrize("delay", [0, 1])
def test_checkpoint_orders_only_real_pr_comments(monkeypatch, author, body, expected, delay):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="owned PR", assignee="a")
        kb.claim_task(conn, tid)
        kb.add_comment(conn, tid, "a", f"Draft {URL}")
        assert kc.continue_task(conn, tid, checkpoint="finish owned PR")["ok"]
        monkeypatch.setattr(time, "time", lambda: now + delay)
        kb.add_comment(conn, tid, author, body)
        assert kbd.check_respawn_guard(conn, tid) == expected


@pytest.mark.parametrize("watermark", [None, True, "1", -1])
def test_invalid_or_legacy_checkpoint_watermark_fails_closed(monkeypatch, watermark):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="legacy checkpoint", assignee="a")
        kb.claim_task(conn, tid)
        kb.add_comment(conn, tid, "a", f"Draft {URL}")
        result = kc.continue_task(conn, tid, checkpoint="finish", metadata={"comment_id_at_checkpoint": 999999})
        assert result["ok"]
        row = conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND kind='continued'",
                           (tid,)).fetchone()
        data = kb._json_dict(row["payload"])
        assert data["comment_id_at_checkpoint"] != 999999
        data["comment_id_at_checkpoint"] = watermark
        import json
        conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(data), row["id"]))
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


@pytest.mark.parametrize("mode", ["scheduled", "crashed", "new_pr_report", "missing_checkpoint"])
def test_non_immediate_or_superseded_continuation_still_guards(monkeypatch, mode):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="external wait", assignee="a")
        kb.claim_task(conn, tid)
        kb.add_comment(conn, tid, "a", f"PR {URL}")
        result = kc.continue_task(conn, tid, checkpoint="Existing PR",
                                  resume_after_seconds=60 if mode == "scheduled" else 0)
        assert result["ok"]
        if mode == "scheduled":
            kc.wake_due_continuations(conn, now=now + 61)
        elif mode == "crashed":
            kb.claim_task(conn, tid)
            kb._end_run(conn, tid, outcome="crashed", status="crashed", summary="worker failed")
        elif mode == "new_pr_report":
            monkeypatch.setattr(time, "time", lambda: now + 1)
            kb.add_comment(conn, tid, "a", f"External PR remains: {URL}")
        else:
            conn.execute("UPDATE task_runs SET metadata='{}' WHERE id=?", (result["run_id"],))
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"
