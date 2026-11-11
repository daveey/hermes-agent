"""Bound early answers: a decision ends a blocked clarify only for the exact question it was bound to.

Session matching alone (the pre-binding contract) let any undelivered decision of the same session end an unrelated
clarify. Here a provider that declares ``accepts_question`` receives the exact displayed question descriptor and
must echo its digest and tool call; legacy decision answers are ignored for a bound question, while a legacy
withdrawal that answers nothing (the update pause) still works.
"""

import json
import threading

import pytest

from tools import clarify_early
from tools.clarify_tool import clarify_tool

QUESTIONS = [{"question": "Which runner?", "choices": ["canary", "aws"]}]
WAIT_S = 10


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(clarify_early, "POLL_INTERVAL_S", 0.05)
    clarify_early.reset_for_tests()
    yield
    clarify_early.reset_for_tests()


class Blocking:
    def __init__(self):
        self.event = threading.Event()
        self.entered = threading.Event()
        self.calls = 0

    def __call__(self, questions):
        self.calls += 1
        self.entered.set()
        self.event.wait(WAIT_S)
        return {"answers": {}, "outcome": "cancelled"}

    def cancel_early(self, reason):
        self.event.set()
        return True


def _decision_answer(commits, binding=None):
    result = {"status": "answered_elsewhere", "decisions": [{"requestId": "r1", "title": "Which runner?",
                                                             "choice": "canary"}]}
    if binding is not None:
        result["binding"] = binding
    return clarify_early.EarlyAnswer(result, commit=lambda: commits.append("done"))


def test_descriptor_is_the_exact_ordered_snapshot_with_a_stable_digest():
    a = clarify_early.question_descriptor([{"question": "Q", "choices_offered": ["x", "y"]}], "call_1")
    b = clarify_early.question_descriptor([{"question": "Q", "choices_offered": ["y", "x"]}], "call_1")
    assert a["questions"] == [{"question": "Q", "choices": ["x", "y"]}] and a["toolCallId"] == "call_1"
    assert a["digest"] != b["digest"]  # order is part of the question identity
    assert a["digest"] == clarify_early.question_descriptor([{"question": "Q", "choices_offered": ["x", "y"]}],
                                                            "other")["digest"]


def test_bound_provider_receives_the_descriptor_and_its_echo_ends_the_clarify():
    seen, commits = [], []

    def provider(sid, tcid, question=None):
        seen.append(question)
        return _decision_answer(commits, {"toolCallId": tcid, "questionDigest": question["digest"]})
    provider.accepts_question = True
    clarify_early.register_provider(provider)
    cb = Blocking()
    out = json.loads(clarify_tool(QUESTIONS, callback=cb, session_id="s1", tool_call_id="call_a"))
    assert out["status"] == "answered_elsewhere" and commits == ["done"] and cb.calls == 0
    assert seen[0]["toolCallId"] == "call_a"
    assert seen[0]["questions"] == [{"question": "Which runner?", "choices": ["canary", "aws"]}]


def test_unbound_same_session_decision_does_not_end_the_clarify():
    """Negative control: the provider answers for the right session but for a different question."""
    commits = []

    def provider(sid, tcid, question=None):
        return _decision_answer(commits, {"toolCallId": tcid, "questionDigest": "0" * 64})
    provider.accepts_question = True
    clarify_early.register_provider(provider)
    cb = Blocking()
    box = {}
    t = threading.Thread(target=lambda: box.update(out=json.loads(clarify_tool(
        QUESTIONS, callback=cb, session_id="s1", tool_call_id="call_b"))))
    t.start()
    assert cb.entered.wait(WAIT_S)
    t.join(0.5)
    assert t.is_alive(), "an unbound decision must not end the clarify"
    cb.event.set(); t.join(WAIT_S)
    assert commits == [] and box["out"].get("status") != "answered_elsewhere"


def test_legacy_decision_answer_cannot_end_a_bound_question():
    commits = []
    clarify_early.register_provider(lambda sid, tcid: _decision_answer(commits))
    cb = Blocking()
    box = {}
    t = threading.Thread(target=lambda: box.update(out=json.loads(clarify_tool(
        QUESTIONS, callback=cb, session_id="s1", tool_call_id="call_c"))))
    t.start()
    assert cb.entered.wait(WAIT_S)
    t.join(0.5)
    assert t.is_alive()
    cb.event.set(); t.join(WAIT_S)
    assert commits == []


def test_echo_for_another_tool_call_is_rejected():
    commits = []

    def provider(sid, tcid, question=None):
        return _decision_answer(commits, {"toolCallId": "call_other", "questionDigest": question["digest"]})
    provider.accepts_question = True
    assert clarify_early.poll("s1", "call_d", clarify_early.question_descriptor(QUESTIONS, "call_d")) is None
    clarify_early.register_provider(provider)
    assert clarify_early.poll("s1", "call_d", clarify_early.question_descriptor(QUESTIONS, "call_d")) is None


def test_legacy_withdrawal_without_decisions_still_ends_the_clarify():
    """The update-pause provider answers nothing; it may still withdraw the clarify."""
    clarify_early.register_provider(lambda sid, tcid: clarify_early.EarlyAnswer(
        {"status": "paused_for_update", "outcome": "paused_for_update", "notice": "paused"}, commit=lambda: None))
    out = json.loads(clarify_tool(QUESTIONS, callback=Blocking(), session_id="s1", tool_call_id="call_e"))
    assert out["status"] == "paused_for_update"


def test_poll_without_descriptor_keeps_the_legacy_two_argument_contract():
    commits = []
    clarify_early.register_provider(lambda sid, tcid: _decision_answer(commits))
    assert clarify_early.poll("s1", "call_f") is not None
