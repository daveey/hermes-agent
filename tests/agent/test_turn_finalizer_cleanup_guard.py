"""Regression test for #8049.

When the post-loop cleanup chain in ``finalize_turn`` raises — trajectory
save (file I/O), resource teardown (remote VM/browser), or session
persistence (SQLite) — the partial ``final_response`` the caller is waiting
for must still be returned.  Previously any of those raised straight out of
``run_conversation``, so a subprocess wrapper saw an empty stdout with no
traceback and lost the whole turn.
"""

import logging
from unittest.mock import patch

import pytest

from agent.turn_finalizer import finalize_turn


class _StubBudget:
    used = 5
    max_total = 3
    remaining = 0


class _StubCompressor:
    last_prompt_tokens = 0


from agent.status_output import StatusOutputMixin


class _StubAgent(StatusOutputMixin):
    """Minimal agent surface that ``finalize_turn`` reads from."""

    def __init__(self, *, raise_in):
        self._raise_in = set(raise_in)
        self.max_iterations = 3
        self.iteration_budget = _StubBudget()
        self.context_compressor = _StubCompressor()
        self.model = "stub/model"
        self.provider = "stub"
        self.base_url = "http://stub"
        self.session_id = "sess-1"
        self.quiet_mode = True
        self.platform = "cli"
        self._interrupt_requested = False
        self._interrupt_message = None
        self._tool_guardrail_halt_decision = None
        self._response_was_previewed = False
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        for attr in (
            "session_input_tokens",
            "session_output_tokens",
            "session_cache_read_tokens",
            "session_cache_write_tokens",
            "session_reasoning_tokens",
            "session_prompt_tokens",
            "session_completion_tokens",
            "session_total_tokens",
            "session_estimated_cost_usd",
        ):
            setattr(self, attr, 0)
        self.session_cost_status = "ok"
        self.session_cost_source = "stub"

    # --- fallible cleanup surfaces -------------------------------------
    def _save_trajectory(self, *a, **k):
        if "save_trajectory" in self._raise_in:
            raise RuntimeError("trajectory disk full")

    def _cleanup_task_resources(self, *a, **k):
        if "cleanup_task_resources" in self._raise_in:
            raise RuntimeError("docker teardown EOF")

    def _drop_trailing_empty_response_scaffolding(self, *a, **k):
        pass

    def _persist_session(self, *a, **k):
        if "persist_session" in self._raise_in:
            raise RuntimeError("sqlite database is locked")

    # --- harmless no-ops ------------------------------------------------
    def _emit_status(self, *a, **k):
        pass

    def _safe_print(self, *a, **k):
        pass

    def _handle_max_iterations(self, messages, n):
        return "PARTIAL SUMMARY FROM MODEL"

    def _file_mutation_verifier_enabled(self):
        return False

    def _turn_completion_explainer_enabled(self):
        return False

    def _drain_pending_steer(self):
        return None

    def clear_interrupt(self):
        pass

    def _sync_external_memory_for_turn(self, **k):
        pass


def _run(
    agent,
    *,
    final_response=None,
    api_call_count=3,
    turn_exit_reason="unknown",
):
    messages = [
        {"role": "user", "content": "do a thing"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "file contents"},
    ]
    return finalize_turn(
        agent,
        final_response=final_response,
        api_call_count=api_call_count,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=None,
        effective_task_id="task-1",
        turn_id="turn-1",
        user_message="do a thing",
        original_user_message="do a thing",
        _should_review_memory=False,
        _turn_exit_reason=turn_exit_reason,
    )




@pytest.mark.parametrize(
    "step", ["save_trajectory", "cleanup_task_resources", "persist_session"]
)
def test_single_cleanup_step_raises_does_not_skip_others(step):
    agent = _StubAgent(raise_in=(step,))
    result = _run(agent)
    # Response survives.
    assert result["final_response"] == "PARTIAL SUMMARY FROM MODEL"
    # Exactly the failing step is recorded; the others ran without error.
    assert result["cleanup_errors"] == [
        next(
            e
            for e in result["cleanup_errors"]
            if e.startswith(step)
        )
    ]
    assert len(result["cleanup_errors"]) == 1


def test_a_ticketed_steer_stays_out_of_the_leftover_follow_up():
    """A gateway transfer still buffered when the turn ends is not this turn's leftover steer: the
    finalizer hands back only ticketless steers, so settlement returns the transfer to its queue."""
    agent = _StubAgent(raise_in=())
    agent._drain_leftover_steer = lambda: "typed /steer"
    agent._drain_pending_steer = lambda: "typed /steer\nqueued prompt"
    assert _run(agent)["pending_steer"] == "typed /steer"


def test_clean_turn_has_no_cleanup_errors_key():
    agent = _StubAgent(raise_in=())
    result = _run(agent)
    assert result["final_response"] == "PARTIAL SUMMARY FROM MODEL"
    assert result["completed"] is False
    assert "cleanup_errors" not in result


@pytest.mark.parametrize(
    ("persist_disabled", "expected_calls"),
    [
        (True, ["transform_llm_output"]),
        (False, ["transform_llm_output", "post_llm_call", "on_session_end"]),
    ],
)
def test_persist_disabled_turn_skips_session_end_hook(
    persist_disabled, expected_calls
):
    agent = _StubAgent(raise_in=())
    agent._persist_disabled = persist_disabled
    calls = []

    def capture(name, _logger, **_kwargs):
        calls.append(name)
        return []

    with patch("agent.turn_finalizer._invoke_hook_safely", side_effect=capture):
        _run(
            agent,
            final_response="done",
            api_call_count=1,
            turn_exit_reason="text_response(stop)",
        )

    assert ("on_session_end" in calls) == ("on_session_end" in expected_calls)




# --- a retired worker generation on an interrupted turn is an expected fence -----------------
# A gateway restart/core update retires the Kanban worker's generation mid-turn; every later
# write is refused ``stale_generation``. On an interrupted turn that is the planned shutdown, not
# a persistence fault, so it must not log an ERROR traceback (the self-heal detector files those).


class _StaleGenerationAgent(_StubAgent):
    def __init__(self, error):
        super().__init__(raise_in=())
        self._error = error

    def _persist_session(self, *a, **k):
        raise self._error


def _finalize(agent, *, interrupted):
    messages = [{"role": "user", "content": "do a thing"}]
    return finalize_turn(
        agent,
        final_response=None,
        api_call_count=1,
        interrupted=interrupted,
        failed=False,
        messages=messages,
        conversation_history=None,
        effective_task_id="task-1",
        turn_id="turn-1",
        user_message="do a thing",
        original_user_message="do a thing",
        _should_review_memory=False,
        _turn_exit_reason="interrupted_by_user" if interrupted else "unknown",
    )


def _persist_records(caplog):
    return [r for r in caplog.records if "_persist_session" in r.getMessage()]


def test_interrupted_turn_stale_generation_is_a_warning_without_traceback(caplog):
    from agent.runtime_session_store import WorkerPersistenceError

    caplog.set_level(logging.DEBUG)
    result = _finalize(_StaleGenerationAgent(WorkerPersistenceError("stale_generation")), interrupted=True)
    records = _persist_records(caplog)
    assert [r.levelno for r in records] == [logging.WARNING]
    assert records[0].exc_info is None
    assert "generation retired (interrupted turn)" in records[0].getMessage()
    # Still recorded for the caller.
    assert result["cleanup_errors"] == ["persist_session: stale_generation"]


def test_interrupted_turn_runtime_store_stale_generation_is_a_warning(caplog):
    from hermes_state_runtime import RuntimeStoreError

    caplog.set_level(logging.DEBUG)
    _finalize(_StaleGenerationAgent(RuntimeStoreError("stale_generation")), interrupted=True)
    assert [r.levelno for r in _persist_records(caplog)] == [logging.WARNING]


def test_uninterrupted_turn_stale_generation_stays_an_error(caplog):
    from agent.runtime_session_store import WorkerPersistenceError

    caplog.set_level(logging.DEBUG)
    _finalize(_StaleGenerationAgent(WorkerPersistenceError("stale_generation")), interrupted=False)
    records = _persist_records(caplog)
    assert [r.levelno for r in records] == [logging.ERROR]
    assert records[0].exc_info is not None


@pytest.mark.parametrize(
    "error_factory",
    [
        lambda: RuntimeError("sqlite database is locked"),
        lambda: __import__("agent.runtime_session_store", fromlist=["x"]).WorkerPersistenceError("outbox_full"),
        lambda: RuntimeError("stale_generation"),  # right text, wrong type: not a store fence
    ],
)
def test_interrupted_turn_other_errors_stay_errors_with_traceback(caplog, error_factory):
    caplog.set_level(logging.DEBUG)
    _finalize(_StaleGenerationAgent(error_factory()), interrupted=True)
    records = _persist_records(caplog)
    assert [r.levelno for r in records] == [logging.ERROR]
    assert records[0].exc_info is not None
