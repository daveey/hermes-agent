"""The kanban stop-loop nudge must make the worker's text answer durable before the next model call.

An observed managed worker (e.g. Ask Eve's private observer) requires every SDK response to be bound to exactly
one durable assistant row before the next SDK request. The nudge used to append the text-only answer as ephemeral
scaffolding (``_kanban_stop_synthetic``) without a flush, so the second request went out with the first response
unbound and the observer faulted closed ("previous response never durably bound"). The sibling verify-on-stop and
pre_verify gates already flush the candidate through ``_append_interim_answer``; only their nudge is synthetic.
"""

from __future__ import annotations

from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _response(content):
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="test/model",
        usage=None,
    )


@pytest.fixture
def worker(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    from run_agent import AIAgent

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_flushprobe")
    monkeypatch.delenv("HERMES_KANBAN_STOP_NUDGE", raising=False)
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")
    with closing(SessionDB(db_path=tmp_path / "state.db")) as db:
        with (
            patch("model_tools.get_tool_definitions", return_value=[]),
            patch("model_tools.check_toolset_requirements", return_value={}),
            patch("agent.process_bootstrap.OpenAI"),
        ):
            agent = AIAgent(
                session_id="kanban-stop-flush",
                session_db=db,
                api_key="test-key",
                base_url="https://example.invalid/v1",
                provider="openai-compat",
                model="test/model",
                max_iterations=2,
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
        agent._cached_system_prompt = "stable test prompt"
        agent.save_trajectories = False
        agent.compression_enabled = False
        agent._cleanup_task_resources = lambda *_a, **_kw: None
        agent._save_trajectory = lambda *_a, **_kw: None
        yield agent, db


def _assistant_rows(db, sid):
    return [
        row[0]
        for row in db._conn.execute(
            "SELECT content FROM messages WHERE session_id = ? AND role = 'assistant' ORDER BY id", (sid,)
        ).fetchall()
    ]


def test_text_only_answer_is_durable_before_the_nudged_second_request(worker):
    agent, db = worker
    seen_at_request = []
    answers = iter(["MANAGED_DONE", "still done"])

    def model_call(_api_kwargs):
        seen_at_request.append(_assistant_rows(db, agent.session_id))
        return _response(next(answers))

    agent._interruptible_api_call = model_call
    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        result = agent.run_conversation("work kanban task t_flushprobe")

    assert len(seen_at_request) == 2, "the kanban nudge must have continued the turn into a second request"
    assert getattr(agent, "_kanban_stop_nudges", 0) >= 1
    assert seen_at_request[0] == []
    # The first SDK response is bound to exactly one durable assistant row before the second request.
    assert seen_at_request[1] == ["MANAGED_DONE"]
    # The nudge itself stays ephemeral scaffolding: never persisted, never replayed.
    assert not [m for m in result["messages"] if m.get("_kanban_stop_synthetic")]
    persisted_user = [
        row[0]
        for row in db._conn.execute(
            "SELECT content FROM messages WHERE session_id = ? AND role = 'user'", (agent.session_id,)
        ).fetchall()
    ]
    assert all("kanban_complete" not in (content or "") for content in persisted_user)
    # The answer row keeps its finish_reason marker and is not itself flagged synthetic.
    answer = next(m for m in result["messages"] if m.get("content") == "MANAGED_DONE")
    assert answer.get("finish_reason") == "kanban_terminal_required"
    assert not answer.get("_kanban_stop_synthetic")


def test_budget_exhaustion_after_nudge_keeps_one_durable_answer(worker):
    """The flushed answer is also the budget fallback: it must not be duplicated in memory or on disk."""
    agent, db = worker
    agent.max_iterations = 1
    agent.iteration_budget.max_total = 1
    agent._interruptible_api_call = lambda _kwargs: _response("MANAGED_DONE")
    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        result = agent.run_conversation("work kanban task t_flushprobe")

    assert result["final_response"] == "MANAGED_DONE"
    assert [m["role"] for m in result["messages"]] == ["user", "assistant"]
    assert _assistant_rows(db, agent.session_id) == ["MANAGED_DONE"]
