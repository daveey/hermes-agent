"""agent_owned_block refusals must teach the fix, and the guidance that agents read must not invite them.

Coach store (week of 2026-10-27): 62 refusals in 60 sessions. Causes, by evidence:
- the worker prompt told agents to call ``kanban_block(reason=...)`` with no kind or human_request;
- the kanban_block ``reason`` schema said "for needs_input/capability write numbered steps" (the ask went
  into reason, not human_request), and ``kind`` said capability etc. "surface to a human";
- kanban_create's ``initial_status`` text never said 'blocked' needs human_request, so asks went into body;
- kanban_continue's exhaustion error prescribed a kanban_block call the guard itself refused.
The guard is unchanged: every ask-less, wake-less block or blocked card is still refused.
"""
import json

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_continuation as kcont
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

EXHAUSTED_REASON = "continuation_exhausted: split the work or reset the continuation budget"


@pytest.fixture
def home(monkeypatch, tmp_path):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    for var in ("HERMES_SESSION_ID", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(var, raising=False)
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return h


@pytest.fixture
def worker_env(home, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return tid


def _status(tid):
    with kbc.connect_closing() as conn:
        t = kb.get_task(conn, tid)
        return t.status, t.block_kind


def _assert_teaches_fix(text):
    """The refusal carries a copyable human_request with every field, and the agent-wait routes."""
    assert "agent_owned_block" in text
    assert "human_request={" in text
    for field in ("action", "why_human", "interaction", "scope", "evidence"):
        assert f"'{field}'" in text, field
    assert "Unblock = " in text and "archive = " in text


# --- kanban_block: tool + CLI ------------------------------------------------------------------

@pytest.mark.parametrize("kind", [None, "capability", "transient", "needs_input"])
def test_tool_block_refusal_has_human_request_skeleton(worker_env, kind):
    from tools import kanban_tools as kt
    args = {"reason": "1) Grant ec2:RunInstances to the hub role. 2) Unblock this card."}
    if kind:
        args["kind"] = kind
    d = json.loads(kt._handle_block(args))
    _assert_teaches_fix(d["error"])
    assert "kind='assignment'" in d["error"] or "needs=" in d["error"]
    assert _status(worker_env) == ("running", None)  # guard unchanged: still refused, task untouched


@pytest.mark.parametrize("extra", ["", "--kind capability", "--kind needs_input"])
def test_cli_block_refusal_has_human_request_skeleton(home, extra):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="stuck", assignee="alice")
    out = kanban_cli.run_slash(f"block {tid} need a grant {extra}".strip())
    _assert_teaches_fix(out)
    assert _status(tid)[0] == "ready"


# --- kanban_create initial_status='blocked': tool + CLI -----------------------------------------

def test_tool_create_blocked_refusal_has_skeleton_and_agent_wait_routes(home):
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_create({
        "title": "Decide: switch CI hosts to on-demand?", "assignee": "default", "requires": [],
        "initial_status": "blocked", "body": "Ask (David): approve. Unblock = do it; archive = keep spot."}))
    err = d.get("error", "")
    _assert_teaches_fix(err)
    assert "parents=" in err and "kanban_schedule" in err
    with kbc.connect_closing() as conn:
        assert not [t for t in kb.list_tasks(conn) if t.title.startswith("Decide: switch")]


def test_cli_create_blocked_refusal_has_skeleton_and_agent_wait_routes(home):
    out = kanban_cli.run_slash("create 'Decide: switch CI hosts?' --initial-status blocked")
    _assert_teaches_fix(out)
    assert "parents=" in out and "kanban_schedule" in out
    assert "Traceback" not in out
    with kbc.connect_closing() as conn:
        assert not [t for t in kb.list_tasks(conn) if t.title.startswith("Decide: switch")]


def test_create_blocked_with_human_request_still_allowed(home):
    with kbc.connect_closing() as conn:
        assert kb.create_task(conn, title="Choose", initial_status="blocked", human_request={
            "action": "Pick A or B. Unblock = build it; archive = drop it.", "why_human": "taste",
            "interaction": "reply", "scope": "x", "evidence": "y"})


# --- schema + worker prompt guidance --------------------------------------------------------------

def test_schemas_say_blocked_and_human_kinds_need_human_request():
    from tools.kanban_tools_schemas import KANBAN_BLOCK_SCHEMA, KANBAN_CREATE_SCHEMA
    create_props = KANBAN_CREATE_SCHEMA["parameters"]["properties"]
    assert "requires human_request" in create_props["initial_status"]["description"]
    block_props = KANBAN_BLOCK_SCHEMA["parameters"]["properties"]
    kind_desc = block_props["kind"]["description"]
    assert "human_request" in kind_desc and "refused" in kind_desc
    assert "The others surface to a human" not in kind_desc
    reason_desc = block_props["reason"]["description"]
    assert "human_request" in reason_desc
    assert "For needs_input/capability write numbered steps" not in reason_desc
    assert "human_request" in KANBAN_BLOCK_SCHEMA["description"].split("'capability'")[1][:200]


def test_worker_prompt_never_prescribes_bare_kanban_block():
    from agent import prompt_builder
    from agent import kanban_stop, kanban_turn_recovery
    import inspect
    texts = [prompt_builder.KANBAN_GUIDANCE, inspect.getsource(kanban_stop),
             inspect.getsource(kanban_turn_recovery)]
    for text in texts:
        assert "kanban_block(reason=...)" not in text
        assert 'kanban_block(reason=\\"...\\")' not in text and 'kanban_block(reason="...")' not in text
    assert "human_request=" in prompt_builder.KANBAN_GUIDANCE


# --- continuation exhaustion: the prescribed block lands (tool + CLI) ------------------------------

def _exhaust(tid):
    with kbc.connect_closing() as conn:
        assert kcont.continue_task(conn, tid, checkpoint="one", max_continuations=1)["ok"]
        assert kb.claim_task(conn, tid)
        refused = kcont.continue_task(conn, tid, checkpoint="two", max_continuations=1)
        run_id = kb._current_run_id(conn, tid)
    assert refused["reason_code"] == "continuation_exhausted"
    assert EXHAUSTED_REASON in refused["error"]
    return run_id


def test_tool_block_with_prescribed_exhaustion_reason_lands_in_agent_triage(worker_env, monkeypatch):
    from tools import kanban_tools as kt
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(_exhaust(worker_env)))
    d = json.loads(kt._handle_block({"kind": "transient", "reason": EXHAUSTED_REASON}))
    assert "error" not in d, d
    assert _status(worker_env) == ("triage", "transient")


def test_cli_block_with_prescribed_exhaustion_reason_lands_in_agent_triage(home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="long", assignee="alice")
        kb.claim_task(conn, tid)
    _exhaust(tid)
    out = kanban_cli.run_slash(f"block {tid} '{EXHAUSTED_REASON}' --kind transient")
    assert "agent_owned_block" not in out, out
    assert _status(tid) == ("triage", "transient")


def test_exhaustion_prefix_without_a_real_refusal_is_still_refused(worker_env):
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_block({"kind": "transient", "reason": EXHAUSTED_REASON}))
    assert "agent_owned_block" in d.get("error", "")
    assert _status(worker_env) == ("running", None)


def test_exhaustion_with_extra_prose_is_still_refused(worker_env, monkeypatch):
    from tools import kanban_tools as kt
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(_exhaust(worker_env)))
    d = json.loads(kt._handle_block({"kind": "transient",
                                     "reason": EXHAUSTED_REASON + ". Also approve the prod deploy."}))
    assert "agent_owned_block" in d.get("error", "")
    assert _status(worker_env) == ("running", None)
