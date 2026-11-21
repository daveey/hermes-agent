"""Ready age starts at release, never at the beginning of a spend hold."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from hermes_cli import kanban_admission_score as score
from hermes_cli import kanban_chat_wait as cw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_parent_unclaimed as pu
from tests.hermes_cli import test_kanban_ready_wait_checkpoint as base
from tests.hermes_cli import test_kanban_spend_promote as spend

conn = base.conn
fleet = spend.fleet


def _assert_release(conn, tid, at):
    assert pu.ready_since(conn, tid, at - 10800) == at
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    assert score.rank(conn, [row], now_ms=(at + 4) * 1000)[0][1].age_buckets == 0
    base._subscribe(conn, tid)
    assert cw.sweep(conn, now=at + 4) == 0
    assert cw.sweep(conn, now=at + cw.THRESHOLD_SECONDS - 1) == 0
    assert cw.sweep(conn, now=at + cw.THRESHOLD_SECONDS) == 1
    assert cw.sweep(conn, now=at + cw.THRESHOLD_SECONDS + 1) == 0


def test_manual_promote_resets_three_hour_spend_hold(conn, monkeypatch):
    at = 1795292854
    monkeypatch.setattr("time.time", lambda: at - 10800)
    tid = kb.create_task(conn, title="bounded work", assignee="default")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        kb._append_event(conn, tid, "spend_budget_triaged", {"reason": "runs"})
    monkeypatch.setattr("time.time", lambda: at)
    assert kb.promote_task(conn, tid, actor="user")[0]
    _assert_release(conn, tid, at)


def test_spend_promote_resets_three_hour_spend_hold(fleet, monkeypatch):
    f = fleet
    import time

    at = int(time.time())
    episode = spend._stop(f, at=at - 10800)
    # Do not rewrite signed courier events or the fixture caller's live lease.
    with kb.write_txn(f.h):
        f.h.execute("UPDATE tasks SET created_at=? WHERE id=?", (at - 10800, f.target))
        f.h.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind='created'",
                    (at - 10800, f.target))
    cid = spend._decision_comment(f)
    at = int(time.time())
    monkeypatch.setattr("time.time", lambda: at)
    spend._submit(f, spend._op(f, episode, cid))
    spend._round(f)
    assert kb.get_task(f.h, f.target).status == "ready"
    _assert_release(f.h, f.target, at)


def test_long_wait_after_promotion_notifies_exactly_once(conn, monkeypatch):
    at = 1795292854
    monkeypatch.setattr("time.time", lambda: at - 10800)
    tid = kb.create_task(conn, title="long promotion wait", assignee="default")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        kb._append_event(conn, tid, "spend_budget_triaged", {})
    monkeypatch.setattr("time.time", lambda: at)
    assert kb.promote_task(conn, tid, actor="user")[0]
    base._subscribe(conn, tid)
    assert cw.sweep(conn, now=at + cw.THRESHOLD_SECONDS) == 1
    assert cw.sweep(conn, now=at + cw.THRESHOLD_SECONDS + 1) == 0


# Independently enumerate the audited entry paths, not the registry under test.
@pytest.mark.parametrize("kind", [
    "created", "promoted", "promoted_manual", "triage_swept", "containment_released",
    "review_reopened", "reopened", "changes_requested", "reclaimed", "unblocked",
    "continuation_woken", "condition_met", "wake_probe_stale", "wake_check_stale", "owner_restart_lost",
])
def test_ready_entry_path_floor(conn, kind):
    tid = kb.create_task(conn, title=kind, assignee="default")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET created_at=100 WHERE id=?", (tid,))
        conn.execute("UPDATE task_events SET created_at=100 WHERE task_id=?", (tid,))
        kb._append_event(conn, tid, kind, {"status": "ready"})
        conn.execute("UPDATE task_events SET created_at=200 WHERE task_id=? AND kind=?", (tid, kind))
    assert pu.ready_since(conn, tid, 100) >= 200
    assert score._ready_since(conn, [tid])[tid] >= 200
    assert f"'{kind}'" in cw._floor_kinds()


# Non-entry observations in status-writing functions. New event kinds require an
# explicit audit even when a writer uses a bound status parameter, different SQL
# whitespace, or a bare (rather than facade-qualified) _append_event call.
NON_ENTRY_WRITER_EVENTS = {
    "continuation_exhausted", "blocked", "spend_budget_held", "gave_up",
    "dependency_wait", "linked", "claim_rejected", "completed", "review_requested",
    "descendant_invalidated", "status", "scheduled", "assigned", "supervisor_escalated",
    "archive_worker_termination", "archived", "request_withdrawn", "claimed",
    "agent_block_escalated", "timed_out", "stale", "reconciled", "parent_found",
    "parent_missing", "status_healed",
    # Preclaim observation; exhaustion moves to triage, never releases to ready.
    "review_admission_refused",
}


def test_review_admission_refusal_does_not_reset_ready_age(conn):
    tid = kb.create_task(conn, title="review refusal observation", assignee="default")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET created_at=100 WHERE id=?", (tid,))
        conn.execute("UPDATE task_events SET created_at=100 WHERE task_id=?", (tid,))
        kb._append_event(conn, tid, "review_admission_refused", {"preclaim": True})
        conn.execute(
            "UPDATE task_events SET created_at=200 WHERE task_id=? AND kind=?",
            (tid, "review_admission_refused"),
        )
    assert pu.ready_since(conn, tid, 100) == 100
    assert score._ready_since(conn, [tid])[tid] == 100
    assert "'review_admission_refused'" not in cw._floor_kinds()


def _writer_events(source):
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        strings = [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        if not any(re.search(r"UPDATE\s+tasks\s+SET\s+(?:(?!WHERE).)*\bstatus\s*=", s, re.IGNORECASE | re.DOTALL)
                   for s in strings):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call) or len(call.args) < 3:
                continue
            name = (call.func.attr if isinstance(call.func, ast.Attribute)
                    else call.func.id if isinstance(call.func, ast.Name) else "")
            if name == "_append_event" and isinstance(call.args[2], ast.Constant):
                yield call.args[2].value


def test_status_writers_register_new_event_kinds():
    from hermes_cli.kanban_ready_events import READY_ENTRY_EVENT_KINDS

    found = set()
    for path in Path(kb.__file__).parent.glob("kanban*.py"):
        events = set(_writer_events(path.read_text()))
        assert events <= READY_ENTRY_EVENT_KINDS | NON_ENTRY_WRITER_EVENTS, (path.name, events)
        found |= events
    assert {"promoted_manual", "triage_swept", "review_reopened", "reopened", "changes_requested"} <= found


@pytest.mark.parametrize("sql", ["status='ready'", "status = ?"])
@pytest.mark.parametrize("writer", ["_append_event", "kb._append_event"])
def test_writer_inventory_detects_unregistered_new_kind(sql, writer):
    source = f'''def new_path(conn):
        conn.execute("UPDATE tasks SET {sql} WHERE id=?")
        {writer}(conn, tid, "new_ready_path", {{}})
    '''
    from hermes_cli.kanban_ready_events import READY_ENTRY_EVENT_KINDS

    assert set(_writer_events(source)) - READY_ENTRY_EVENT_KINDS - NON_ENTRY_WRITER_EVENTS == {"new_ready_path"}
