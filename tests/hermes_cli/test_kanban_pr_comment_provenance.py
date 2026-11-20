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


def _park(conn, tid, kind):
    if kind == "scheduled":
        assert kb.schedule_task(conn, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z review of the PR",
                                clock_only=True, awaiting={"kind": "clock", "why": "PR review window"})
    else:
        assert kc.continue_task(conn, tid, checkpoint="wait for review", resume_after_seconds=60)["ok"]


def _owner_parked_then_noted(conn, monkeypatch, now, kind, note=URL, synced=False):
    """Run R reports ``URL``, parks orderly at now+1, then writes its checkpoint note at now+8
    (2026-11-12 MacBook t_01a1cff0541bf30c: park 06:48:32, own checkpoint comment 06:48:39).
    ``synced``: a Board-synced schema, where runs carry a portable uid and comments name it."""
    if synced:
        conn.execute("ALTER TABLE task_runs ADD COLUMN uid TEXT")
    tid = kb.create_task(conn, title="finish owned PR", assignee="a")
    kb.claim_task(conn, tid)
    run_id = conn.execute("SELECT id FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0]
    if synced:
        conn.execute("UPDATE task_runs SET uid = '01a1d022-04f4-7ae7-b583-' || printf('%012d', id) WHERE id=?",
                     (run_id,))
    _worker_comment(conn, tid, run_id, monkeypatch, f"Opened {URL}")
    monkeypatch.setattr(time, "time", lambda: now + 1)
    _park(conn, tid, kind)
    monkeypatch.setattr(time, "time", lambda: now + 8)
    _worker_comment(conn, tid, run_id, monkeypatch, f"Checkpoint: waiting on review of {note}")
    return tid, run_id


@pytest.mark.parametrize("synced", [False, True], ids=["local", "synced"])
@pytest.mark.parametrize("kind", ["scheduled", "continued"])
def test_own_checkpoint_note_after_orderly_park_keeps_the_park_as_handoff(monkeypatch, kind, synced):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    monkeypatch.setattr(prs, "pr_state", lambda url: "OPEN")
    with kbc.connect() as conn:
        tid, _run = _owner_parked_then_noted(conn, monkeypatch, now, kind, synced=synced)
        assert prs.active_pr_reason(conn, tid, now + 700) is None


@pytest.mark.parametrize("kind", ["scheduled", "continued"])
def test_own_note_after_park_naming_a_new_pr_still_guards(monkeypatch, kind):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    monkeypatch.setattr(prs, "pr_state", lambda url: "OPEN")
    with kbc.connect() as conn:
        tid, _run = _owner_parked_then_noted(conn, monkeypatch, now, kind, note=NEW_URL)
        assert prs.active_pr_reason(conn, tid, now + 700) == "active_pr"


@pytest.mark.parametrize("kind", ["scheduled", "continued"])
def test_pr_note_after_park_from_another_run_of_the_card_still_guards(monkeypatch, kind):
    """A lingering earlier run of the same card is not the run that parked."""
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    monkeypatch.setattr(prs, "pr_state", lambda url: "OPEN")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="finish owned PR", assignee="a")
        kb.claim_task(conn, tid)
        first = conn.execute("SELECT id FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0]
        _worker_comment(conn, tid, first, monkeypatch, f"Opened {URL}")
        assert kc.continue_task(conn, tid, checkpoint="next run", resume_after_seconds=0)["ok"]
        monkeypatch.setattr(time, "time", lambda: now + 1)
        kb.claim_task(conn, tid)
        monkeypatch.setattr(time, "time", lambda: now + 2)
        _park(conn, tid, kind)
        monkeypatch.setattr(time, "time", lambda: now + 9)
        _worker_comment(conn, tid, first, monkeypatch, f"Checkpoint: waiting on review of {URL}")
        assert prs.active_pr_reason(conn, tid, now + 700) == "active_pr"


@pytest.mark.parametrize("gap", [0, 1], ids=["same_second_as_claim", "after_claim"])
def test_parked_run_note_after_a_later_claim_still_guards(monkeypatch, gap):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    monkeypatch.setattr(prs, "pr_state", lambda url: "OPEN")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="finish owned PR", assignee="a")
        kb.claim_task(conn, tid)
        run_id = conn.execute("SELECT id FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0]
        _worker_comment(conn, tid, run_id, monkeypatch, f"Opened {URL}")
        monkeypatch.setattr(time, "time", lambda: now + 1)
        _park(conn, tid, "scheduled")
        monkeypatch.setattr(time, "time", lambda: now + 600)
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))  # the wake
        kb.claim_task(conn, tid)
        monkeypatch.setattr(time, "time", lambda: now + 600 + gap)
        _worker_comment(conn, tid, run_id, monkeypatch, f"Still on {URL}")
        assert prs.active_pr_reason(conn, tid, now + 700) == "active_pr"


def test_pr_notes_with_no_park_still_guard(monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    monkeypatch.setattr(prs, "pr_state", lambda url: "OPEN")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="finish owned PR", assignee="a")
        kb.claim_task(conn, tid)
        run_id = conn.execute("SELECT id FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0]
        _worker_comment(conn, tid, run_id, monkeypatch, f"Opened {URL}")
        monkeypatch.setattr(time, "time", lambda: now + 8)
        _worker_comment(conn, tid, run_id, monkeypatch, f"Checkpoint {URL}")
        conn.execute("UPDATE task_runs SET status='crashed', outcome='crashed', ended_at=? WHERE id=?",
                     (now + 9, run_id))
        assert prs.active_pr_reason(conn, tid, now + 700) == "active_pr"


def test_chat_note_after_scheduled_park_stays_fyi(monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    monkeypatch.setattr(prs, "pr_state", lambda url: "OPEN")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="finish owned PR", assignee="a")
        kb.claim_task(conn, tid)
        run_id = conn.execute("SELECT id FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0]
        _worker_comment(conn, tid, run_id, monkeypatch, f"Opened {URL}")
        monkeypatch.setattr(time, "time", lambda: now + 1)
        _park(conn, tid, "scheduled")
        monkeypatch.setattr(time, "time", lambda: now + 8)
        kb.add_comment(conn, tid, "a", f"FYI review of {URL} is running")
        assert prs.active_pr_reason(conn, tid, now + 700) is None


_FOREIGN_UID = "01a1d022-0000-7000-8000-000000000000"


def _set(key, value):
    return lambda conn, tid, run_id, data, raw, now: data.__setitem__(key, value(run_id) if callable(value) else value)


def _uid_less_run(conn, tid, run_id, data, raw, now):  # a legacy uid-less note against a uid-less run
    conn.execute("UPDATE task_runs SET uid=NULL WHERE id=?", (run_id,))
    data.pop("writer_run_uid")


def _missing_park_run(conn, tid, run_id, data, raw, now):  # the park and the note name a run with no row
    conn.execute("UPDATE task_events SET run_id=? WHERE task_id=? AND kind='scheduled'", (run_id + 50, tid))
    data["writer_run_id"] = run_id + 50


_SYNCED_PROVENANCE = {
    "no_uid": lambda conn, tid, run_id, data, raw, now: data.pop("writer_run_uid"),  # an integer can't vouch here
    "foreign_uid": _set("writer_run_uid", _FOREIGN_UID),
    "uid_less_park_run": _uid_less_run,
    "duplicate_event": lambda conn, tid, run_id, data, raw, now: conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'commented', ?, ?)", (tid, raw, now + 8)),
}
_LOCAL_PROVENANCE = {
    "boolean_run": _set("writer_run_id", True),
    "other_run": _set("writer_run_id", lambda run_id: run_id + 1),
    "stray_uid": _set("writer_run_uid", _FOREIGN_UID),  # unsynced schema: unreadable
    "unmatched_row": lambda conn, tid, run_id, data, raw, now: conn.execute(
        "UPDATE task_comments SET author='someone-else' WHERE id=?", (data["comment_id"],)),
    "missing_park_run": _missing_park_run,
    "unreadable_park_time": lambda conn, tid, run_id, data, raw, now: conn.execute(
        "UPDATE task_events SET created_at='later' WHERE task_id=? AND kind='scheduled'", (tid,)),
    "unreadable_claim_time": lambda conn, tid, run_id, data, raw, now: conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'claimed', '{}', 'x')", (tid,)),
}


@pytest.mark.parametrize("provenance", [*_SYNCED_PROVENANCE, *_LOCAL_PROVENANCE])
def test_unproven_writer_of_post_park_note_still_guards(monkeypatch, provenance):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    monkeypatch.setattr(prs, "pr_state", lambda url: "OPEN")
    with kbc.connect() as conn:
        tid, run_id = _owner_parked_then_noted(conn, monkeypatch, now, "scheduled",
                                               synced=provenance in _SYNCED_PROVENANCE)
        event, raw = conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND kind='commented' "
                                  "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        data = json.loads(raw)
        {**_SYNCED_PROVENANCE, **_LOCAL_PROVENANCE}[provenance](conn, tid, run_id, data, raw, now)
        conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(data), event))
        assert prs.active_pr_reason(conn, tid, now + 700) == "active_pr"
