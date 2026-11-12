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


@pytest.mark.parametrize("premise, probe", [
    ("Merge https://github.com/example/project/pull/42", "gh pr view 42 -R example/project"),
    ("Remove /var/cache/example-build", "test -e /var/cache/example-build"),
    ("Install build 649; version read: `device-info --version`", "device-info --version"),
])
def test_machine_premise_warns_without_refusing(worker_env, premise, probe):
    from tools import kanban_tools as kt
    ask = {**CLEAR, "action": premise + ". Unblock = proceed; archive = drop it."}
    result = json.loads(kt._handle_block({"reason": "Human step", "kind": "needs_input",
                                         "human_request": ask}))
    assert result.get("ok") is True, result
    assert _task(worker_env).status == "blocked"
    assert probe in result["resolved_check_warning"]
    assert "resolved_check" in result["resolved_check_warning"]


@pytest.mark.parametrize("extra", [
    {"action": "Press the door-release button. Unblock = proceed; archive = drop it.", "evidence": "Door is closed"},
    {"evidence": "https://github.com/example/project/pull/42", "resolved_check": "false"},
])
def test_no_premise_warning_for_physical_or_probed_asks(worker_env, extra):
    from tools import kanban_tools as kt
    result = json.loads(kt._handle_block({"reason": "Human step", "kind": "needs_input",
                                         "human_request": {**CLEAR, **extra}}))
    assert result.get("ok") is True, result
    assert "resolved_check_warning" not in result


@pytest.mark.parametrize("evidence, expected", [
    ("See https://github.com/example/project/pull/42", "MERGED|CLOSED"),
    ('Remove `/var/cache/build files`', "test -e '/var/cache/build files'"),
    ('Remove "/var/cache/build files"', "test -e '/var/cache/build files'"),
    ("Remove '/var/cache/build files'", "test -e '/var/cache/build files'"),
    ("Install version v649", "existing version read"),
    ("Device needs ios-app1655", "existing version read"),
])
def test_evidence_premises_preserve_probe_targets(worker_env, evidence, expected):
    from tools import kanban_tools as kt
    result = json.loads(kt._handle_block({"reason": "Human step", "kind": "needs_input",
                                         "human_request": {**CLEAR, "evidence": evidence}}))
    assert result.get("ok") is True, result
    assert expected in result["resolved_check_warning"]


@pytest.mark.parametrize("ask", [None, {}, {"action": "See https://example.com/docs"},
    {"action": "https://github.com/example/project/pull/42oops"}])
def test_no_machine_premise_has_no_advice(ask):
    from hermes_cli.kanban_ask_premise import warning_fields
    assert warning_fields(ask) == {}


def test_create_blocked_with_vague_ask_is_refused(worker_env, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_create({"title": "Decide: second builder", "initial_status": "blocked",
                                      "human_request": VAGUE, "requires": [], "placement": "local"}))
    assert "error" in d and "user_ask_incomplete" in d["error"], d
