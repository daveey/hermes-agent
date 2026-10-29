"""needs_input blocks need a human_request: refused, swept, and classified as blocked on the sidebar."""
import json
import time

import pytest

CUTOVER_REASON = (
    "1) Put GATEWAY-MIGRATION.md (Mac: ~/code/hermes/hosts/hermes-ci-capacity/) on the hub, e.g. attach "
    "it to this card or copy to ~/asg-ci-controller/, or tell me how to read a ci-gw docker box "
    "(docker3-5) config. 2) Then unblock this card"
)
ASK = {"action": "Copy GATEWAY-MIGRATION.md to the hub. Unblock = proceed; archive = drop it.", "why_human": "only on the MacBook",
       "interaction": "reply", "scope": "ASG CI cutover", "evidence": "file absent on hub"}


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


def _status(tid):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        t = kb.get_task(conn, tid)
        return t.status, t.block_kind
    finally:
        conn.close()


def test_cutover_reason_without_human_request_is_refused(worker_env):
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_block({"reason": CUTOVER_REASON, "kind": "needs_input"}))
    blob = json.dumps(d)
    assert "error" in d and "agent_owned_block" in blob
    assert "human_request" in blob and "needs=" in blob
    assert _status(worker_env) == ("running", None)


def test_needs_input_with_human_request_blocks_and_sidebar_shows_blocked(worker_env):
    from hermes_cli.kanban_session_activity import _replica_bucket
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_block({"reason": CUTOVER_REASON, "kind": "needs_input", "human_request": ASK}))
    assert d.get("ok") is True
    assert _status(worker_env) == ("blocked", "needs_input")
    assert _replica_bucket({"status": "blocked", "block_kind": "needs_input", "human_request": ASK}) == "blocked"
    assert _replica_bucket({"status": "blocked", "block_kind": "needs_input", "human_request": None}) == "active"


def test_policy_function_refuses_needs_input_unless_owned():
    from hermes_cli.kanban_block_policy import block_policy_error
    assert "agent_owned_block" in block_policy_error("needs_input", reason=CUTOVER_REASON)
    assert block_policy_error("needs_input", human_request=ASK) is None
    assert block_policy_error("needs_input", wake_check="test -f /x") is None
    assert block_policy_error("needs_input", clock_only=True) is None
    assert block_policy_error("needs_input", blocker_id="gh-billing") is None


def test_sweep_escalates_askless_needs_input_and_spares_asked(worker_env):
    import json as _json
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.kanban_block_policy import sweep_agent_owned_blocks
    conn = kbc.connect()
    try:
        asked = kb.create_task(conn, title="asked", assignee="test-worker")
        askless = worker_env
        conn.execute("UPDATE tasks SET status='blocked', block_kind='needs_input', human_request=NULL, "
                     "current_run_id=NULL WHERE id=?", (askless,))
        conn.execute("UPDATE tasks SET status='blocked', block_kind='needs_input', human_request=?, "
                     "current_run_id=NULL WHERE id=?", (_json.dumps(ASK), asked))
        conn.commit()
        assert sweep_agent_owned_blocks(conn) == []  # inside the grace period
        later = int(time.time()) + 3 * 3600
        assert sweep_agent_owned_blocks(conn, now=later) == [askless]
        assert kb.get_task(conn, askless).status == "triage"
        assert kb.get_task(conn, asked).status == "blocked"
    finally:
        conn.close()
