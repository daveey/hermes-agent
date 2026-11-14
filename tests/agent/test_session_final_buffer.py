"""Dynamic gates: real turn setup, hook dispatch, stream sink and tool-result loop.

The deterministic callback is the status-fresh protocol, without a dependency on
an installed coach plugin. Only model replies and the external read are scripted.
"""
from types import SimpleNamespace

import pytest

from agent.pre_final_gate import needs_buffer
from hermes_cli import plugins
from tests.agent import test_pre_final_gate as support

agent = support.agent
ASK = "what going on here, why are we blocked and nothing running"
DRAFT = "The follow-ups were assigned to a cloud worker but never started."
CORRECTED = "The card is running and its PR is in review."


@pytest.fixture
def session_gate():
    armed = {}

    def pre(session_id, user_message, **kwargs):
        armed.pop(session_id, None)
        if user_message == ASK:
            armed[session_id] = False
        gate.needs_buffer = bool(armed)

    def post(session_id, **kwargs):
        armed.pop(session_id, None)
        gate.needs_buffer = bool(armed)

    def gate(session_id, turn_messages, **kwargs):
        if session_id not in armed or armed[session_id]:
            return None
        if any(m.get("tool_calls") for m in turn_messages):
            return None
        armed[session_id] = True
        return {"hold": True, "nudge": "Read live state before reporting status."}

    gate.needs_buffer = False
    gate.needs_buffer_for_session = lambda *, session_id: session_id in armed
    return {"pre_llm_call": pre, "post_llm_call": post, "pre_final_gate": gate}


@pytest.fixture
def hooks(agent, session_gate, monkeypatch):
    manager = plugins.PluginManager()
    manager._discovered = True
    manager._hooks = {name: [cb] for name, cb in session_gate.items()}
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugins, "_delivery_manager", lambda: manager)
    return manager


class Script(support.Run):
    def __init__(self, agent, answers):
        super().__init__(agent, answers)
        self.immediate = []
        self.before_emit = None

    def _call(self, api_kwargs):
        if self.before_emit:
            self.before_emit()
        self.requests.append(api_kwargs)
        answer = self.answers.pop(0)
        response = support._response(answer) if isinstance(answer, str) else answer
        text = response.choices[0].message.content
        before = len(self.screen)
        self.agent._fire_stream_delta(text)
        self.immediate.append(self.screen[before:])
        return response


def test_first_status_holds_before_any_sink_and_reads_tool(agent, hooks, monkeypatch, tmp_path):
    card = tmp_path / "card.json"
    card.write_text('{"status":"running","pr":"in review"}')
    reads = []

    def read_card(*args, **kwargs):
        reads.append(card.read_text())
        return reads[-1]

    # Keep the real executor, tool row construction and continuation. Substitute
    # only the external service boundary, not turn setup or stream buffering.
    monkeypatch.setattr("model_tools.handle_function_call", read_card)
    agent.valid_tool_names = {"kanban_show"}
    call = SimpleNamespace(id="read1", type="function", function=SimpleNamespace(
        name="kanban_show", arguments="{}"))
    response = support._response("Checking the card.")
    response.choices[0].message.tool_calls = [call]
    response.choices[0].finish_reason = "tool_calls"
    run = Script(agent, [DRAFT, response, CORRECTED])
    result = agent.run_conversation(ASK)
    assert reads == [card.read_text()]
    assert result["final_response"] == CORRECTED
    assert run.immediate[0] == []
    assert DRAFT not in run.shown
    assert CORRECTED in run.shown
    assert any(m["role"] == "tool" and "in review" in m["content"] for m in result["messages"])
    assert agent._pre_final_gate_attempts == 1


def test_ordinary_streams_while_other_status_session_is_armed(agent, hooks, session_gate):
    session_gate["pre_llm_call"](session_id="other-status", user_message=ASK)
    run = Script(agent, ["Hello."])
    result = agent.run_conversation("hello")
    assert result["final_response"] == "Hello."
    assert run.immediate == [["Hello."]]


def test_next_ordinary_turn_clears_stale_eligibility(agent, hooks):
    first = Script(agent, [DRAFT, CORRECTED])
    agent.run_conversation(ASK)
    assert first.immediate == [[], []]
    # Even an interrupted prior cleanup must not retain the turn's buffer.
    agent._final_gate_buffer = ["stale rejected delta"]
    run = Script(agent, ["Hello."])
    result = agent.run_conversation("hello")
    assert run.immediate == [["Hello."]]
    assert "stale" not in run.shown
    assert result["final_response"] == "Hello."
    assert agent._pre_final_gate_attempts == 0


def test_status_holds_only_once_without_reset_on_continuation(agent, hooks):
    run = Script(agent, [DRAFT, DRAFT])
    result = agent.run_conversation(ASK)
    assert len(run.requests) == 2
    assert agent._pre_final_gate_attempts == 1
    assert result["final_response"] == DRAFT
    assert run.immediate == [[], []]
    assert run.screen == [DRAFT]


def test_cancel_discards_dynamic_buffer(agent, hooks):
    run = Script(agent, [DRAFT])
    run.before_emit = lambda: setattr(agent, "_interrupt_requested", True)
    result = agent.run_conversation(ASK)
    assert DRAFT not in run.shown
    assert DRAFT not in (result["final_response"] or "")


@pytest.mark.parametrize("legacy,decision", [(False, True), (True, False)])
def test_predicate_overrides_legacy(agent, hooks, legacy, decision):
    gate = hooks._hooks["pre_final_gate"][0]
    seen = []
    gate.needs_buffer = legacy

    def predicate(*, session_id):
        seen.append(session_id)
        return decision

    gate.needs_buffer_for_session = predicate
    assert needs_buffer(agent) is decision
    assert seen == [agent.session_id]


@pytest.mark.parametrize("legacy", [True, False, lambda: True])
def test_predicate_error_logs_and_falls_back(agent, hooks, caplog, legacy):
    gate = hooks._hooks["pre_final_gate"][0]
    gate.needs_buffer = legacy

    def broken(*, session_id):
        raise ValueError("predicate unavailable")

    gate.needs_buffer_for_session = broken
    assert needs_buffer(agent) is (False if callable(legacy) else legacy)
    assert "predicate" in caplog.text


@pytest.mark.parametrize("legacy,expected", [(True, True), (False, False), (lambda: True, False)])
def test_legacy_boolean_not_callable_truthiness(agent, hooks, legacy, expected):
    gate = hooks._hooks["pre_final_gate"][0]
    del gate.needs_buffer_for_session
    gate.needs_buffer = legacy
    assert needs_buffer(agent) is expected


@pytest.mark.parametrize("kind", ["cron", "delegate", "fork", "persist_disabled"])
def test_scope_exclusion_applies_before_predicate(agent, hooks, kind):
    gate = hooks._hooks["pre_final_gate"][0]
    calls = []
    gate.needs_buffer_for_session = lambda **kw: calls.append(kw) or True
    if kind == "cron":
        agent.platform = "cron"
    elif kind == "delegate":
        agent._delegate_depth = 1
    elif kind == "fork":
        agent._turn_origin = "btw"
    else:
        agent._persist_disabled = True
    assert needs_buffer(agent) is False
    assert calls == []
    if kind in ("cron", "delegate"):
        gate.include_background = True
    else:
        gate.include_forks = True
    assert needs_buffer(agent) is True
    assert calls == [{"session_id": agent.session_id}]


def test_gate_without_buffer_attributes_streams_ordinary_turn(agent, hooks):
    # A legacy gate that declares neither predicate nor needs_buffer must not
    # buffer: the missing flag defaults to False, so ordinary text streams live.
    seen = []

    def plain_gate(session_id, turn_messages, **kwargs):
        seen.append(session_id)  # no verdict: let the reply through

    hooks._hooks["pre_final_gate"] = [plain_gate]
    assert needs_buffer(agent) is False
    run = Script(agent, ["Hello."])
    result = agent.run_conversation("hello")
    assert result["final_response"] == "Hello."
    assert run.immediate == [["Hello."]]
    assert agent._final_gate_buffer is None
    assert seen  # the gate itself still ran at finalization


def test_last_pre_hook_controls_buffer(agent, hooks):
    gate = hooks._hooks["pre_final_gate"][0]
    # Legacy dynamic gates are also armed after *all* hooks, not just the first.
    del gate.needs_buffer_for_session
    hooks._hooks["pre_llm_call"].append(lambda **kw: setattr(gate, "needs_buffer", True))
    run = Script(agent, ["Hello."])
    agent.run_conversation("hello")
    assert run.immediate == [[]]
    assert run.screen == ["Hello."]
