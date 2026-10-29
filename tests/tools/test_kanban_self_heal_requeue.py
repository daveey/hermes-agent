"""A self-heal fix card may requeue the one card named in its defect.

Ordinary workers stay refused (kanban_unblock is orchestrator-only, and the
worker shell stays fenced). The grant is the fix card itself, or a card that
finished fix card created, and only the single ``kanban card t_...`` id.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]


@pytest.fixture
def board(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    for key in (
        "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_HOME",
        "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_WORKER_TASK",
    ):
        monkeypatch.delenv(key, raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _triage(conn, kb, title="crashed card"):
    tid = kb.create_task(conn, title=title, assignee="default")
    conn.execute(
        "UPDATE tasks SET status='triage', consecutive_failures=3, "
        "last_failure_error='crash' WHERE id=?",
        (tid,),
    )
    conn.commit()
    return tid


def _fix(conn, kb, target, *, key="selfheal:kanban-50a03854e8:fix", created_by="self-heal",
         title=None, status=None):
    title = title or (
        f"self-heal fix: kanban card {target} on hermes-hub/default failed 3x (crashed):"
    )
    tid = kb.create_task(
        conn, title=title, created_by=created_by, idempotency_key=key, assignee="default",
    )
    if status:
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        conn.commit()
    return tid


def _as_worker(monkeypatch, conn, kb, tid):
    kb.claim_task(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(kb._current_run_id(conn, tid)))


def _unblock(target):
    from tools import kanban_tools as kt
    return json.loads(kt._handle_unblock({"task_id": target}))


def test_self_heal_fix_worker_requeues_named_triage_card(board, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect()
    target = _triage(conn, kb)
    fix = _fix(conn, kb, target)
    _as_worker(monkeypatch, conn, kb, fix)
    conn.close()

    out = _unblock(target)
    assert out.get("ok") is True, out
    assert out.get("status") == "ready"
    assert out.get("via") == "self-heal-requeue"

    conn = kbc.connect()
    try:
        task = kb.get_task(conn, target)
        assert task is not None
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        assert task.last_failure_error is None
        events = [e for e in kb.list_events(conn, target) if e.kind == "self_heal_requeue"]
        assert len(events) == 1
        assert events[0].payload == {"by": fix, "fix_card": fix, "target": target}
        assert any(e.kind == "unblocked" for e in kb.list_events(conn, target))
    finally:
        conn.close()


def test_self_heal_fix_worker_cannot_requeue_a_different_card(board, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect()
    named = _triage(conn, kb, "named")
    other = _triage(conn, kb, "other")
    fix = _fix(conn, kb, named)
    _as_worker(monkeypatch, conn, kb, fix)
    conn.close()

    out = _unblock(other)
    assert out.get("ok") is not True
    assert "orchestrator-only" in out.get("error", "")

    conn = kbc.connect()
    try:
        assert kb.get_task(conn, other).status == "triage"
        assert kb.get_task(conn, named).status == "triage"
    finally:
        conn.close()


def test_ordinary_worker_still_cannot_unblock(board, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect()
    target = _triage(conn, kb)
    worker = kb.create_task(conn, title="plain worker", assignee="default")
    _as_worker(monkeypatch, conn, kb, worker)
    conn.close()

    out = _unblock(target)
    assert out.get("ok") is not True
    assert "orchestrator-only" in out.get("error", "")
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, target).status == "triage"
    finally:
        conn.close()


def test_investigation_card_and_plural_defect_are_not_a_grant(board, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    conn = kbc.connect()
    target = _triage(conn, kb)
    investigation = _fix(
        conn, kb, target, key="selfheal:kanban-50a03854e8",
        title=f"self-heal: kanban card {target} on hub/default failed 3x",
    )
    _as_worker(monkeypatch, conn, kb, investigation)
    assert kt._check_kanban_unblock() is False
    assert _unblock(target).get("ok") is not True

    plural = _fix(
        conn, kb, target,
        key="selfheal:kanban-aabbccddee:fix",
        title=f"self-heal fix: kanban cards {target}+2 on hub/default failed",
    )
    _as_worker(monkeypatch, conn, kb, plural)
    assert kt._check_kanban_unblock() is False
    assert _unblock(target).get("ok") is not True
    assert kb.get_task(conn, target).status == "triage"
    conn.close()


def test_child_of_done_fix_card_can_requeue_the_same_card(board, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    conn = kbc.connect()
    target = _triage(conn, kb)
    fix = _fix(conn, kb, target, status="done")
    child = kb.create_task(
        conn, title=f"requeue {target}", assignee="default", created_by="default",
        creator_task_id=fix,
    )
    _as_worker(monkeypatch, conn, kb, child)
    conn.close()

    assert kt._check_kanban_unblock() is True
    out = _unblock(target)
    assert out.get("ok") is True, out
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, target).status == "ready"
        payload = next(
            e.payload for e in kb.list_events(conn, target) if e.kind == "self_heal_requeue"
        )
        assert payload == {"by": child, "fix_card": fix, "target": target}
    finally:
        conn.close()


def test_child_of_unfinished_fix_card_cannot_requeue(board, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect()
    target = _triage(conn, kb)
    fix = _fix(conn, kb, target, status="running")
    child = kb.create_task(
        conn, title="too early", assignee="default", creator_task_id=fix,
    )
    _as_worker(monkeypatch, conn, kb, child)
    conn.close()

    assert _unblock(target).get("ok") is not True
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, target).status == "triage"
    finally:
        conn.close()


def test_later_generation_fix_key_is_a_grant(board, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect()
    target = _triage(conn, kb)
    fix = _fix(conn, kb, target, key="selfheal:kanban-50a03854e8:r2:fix")
    _as_worker(monkeypatch, conn, kb, fix)
    conn.close()

    assert _unblock(target).get("ok") is True


def test_self_heal_worker_cli_unblock_stays_fenced(board, monkeypatch):
    """The shell fence is unchanged: only the in-process tool has the grant."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect()
    target = _triage(conn, kb)
    fix = _fix(conn, kb, target)
    _as_worker(monkeypatch, conn, kb, fix)
    conn.close()

    proc = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", "unblock", target],
        cwd=ROOT, env=dict(__import__("os").environ), stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=45, check=False,
    )
    assert proc.returncode != 0, (proc.stdout, proc.stderr)
    assert "orchestrator-only" in proc.stderr
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, target).status == "triage"
    finally:
        conn.close()
