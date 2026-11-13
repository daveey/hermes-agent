"""A slash-command /loop tick's submitted prompt row is typed ``loop_tick`` (TUI and gateway) and every reader treats
it like the other typed synthetic rows: never a person's reply, a notice in transcripts, a turn boundary for the
compressor, machinery for silence, and not an automation-replay turn (the loop owns its own retry). [daveey]"""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from hermes_cli import loops
from hermes_cli.loops import LOOP_TICK_DISPLAY_KIND, loop_tick_display


def test_loop_tick_display_names_the_command_not_the_expanded_prompt():
    assert loop_tick_display("  /recap   the  ring ") == {
        "display_kind": "loop_tick", "display_metadata": {"display_text": "↻ /loop /recap the ring"}}


def test_tui_slash_tick_submits_its_prompt_typed(monkeypatch):
    from tui_gateway import server
    monkeypatch.setitem(server._methods, "command.dispatch",
                        lambda rid, params: {"result": {"type": "send", "message": "Run the recap skill now"}})
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    submitted: list = []
    monkeypatch.setattr(server, "_notif_submit", lambda *a, **k: submitted.append((a, k)) or True)
    session = {"history_lock": threading.RLock(), "running": True, "history": []}
    mgr = SimpleNamespace(abandon_tick=lambda: None, complete_tick=lambda text: {})

    server._notif_slash_loop_tick("rid", "sid", session, mgr, "/recap")

    (args, kwargs), = submitted
    assert args[3] == "Run the recap skill now"
    assert kwargs == loop_tick_display("/recap")


class _Runner:
    """The few GatewayRunner seams ``_loop_wakeup_fire_one`` touches."""

    def __init__(self, adapter):
        self._adapter, self._running_agents = adapter, {}

    def _adapters_for_profile(self, profile):
        from gateway.config import Platform
        return {Platform.DISCORD: self._adapter}

    def _build_process_event_source(self, evt):
        return SimpleNamespace(chat_id=evt["chat_id"], thread_id="", message_id=None, profile=None)

    def _session_key_for_source(self, source):
        return "agent:main:discord:dm:42"

    async def _run_in_executor_with_context(self, func, *args):
        return func(*args)

    async def _publish_local_control(self, source):
        return None


@pytest.mark.parametrize("wakeup, typed", [("/recap", True), ("check the ring card", False)])
def test_gateway_slash_tick_event_persists_as_loop_tick(monkeypatch, wakeup, typed):
    from gateway import run_goals, session_automation
    from gateway.response_filters import display_kind_for_event, display_metadata_for_event, \
        is_machinery_display_kind

    events: list = []

    async def handle_message(event):
        events.append(event)
    adapter = SimpleNamespace(handle_message=handle_message)
    runner = _Runner(adapter)
    runner._synthetic_prompt_event = run_goals.GatewayGoalsMixin._synthetic_prompt_event

    class Loop:
        state = SimpleNamespace(ticks_fired=1)

        def __init__(self, session_id=None):
            pass

        def is_due(self, now=None):
            return True

        def fire_tick(self):
            return wakeup

        def complete_tick(self, text):
            return {}

        def abandon_tick(self):
            raise AssertionError("the tick was injected")
    monkeypatch.setattr(loops, "LoopManager", Loop)
    monkeypatch.setattr(loops, "goal_blocks_loop_tick", lambda sid: False)
    monkeypatch.setattr("agent.estop.check_paused", lambda *a, **k: False)

    async def no_authority(*a, **k):
        return False
    monkeypatch.setattr(session_automation, "admit_route_wake", no_authority)
    route = {"platform": "discord", "chat_id": "42", "chat_type": "dm", "user_id": "42"}
    asyncio.run(run_goals.GatewayGoalsMixin._loop_wakeup_fire_one(
        runner, "sid-1", SimpleNamespace(awaiting_response=False, next_due_at=0, route=route), 1e12, set()))

    event, = events
    assert event.internal is True
    if typed:
        assert display_kind_for_event(event) == LOOP_TICK_DISPLAY_KIND
        assert display_metadata_for_event(event) == loop_tick_display(wakeup)["display_metadata"]
    else:  # a wakeup-framed loop keeps its generic internal row (its frame identifies it)
        assert display_kind_for_event(event) == "internal_notification"
    assert is_machinery_display_kind(display_kind_for_event(event))  # silence rules unchanged for the tick


def test_compressor_treats_a_loop_tick_as_a_new_turn_and_not_human_input():
    from agent.context_compressor import ContextCompressor
    from agent.context_compressor_turns import turn_ended_after
    messages = [{"role": "user", "content": "fix the header"},
                {"role": "assistant", "content": "done"},
                {"role": "user", "content": "Run the recap skill now", "display_kind": "loop_tick"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]}]
    assert turn_ended_after(messages, 0) is True
    assert ContextCompressor._is_actionable_user_turn(messages[2]) is False


def test_history_projection_keeps_the_loop_tick_kind_and_its_line():
    from tui_gateway.server import _history_to_messages
    meta = loop_tick_display("/recap")["display_metadata"]
    projected, = _history_to_messages([{"role": "user", "content": "Run the recap skill now",
                                        "display_kind": "loop_tick", "display_metadata": meta}])
    assert projected["display_kind"] == "loop_tick" and projected["display_metadata"] == meta


def test_cli_resume_recap_shows_the_loop_line_not_the_prompt():
    from hermes_cli.cli_agent_setup_mixin import _collect_resume_entries
    rows = [{"role": "user", "content": "Run the recap skill now", **loop_tick_display("/recap")},
            {"role": "user", "content": "Run it again", "display_kind": "loop_tick"}]
    entries, _, _ = _collect_resume_entries(rows, {}, lambda s: s)
    assert entries == [("event", "↻ /loop /recap"), ("event", "↻ /loop")]


def test_a_loop_tick_is_not_an_automation_replay_turn():
    """The loop schedules its own next tick; a failed tick is not replayed by automation_replay."""
    from tui_gateway import automation_replay
    assert automation_replay.is_automation(LOOP_TICK_DISPLAY_KIND) is False
