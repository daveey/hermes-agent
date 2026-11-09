"""turn_stop_gates._pre_verify_nudge: a registered ``pre_verify`` hook runs after the answer has
streamed, so the user must see a status while it runs and the log must carry its duration
(self-heal agent-7aa110cfd7: a 23 s silent reply check made a finished answer look stuck)."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from agent import turn_stop_gates, verify_hooks


class _Agent(SimpleNamespace):
    def __init__(self, events, **kw):
        base = dict(_turn_file_mutation_paths={"a.py"}, session_id="sess-1", platform="tui",
                    model="m", _resolved_is_coding=False)
        base.update(kw)
        super().__init__(**base)
        self._events = events

    def _emit_status(self, message):
        self._events.append(("status", message))


def _wire(monkeypatch, events, *, hook=True, result="keep going"):
    from hermes_cli import lifecycle, plugins

    monkeypatch.setattr(lifecycle, "has_hook", lambda name: hook and name == "pre_verify")
    monkeypatch.setattr(verify_hooks, "pre_verify_every_turn", lambda config=None: True)
    monkeypatch.setattr(verify_hooks, "max_verify_nudges", lambda config=None: 3)

    def fake(**kw):
        events.append(("hook", kw["session_id"]))
        return result

    monkeypatch.setattr(plugins, "get_pre_verify_continue_message", fake)


@pytest.mark.parametrize("result,continued", [("keep going", True), (None, False)])
def test_status_emitted_once_before_hook_and_duration_logged(monkeypatch, caplog, result, continued):
    events = []
    _wire(monkeypatch, events, result=result)
    agent = _Agent(events)
    with caplog.at_level(logging.INFO, logger="agent.conversation_loop"):
        out = turn_stop_gates._pre_verify_nudge(agent, "Done.", 0)
    assert out == result
    assert events == [("status", "Checking the reply before finishing…"), ("hook", "sess-1")]
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("pre_verify done ")]
    assert len(lines) == 1, caplog.text
    assert "session=sess-1" in lines[0] and "seconds=" in lines[0]
    assert f"continued={continued}" in lines[0]


def test_no_hook_no_status(monkeypatch, caplog):
    events = []
    _wire(monkeypatch, events, hook=False)
    with caplog.at_level(logging.INFO, logger="agent.conversation_loop"):
        assert turn_stop_gates._pre_verify_nudge(_Agent(events), "Done.", 0) is None
    assert events == []
    assert "pre_verify done" not in caplog.text


def test_fork_turn_no_status(monkeypatch):
    events = []
    _wire(monkeypatch, events)
    agent = _Agent(events, _turn_origin="background_review", _persist_disabled=True)
    assert turn_stop_gates._pre_verify_nudge(agent, "Done.", 0) is None
    assert events == []


def test_status_failure_never_skips_hook(monkeypatch):
    events = []
    _wire(monkeypatch, events)
    agent = _Agent(events)

    def boom(message):
        raise RuntimeError("no sink")

    agent._emit_status = boom
    assert turn_stop_gates._pre_verify_nudge(agent, "Done.", 0) == "keep going"
    assert events == [("hook", "sess-1")]
