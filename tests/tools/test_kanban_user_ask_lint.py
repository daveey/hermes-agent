"""A block on David must say, in its ask line, what unblock and archive mean (coach pattern
blocked-card-missing-user-ask: "the task shows blocked but doesnt tell me what i am supposed to do")."""
import json

import pytest

VAGUE = {"action": "Pick the second-builder design for bb-pod-iroh: A, B or C (recommended)",
         "why_human": "security architecture choice", "interaction": "reply",
         "scope": "release sign jobs", "evidence": "PR #1809 builds differ on the sign host"}
CLEAR = {**VAGUE, "action": ("Reply A, B or C in this chat for the bb-pod-iroh second builder. Unblock = build "
                             "your pick; archive = skip the second builder and record the accepted risk.")}


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return tid


def _task(tid):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        return kb.get_task(conn, tid)
    finally:
        conn.close()


def test_block_with_vague_ask_is_refused(worker_env):
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_block({"reason": "Waiting on the design pick", "kind": "needs_input",
                                     "human_request": VAGUE}))
    assert "error" in d and d.get("rejection") == "user_ask_incomplete"
    assert "unblocking" in d["error"] and "archiving" in d["error"]
    assert _task(worker_env).status == "running"


def test_block_with_unblock_and_archive_meaning_lands(worker_env):
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_block({"reason": "Waiting on the design pick", "kind": "needs_input",
                                     "human_request": CLEAR}))
    assert d.get("ok") is True, d
    t = _task(worker_env)
    assert t.status == "blocked" and CLEAR["action"] in json.dumps(t.human_request)


def test_block_without_human_request_is_not_this_lint(worker_env):
    """Non-human waits keep their own policy; this lint only reads a supplied ask."""
    from hermes_cli.kanban_block_policy import user_ask_lint_error
    assert user_ask_lint_error(None) is None
    assert user_ask_lint_error(CLEAR) is None
    assert "archiving" in user_ask_lint_error({**CLEAR, "action": "Unblock to proceed"})


def test_create_blocked_with_vague_ask_is_refused(worker_env, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_create({"title": "Decide: second builder", "initial_status": "blocked",
                                      "human_request": VAGUE, "requires": [], "placement": "local"}))
    assert "error" in d and "user_ask_incomplete" in d["error"], d
