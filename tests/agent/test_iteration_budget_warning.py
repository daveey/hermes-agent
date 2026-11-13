"""Iteration checkpoints preserve the transcript and the hard budget."""
from copy import deepcopy

import pytest


def _agent(tmp_path, monkeypatch, ratio):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        f"agent:\n  budget_warning_ratio: {ratio}\n", encoding="utf-8"
    )
    from run_agent import AIAgent
    from hermes_state import SessionDB
    from model_tools import _clear_tool_defs_cache
    from tools.registry import invalidate_check_fn_cache

    # Cases model separate worker processes; their availability caches must not
    # survive a change from ordinary to dispatcher-owned construction.
    invalidate_check_fn_cache()
    _clear_tool_defs_cache()
    return AIAgent(session_db=SessionDB(db_path=tmp_path / "proof.db"),
                   model="test-model", provider="openai-compat", api_key="test",
                   base_url="http://127.0.0.1:1/v1", max_iterations=4,
                   quiet_mode=True, skip_context_files=True, skip_memory=True)


@pytest.mark.parametrize("content", ["result", [{"type": "text", "text": "result"}]])
@pytest.mark.parametrize("interrupted", [False, True])
def test_checkpoint_rearms_per_turn_without_changing_budget_or_durable_rows(
    tmp_path, monkeypatch, content, interrupted
):
    from agent.turn_context import _reset_per_turn_agent_state
    from agent.turn_iteration_prep import prepare_iteration
    from agent.tool_executor import _flush_session_db_after_tool_progress

    agent = _agent(tmp_path, monkeypatch, "0.75")
    try:
        for turn in range(2):
            _reset_per_turn_agent_state(agent)
            agent._interrupt_requested = interrupted
            for _ in range(3):
                agent.iteration_budget.consume()
            messages = [{"role": "user", "content": f"work {turn}"},
                        {"role": "assistant", "tool_calls": [{"id": f"t{turn}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
                        {"role": "tool", "tool_call_id": f"t{turn}", "content": deepcopy(content)}]
            assert _flush_session_db_after_tool_progress(agent, messages, stage="checkpoint")
            persisted = agent._session_db.get_messages(agent.session_id)
            notices = sum("iteration budget checkpoint" in str(row["content"]) for row in persisted)
            assert notices == (0 if interrupted else turn + 1)
            assert ("3 of 4" in str(messages[-1]["content"])) is not interrupted
            snapshot = deepcopy(messages)
            # A durable result is never rewritten, even after interruption clears
            # and a new turn has rearmed the notice latch.
            agent._interrupt_requested = False
            prepare_iteration(agent, messages=messages, api_call_count=3)
            assert messages == snapshot
            assert agent.iteration_budget.consume()
            assert not agent.iteration_budget.consume()
    finally:
        agent._session_db.close()


def _fake_agent(*, oneshot, max_total=60, ratio=None):
    from types import SimpleNamespace
    from agent.iteration_budget import IterationBudget

    return SimpleNamespace(
        budget_warning_ratio=ratio, iteration_budget=IterationBudget(max_total),
        _oneshot_session=oneshot, valid_tool_names={"terminal", "write_file"},
        _interrupt_requested=False,
    )


def _run_to_cap(agent):
    """Simulate one tool result per iteration; return the iteration of the first notice."""
    from agent.turn_iteration_prep import _maybe_inject_iteration_budget_warning

    hits = []
    for i in range(1, agent.iteration_budget.max_total + 1):
        agent.iteration_budget.consume()
        msgs = [{"role": "tool", "tool_call_id": f"t{i}", "content": "ok"}]
        if _maybe_inject_iteration_budget_warning(agent, msgs):
            hits.append((i, msgs[-1]["content"]))
    return hits


def test_oneshot_without_ratio_gets_one_notice_by_80_percent(monkeypatch):
    """Regression (defect 4a4dc075ad): a ``chat -q --max-turns 60`` reviewer used all 60
    iterations with no warning, then lost its VERDICT.md write when tools were removed."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    hits = _run_to_cap(_fake_agent(oneshot=True))
    assert len(hits) == 1, hits
    at, text = hits[0]
    assert at <= 48
    assert f"{60 - at} iterations remain" in text
    assert "tools are removed" in text


def test_interactive_without_ratio_still_gets_no_notice(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert _run_to_cap(_fake_agent(oneshot=False)) == []


def test_configured_ratio_wins_for_oneshot(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    hits = _run_to_cap(_fake_agent(oneshot=True, ratio=0.5))
    assert [h[0] for h in hits] == [30]


def test_cap_exhaustion_logs_one_countable_line(caplog):
    import logging
    from agent.turn_finalizer import _log_iteration_cap_exhausted

    agent = _fake_agent(oneshot=True, max_total=6)
    agent.platform, agent.max_iterations = "cli", 6
    agent._iteration_budget_warning_injected = True
    log = logging.getLogger("agent.conversation_loop")
    with caplog.at_level(logging.WARNING, logger=log.name):
        _log_iteration_cap_exhausted(agent, 6, log)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("iteration_cap_exhausted")]
    assert lines == ["iteration_cap_exhausted source=cli oneshot=True warned=True used=6/6"]
