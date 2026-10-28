"""A decision recorded elsewhere (user-steps For-you queue) ends a blocked clarify instead of letting it time out."""

import json
import threading
import time

import pytest

from tools import clarify_early
from tools.clarify_tool import clarify_tool

QUESTIONS = [{"question": "Which request does 'approved' cover?", "choices": ["canary", "aws", "both"]}]
DECISION = {"requestId": "r1", "title": "Canary runner", "choice": "approve", "decidedAt": "2026-10-28T21:19:45+00:00",
            "via": "for-you-queue"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(clarify_early, "POLL_INTERVAL_S", 0.05)
    clarify_early.reset_for_tests()
    yield
    clarify_early.reset_for_tests()


class Blocking:
    """A surface callback that blocks until answered or cancelled, like the TUI bridge."""

    def __init__(self):
        self.event = threading.Event()
        self.reply = None
        self.calls = 0
        self.cancel_reasons = []

    def __call__(self, questions):
        self.calls += 1
        self.event.wait(5)
        return self.reply or {"answers": {}, "outcome": "cancelled"}

    def cancel_early(self, reason):
        self.cancel_reasons.append(reason)
        self.event.set()
        return True


def _answer(commits, decisions=(DECISION,)):
    return clarify_early.EarlyAnswer(
        {"status": "answered_elsewhere", "decisions": list(decisions)}, commit=lambda: commits.append("done"))


def test_pending_decision_is_returned_instead_of_asking():
    """(c) check before asking."""
    commits, seen = [], []
    clarify_early.register_provider(lambda sid, tcid: seen.append((sid, tcid)) or _answer(commits))
    cb = Blocking()
    out = json.loads(clarify_tool(QUESTIONS, callback=cb, session_id="s1", tool_call_id="call_1"))
    assert cb.calls == 0
    assert out["status"] == "answered_elsewhere"
    assert out["decisions"][0]["requestId"] == "r1"
    assert out["notice"].startswith("David answered in the For-you queue")
    assert "responses" not in out  # never shaped like text the user typed
    assert commits == ["done"]
    assert seen == [("s1", "call_1")]


def test_decision_recorded_while_blocked_ends_the_clarify():
    """(a) end the clarify early."""
    commits, state = [], {"ready": False}
    clarify_early.register_provider(lambda sid, tcid: _answer(commits) if state["ready"] else None)
    cb = Blocking()
    box = {}
    t = threading.Thread(target=lambda: box.update(
        out=json.loads(clarify_tool(QUESTIONS, callback=cb, session_id="s1", tool_call_id="call_1"))))
    started = time.monotonic()
    t.start()
    time.sleep(0.2)
    assert t.is_alive() and cb.calls == 1  # genuinely blocked, nothing to deliver yet
    state["ready"] = True
    t.join(3)
    assert not t.is_alive()
    assert time.monotonic() - started < 3
    assert box["out"]["status"] == "answered_elsewhere"
    assert cb.cancel_reasons == ["answered_elsewhere"]
    assert commits == ["done"]


def test_decision_for_another_session_does_not_end_the_clarify():
    """Guard: the provider says None for a session it does not own; the clarify keeps waiting for the user."""
    commits = []
    clarify_early.register_provider(lambda sid, tcid: _answer(commits) if sid == "other" else None)
    cb = Blocking()
    box = {}
    t = threading.Thread(target=lambda: box.update(
        out=json.loads(clarify_tool(QUESTIONS, callback=cb, session_id="s1", tool_call_id="call_1"))))
    t.start()
    time.sleep(0.4)
    assert t.is_alive() and cb.cancel_reasons == [] and commits == []
    cb.reply = {"answers": {"q0": "both"}, "outcome": "submitted"}
    cb.event.set()
    t.join(3)
    assert box["out"]["responses"][0]["user_response"] == "both"
    assert commits == []


def test_a_real_answer_that_wins_the_race_is_not_replaced():
    """The human answered at the same moment: their answer is returned and the decision stays undelivered."""
    commits, state = [], {"ready": False}
    clarify_early.register_provider(lambda sid, tcid: _answer(commits) if state["ready"] else None)

    class Uncancellable(Blocking):
        def cancel_early(self, reason):
            self.cancel_reasons.append(reason)
            return False

    cb = Uncancellable()
    cb.reply = {"answers": {"q0": "aws"}, "outcome": "submitted"}
    box = {}
    t = threading.Thread(target=lambda: box.update(
        out=json.loads(clarify_tool(QUESTIONS, callback=cb, session_id="s1", tool_call_id="call_1"))))
    t.start()
    time.sleep(0.2)
    state["ready"] = True
    cb.event.set()
    t.join(3)
    assert box["out"]["responses"][0]["user_response"] == "aws"
    assert commits == []


def test_provider_failure_never_breaks_the_clarify():
    def boom(sid, tcid):
        raise RuntimeError("store unavailable")

    clarify_early.register_provider(boom)
    cb = Blocking()
    cb.reply = {"answers": {"q0": "aws"}, "outcome": "submitted"}
    cb.event.set()
    out = json.loads(clarify_tool(QUESTIONS, callback=cb, session_id="s1", tool_call_id="call_1"))
    assert out["responses"][0]["user_response"] == "aws"


def test_without_a_session_or_cancel_hook_behaviour_is_unchanged():
    """Surfaces with no cancel hook (plain CLI) and calls with no session id never poll."""
    seen = []
    clarify_early.register_provider(lambda sid, tcid: seen.append(sid))
    out = json.loads(clarify_tool(QUESTIONS, callback=lambda qs: {"answers": {"q0": "aws"}, "outcome": "submitted"},
                                  session_id="s1", tool_call_id="call_1"))
    assert out["responses"][0]["user_response"] == "aws"
    assert seen == ["s1"]  # the pre-ask check still runs; there is no watcher thread
    seen.clear()
    clarify_tool(QUESTIONS, callback=lambda qs: {"answers": {}, "outcome": "submitted"})
    assert seen == []


def test_inline_executor_passes_session_and_tool_call_id():
    """The agent path (inline executor) hands the clarify tool the agent's session id and the call's id."""
    from types import SimpleNamespace

    from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext

    seen = []
    clarify_early.register_provider(lambda sid, tcid: seen.append((sid, tcid)))
    agent = SimpleNamespace(session_id="stored-1", clarify_callback=lambda qs: {"answers": {"q0": "x"}, "outcome": "submitted"})
    out = INLINE_TOOL_EXECUTORS["clarify"](agent, {"questions": QUESTIONS}, InlineToolContext("t", tool_call_id="call_9"))
    assert json.loads(out)["outcome"] == "submitted"
    assert seen == [("stored-1", "call_9")]
