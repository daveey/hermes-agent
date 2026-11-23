"""E4/T3: opt-in review-metadata guard on the real registered ``kanban_request_review`` tool.

Scope is exactly the local request-review tool boundary, driven through ``registry.dispatch``. Transport
callers are NOT exercised here. The MCP stdio server (agent/transports/hermes_tools_mcp_server.py) and the
agent tool loop dispatch via ``model_tools.handle_function_call`` to this registry entry. acp_adapter/tools.py
only renders tool events and hermes_cli/plugins_dispatch.py only runs plugin hooks: neither is a
request-review dispatcher. ACP sessions and plugin-hook tool loops reach the handler only through the agent
tool loop. Their transport proof, and any successful MCP handoff, remain unproven here.
The tool keeps base truthiness for metadata pin_mandatory/require_model (N1); REST's strict rule is separate.
No courier/selfwork result shape is introduced: a refusal is the ordinary ``tool_error`` body.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from tests.tools import test_kanban_tools as worker_fixtures

worker_env = worker_fixtures.worker_env

_TABLES = ("tasks", "task_events", "task_comments", "task_links", "task_attachments", "task_runs")

REFUSED = [
    {"review_model": "class:bogus"},
    {"review_model": "class:"},
    {"review_model": "class: deep"},
    {"review_model": "Class:deep"},
    {"review_model": "gpt-5"},
    {"review_model": "class:fast", "review_provider": "anthropic"},
    {"review_provider": "auto"},
    {"require_model": True},
    {"pin_mandatory": "false"},   # truthy under the tool's base truthiness rule (N1)
    {"pin_mandatory": 1},
    {"review_model": "class:deep", "require_model": "yes"},
]
REFUSED_IDS = ["bogus", "empty-suffix", "padded", "capital", "concrete", "concrete-provider", "provider-only",
               "require-true", "pin-str-false", "pin-1", "class-plus-require"]
# Falsy under base truthiness: accepted by the tool (REST alone applies the strict None/False/int0 rule).
TRUTHINESS_ACCEPTED = [{"pin_mandatory": ""}, {"pin_mandatory": 0}, {"pin_mandatory": 0.0},
                       {"require_model": []}, {"require_model": False}, {"pin_mandatory": None}]
VALID = [None, {}, {"review_model": "class:fast"}, {"review_model": "class:standard", "review_provider": "auto"},
         {"review_model": "class:deep", "review_provider": "auto"}]


def _snapshot() -> dict:
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        conn.row_factory = sqlite3.Row
        return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()]
                for t in _TABLES}


def _status(tid: str) -> str:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        return kb.get_task(conn, tid).status


def _dispatch(args: dict):
    """The real registered handler via ``registry.dispatch``; no transport (agent loop, MCP, ACP) is exercised."""
    import tools.kanban_tools  # registers the kanban toolset
    from tools.registry import registry
    out = registry.dispatch("kanban_request_review", args)
    return json.loads(out) if isinstance(out, str) else out


@pytest.fixture
def effects(monkeypatch):
    """Recording spies (delegating, never raising) on every pre-DB effect the guard must precede."""
    from tools import kanban_tools as kt
    from tools import kanban_tools_completion as ktc
    from tools import kanban_tools_source as kts
    seen: list[str] = []

    def spy(owner, name):
        real = getattr(owner, name)

        def wrapper(*a, **kw):
            seen.append(name)
            return real(*a, **kw)
        monkeypatch.setattr(owner, name, wrapper)

    for owner, name in ((kt, "_worker_guard"), (kt, "_redact_metadata"), (kt, "_goal_gate"),
                        (kt, "judge_goal"), (ktc, "park_unachievable"),
                        (kts, "register_for_ending"), (kts, "stage_completion")):
        if hasattr(owner, name):
            spy(owner, name)
    return seen


def _error_keys() -> set:
    from tools.registry import tool_error
    return set(json.loads(tool_error("x")))


@pytest.mark.parametrize("metadata", REFUSED, ids=REFUSED_IDS)
def test_refused_before_any_effect(worker_env, effects, metadata):
    before = _snapshot()
    result = _dispatch({"summary": "Implemented and verified", "metadata": metadata,
                        "artifacts": ["/fixture/report.txt"]})
    assert "model-neutral" in result.get("error", ""), result
    assert set(result) == _error_keys(), result  # ordinary tool_error, no courier/typed wire shape
    assert effects == [], effects  # before worker guard, redaction, goal gate/judge/park, source, artifacts
    assert _status(worker_env) == "running"
    assert _snapshot() == before


def test_refusal_precedes_missing_summary(worker_env, effects):  # N2 ordering
    before = _snapshot()
    result = _dispatch({"metadata": {"review_model": "class:bogus"}})
    assert "model-neutral" in result.get("error", ""), result
    assert effects == []
    assert _snapshot() == before


def test_missing_summary_without_bad_metadata_keeps_base_refusal(worker_env):  # baseline control
    result = _dispatch({"metadata": {"review_model": "class:fast"}})
    assert "summary is required" in result.get("error", ""), result
    assert _status(worker_env) == "running"


@pytest.mark.parametrize("metadata", ["class:bogus", ["review_model"], 7], ids=["str", "list", "int"])
def test_non_dict_metadata_keeps_base_refusal(worker_env, metadata):  # baseline control
    before = _snapshot()
    result = _dispatch({"summary": "s", "metadata": metadata})
    assert result.get("error", "").startswith("metadata must be an object/dict"), result
    assert _snapshot() == before


def _review_event(tid: str):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        events = [e for e in kb.list_events(conn, tid) if e.kind == "review_requested"]
        return events, kb.get_task(conn, tid)


@pytest.mark.parametrize("metadata", VALID, ids=["omitted", "empty", "fast", "standard-auto", "deep-auto"])
def test_valid_classes_reach_ordinary_handoff(worker_env, effects, metadata, monkeypatch):
    import os
    run_id = int(os.environ["HERMES_KANBAN_RUN_ID"])
    args = {"summary": "Implemented and verified"}
    if metadata is not None:
        args["metadata"] = metadata
    result = _dispatch(args)
    assert result.get("ok") is True, result
    assert _status(worker_env) == "review"
    assert {"_worker_guard", "_goal_gate", "register_for_ending", "stage_completion"} <= set(effects)
    events, task = _review_event(worker_env)
    assert len(events) == 1 and events[0].run_id == run_id  # independent run attribution retained
    assert task.assignee == "test-worker"


@pytest.mark.parametrize("metadata", TRUTHINESS_ACCEPTED, ids=repr)
def test_tool_keeps_base_truthiness_for_mandatory_flags(worker_env, metadata):  # N1 pinned
    result = _dispatch({"summary": "Implemented and verified", "metadata": metadata})
    assert result.get("ok") is True, result
    assert _status(worker_env) == "review"


def test_invalid_replay_on_review_card_refused_before_effects(worker_env, effects):
    assert _dispatch({"summary": "first handoff", "metadata": {"review_model": "class:fast"}}).get("ok") is True
    effects.clear()
    before = _snapshot()
    result = _dispatch({"summary": "replay", "metadata": {"review_model": "class:bogus"}})
    assert "model-neutral" in result.get("error", ""), result
    assert effects == []
    assert _snapshot() == before and _status(worker_env) == "review"


def test_invalid_metadata_on_done_card_refused_before_effects(worker_env, effects):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        assert kb.complete_task(conn, worker_env, summary="done", force=True)
    effects.clear()
    before = _snapshot()
    result = _dispatch({"summary": "late", "metadata": {"review_model": "gpt-5"}})
    assert "model-neutral" in result.get("error", ""), result
    assert effects == []
    assert _snapshot() == before and _status(worker_env) == "done"


def test_valid_replay_on_review_card_keeps_base_refusal(worker_env):  # baseline control
    assert _dispatch({"summary": "first handoff"}).get("ok") is True
    result = _dispatch({"summary": "replay", "metadata": {"review_model": "class:deep"}})
    assert "error" in result and "model-neutral" not in result["error"], result
    assert _status(worker_env) == "review"
    events, _ = _review_event(worker_env)
    assert len(events) == 1


def test_registry_entry_is_the_guarded_handler():
    """All registry callers resolve this one entry; the guard is inside the outer _kanban_handler."""
    from tools import kanban_tools as kt
    from tools.registry import registry
    entry = registry.get_entry("kanban_request_review")
    assert entry is not None and entry.handler is kt._handle_request_review
    inner = kt._handle_request_review.__wrapped__  # functools.wraps chain: _kanban_handler -> guard
    assert inner.__code__.co_qualname.startswith("model_neutral_review"), inner.__code__.co_qualname
