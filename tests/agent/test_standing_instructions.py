"""Standing instructions survive compaction word for word, reach Kanban workers, and hold a don't-wait block.

Regression for self-heal defect agent-aa8d66631c: a relayed "dont block, dont wait" was paraphrased by
compaction into "urgency does not waive safeguards" and the agent later blocked on the user.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.context_compressor import ContextCompressor

WORDS = "get this session work done. dont block, dont wait. dont accept tasks that are waiting for something"
RELAYED = ('[Message from another Hermes session "Balance subscription routing" (@session:default@macbook/x)]\n\n'
           f"David DIRECT NOW: '{WORDS}'. I directly inspected hub: B t_ae11c0bc todo gated PR44.")


def _compact(turns, summary="## Goal\nRouting work; urgency does not waive safeguards."):
    fake = SimpleNamespace(tail_mode="lean", _session_id="")
    return ContextCompressor._augment_summary_lean(fake, summary, turns)


def test_relayed_standing_words_survive_two_compactions_verbatim():
    # Enough later user chatter to push the relay out of the newest-first verbatim-user budget.
    later = [{"role": "user", "content": f"status check {i}: " + "x" * 900} for i in range(40)]
    first = _compact([{"role": "user", "content": RELAYED}, *later])
    assert f"standing: {WORDS}" in first
    # The next compaction sees only the previous summary row (synthetic) plus new turns.
    second = _compact([{"role": "assistant", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] " + first},
                       {"role": "user", "content": "what are we blocked on"}])
    assert f"standing: {WORDS}" in second
    assert "ACTIVE" in second


def test_legacy_tail_mode_also_carries_standing_lines():
    fake = SimpleNamespace(tail_mode="legacy", _session_id="")
    out = ContextCompressor._augment_summary_lean(fake, "summary", [{"role": "user", "content": "please, don't wait on me"}])
    assert "standing: please, don't wait on me" in out


def test_assistant_prose_is_not_a_standing_instruction():
    out = _compact([{"role": "assistant", "content": "I will not block; don't wait for me."}])
    assert "standing:" not in out


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
        tid = kb.create_task(conn, title="migrate launchers", assignee="test-worker")
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
        kb.add_comment(conn, tid, "coordinator", f"standing: {WORDS}")
        for i in range(40):  # push the standing comment out of the newest-30 comment tail
            kb.add_comment(conn, tid, "worker", f"progress note {i}")
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return tid


def test_worker_context_always_shows_standing_comments(worker_env):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        text = kb.build_worker_context(conn, worker_env)
    finally:
        conn.close()
    assert "## Standing instructions" in text
    assert f"standing: {WORDS}" in text
    assert text.index("## Standing instructions") < text.index("## Comment thread")


ASK = {"action": "Pick when to restart the mini gateways", "why_human": "restart needs consent",
       "interaction": "reply", "scope": "mini launcher migration", "evidence": "card comments"}


def test_needs_input_block_under_standing_dont_wait_is_held_once_with_hint(worker_env):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    args = {"reason": "1) Choose now, when idle or later for the restart. 2) Then unblock this card",
            "kind": "needs_input", "human_request": ASK}
    first = json.loads(kt._handle_block(dict(args)))
    blob = json.dumps(first)
    assert "error" in first and "standing" in blob and "decide" in blob.lower()
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, worker_env).status == "running"
        kinds = [e.kind for e in kb.list_events(conn, worker_env)]
    finally:
        conn.close()
    assert "standing_wait_hold" in kinds  # logged for the coach/self-heal
    # A genuine human-only need (credential, physical step) still lands on the second call.
    second = json.loads(kt._handle_block(dict(args)))
    assert "error" not in second, second
