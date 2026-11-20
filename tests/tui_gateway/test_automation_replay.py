"""Unit tests for tui_gateway.automation_replay (the bounded one-replay owner of failed automation turns)."""
from __future__ import annotations

import threading
import types

import pytest

from tui_gateway import automation_replay as ar

NOW = 1_800_000_000.0
TASK = "t_0123456789abcdef"


class _Timers(list):
    def __call__(self, delay, fn):
        timer = types.SimpleNamespace(start=lambda: None, daemon=False, cancelled=False)
        timer.cancel = lambda: setattr(timer, "cancelled", True)
        self.append((delay, fn, timer))
        return timer


@pytest.fixture()
def timers(monkeypatch):
    fake = _Timers()
    monkeypatch.setattr(threading, "Timer", fake)
    monkeypatch.setattr(ar, "ENABLED", True)
    ar.reset()
    yield fake
    ar.reset()


def test_the_plan_never_asks_to_send_it_again(timers):
    plan = ar.plan_turn_failure("kanban_notice", {"task_ids": [TASK]}, "x", {"error": "lease\nmore"}, now=NOW)
    assert plan.retry and plan.due == NOW + 60 and plan.cause == "lease"
    assert plan.line.startswith(f"Couldn't process the notice for {TASK}: lease. Retrying once at ")
    assert plan.line.endswith(" PT.")
    gave_up = ar.plan_turn_failure("process_complete", {ar.REPLAY_METADATA_KEY: {"attempt": 1}}, "x",
                                   {"error": "lease"}, now=NOW)
    assert not gave_up.retry and gave_up.line == ("Couldn't process the background-job report: lease. "
                                                  "Gave up after one retry.")
    assert ar.plan_turn_failure(None, {}, "x", {"error": "e"}) is None  # a person's turn: not ours


def test_a_non_retryable_failure_still_gets_its_one_replay(timers):
    # The lease refusal behind the 19 lost notices was classified "Non-retryable client error".
    plan = ar.plan_turn_failure("kanban_notice", {}, f"{TASK} done",
                                {"error": "Chat ownership lease is unavailable", "failure_retryable": False}, now=NOW)
    assert plan.retry and plan.due == NOW + 60 and "Retrying once at" in plan.line and TASK in plan.line


def test_the_recorded_reset_sets_the_time():
    assert ar.replay_due(NOW, NOW + 3600) == NOW + 3600
    assert ar.replay_due(NOW, NOW + 5) == NOW + 60  # never sooner than the floor
    assert ar.replay_due(NOW, None) == NOW + 60 and ar.replay_due(NOW, True) == NOW + 60


def test_one_timer_per_chat_and_a_later_reset_rearms(timers):
    fired = []
    for n in range(3):
        ar.schedule("k", text=f"d{n}", display_kind="kanban_notice", display_metadata={}, due=NOW + 60 + n,
                    cause="c", persisted=True, now=NOW, fire=fired.append)
    assert len(timers) == 1
    ar.schedule("k", text="quota", display_kind="kanban_notice", display_metadata={}, due=NOW + 7200, cause="q",
                persisted=True, now=NOW, fire=fired.append)
    assert len(timers) == 2 and timers[0][2].cancelled and timers[1][0] == 7200


def test_cancelled_timer_cannot_consume_replacement(timers, monkeypatch):
    from tui_gateway import server
    wakes = []
    monkeypatch.setattr(server, "_wake_session_turn", lambda *a, **kw: wakes.append(a) or "started")
    ar.schedule("k", text="old", display_kind="kanban_notice", display_metadata={}, due=NOW + 60,
                cause="c", persisted=True, now=NOW)
    old_fire = timers[-1][1]
    ar.schedule("k", text="later reset", display_kind="kanban_notice", display_metadata={}, due=NOW + 7200,
                cause="q", persisted=True, now=NOW)
    old_fire()  # Timer.cancel cannot stop a callback already waiting for the lock.
    assert wakes == [] and ar.pending("k")
    timers[-1][1]()
    assert len(wakes) == 1 and not ar.pending("k")


def test_cancel_carried_keeps_an_unpersisted_delivery(timers):
    ar.schedule("k", text="a", display_kind="kanban_notice", display_metadata={}, due=NOW, cause="c", persisted=True,
                now=NOW)
    ar.schedule("k", text="b", display_kind="kanban_notice", display_metadata={}, due=NOW, cause="c", persisted=False,
                now=NOW)
    assert ar.cancel_carried("k") and ar.pending("k")
    assert not ar.cancel_carried("k")  # what is left never reached the transcript
    ar.reset()
    ar.schedule("k", text="a", display_kind="kanban_notice", display_metadata={}, due=NOW, cause="c", persisted=True,
                now=NOW)
    assert ar.cancel_carried("k") and not ar.pending("k") and timers[-1][2].cancelled


def test_the_replay_payload_coalesces_and_marks_itself():
    items = [ar._Item("first", "process_complete", {"display_text": "job"}, True, "c1", NOW),
             ar._Item(f"{TASK} done", "kanban_notice", {"task_ids": [TASK], "display_text": "card"}, True, "c2", NOW)]
    text, kind, meta = ar.replay_payload(items)
    assert kind == "kanban_notice" and "first" in text and f"{TASK} done" in text and "only retry" in text
    assert meta[ar.REPLAY_METADATA_KEY]["deliveries"] == 2 and meta["task_ids"] == [TASK]
    assert meta["display_text"] == "Retry: job · card"
    text, kind, meta = ar.replay_payload(items[:1])
    assert kind == "process_complete" and meta["display_text"] == "Retry: job" and ar.is_replay(meta)


def test_a_busy_chat_is_looked_at_again_then_given_up(timers, monkeypatch):
    from tui_gateway import server
    verdicts = []
    monkeypatch.setattr(server, "_wake_session_turn", lambda *a, **k: verdicts.append(k) or "busy", raising=False)
    ar.schedule("k", text="a", display_kind="kanban_notice", display_metadata={}, due=NOW, cause="c", persisted=True,
                now=NOW)
    for _ in range(ar.BUSY_RECHECKS_MAX + 5):
        timers[-1][1]()
    assert len(verdicts) == ar.BUSY_RECHECKS_MAX + 1 and not ar.pending("k")
    assert len(timers) == ar.BUSY_RECHECKS_MAX + 1


def test_disabled_schedules_nothing(timers, monkeypatch):
    monkeypatch.setattr(ar, "ENABLED", False)
    assert not ar.schedule("k", text="a", display_kind="kanban_notice", display_metadata={}, due=NOW, cause="c",
                           persisted=True, now=NOW)
    assert not timers


# [daveey] provider-169ae0ed1f: a replay refused by a quota wall that still holds is re-armed at its new reset.
# Hub chat 20261101_012339_5a8ef0, Nov 17: the replay fired at the 11:09:59Z reset, the provider still refused
# (credits_required; pool rebooked 12:10:12Z), the replay gave up, and the notice waited 8.5 h for a manual "resume".
def _replay_meta(attempt=1):
    return {"task_ids": [TASK], ar.REPLAY_METADATA_KEY: {"attempt": attempt, "failed_at": NOW - 4000, "cause": "c",
                                                         "deliveries": 1}}


def test_a_replay_refused_by_a_wall_that_still_holds_waits_for_the_new_reset(timers):
    wall = {"error": "HTTP 429: Subscription quota unavailable for anthropic/m; resets at x",
            "failure_resets_at": NOW + 3600}
    plan = ar.plan_turn_failure("kanban_notice", _replay_meta(), "x", wall, now=NOW)
    assert plan.retry and plan.due == NOW + 3600
    assert "Gave up" not in plan.line and "attempt 2 of" in plan.line
    # the cap: the last allowed attempt gives up even on a wall
    last = ar.plan_turn_failure("kanban_notice", _replay_meta(ar.WALL_REPLAYS_MAX), "x", wall, now=NOW)
    assert not last.retry and last.line.endswith(f"Gave up after {ar.WALL_REPLAYS_MAX} retries.")


@pytest.mark.parametrize("result", [
    {"error": "lease"},  # not a wall: the work itself failed
    {"error": "429", "failure_resets_at": NOW - 5},  # the reset passed: the wall is not what refused it
    {"error": "429", "failure_resets_at": NOW + 2 * 86400},  # too far out: the notice is stale by then
    {"error": "429", "failure_resets_at": True},
])
def test_any_other_replay_failure_still_gives_up(timers, result):
    plan = ar.plan_turn_failure("kanban_notice", _replay_meta(), "x", result, now=NOW)
    assert not plan.retry and plan.line.endswith("Gave up after one retry.")


def test_the_rearmed_replay_carries_the_deliveries_not_the_old_note(timers, monkeypatch):
    original = f"{TASK} done"
    item = ar._Item(original, "kanban_notice", {"task_ids": [TASK], "display_text": "card"}, False, "c", NOW - 4000)
    text, kind, meta = ar.replay_payload([item])
    st = types.SimpleNamespace(display_kind=kind, display_metadata=meta, automation_text=text, marker_key="k",
                               automation_plan=None)
    st.automation_plan = ar.plan_turn_failure(kind, meta, text, {"error": "429", "failure_resets_at": NOW + 3600},
                                              now=NOW)
    ar.schedule_for_turn({"session_key": "k"}, st)
    assert ar.pending("k")
    entry = ar._entries["k"]
    assert [i.text for i in entry.items] == [original] and entry.items[0].attempt == 1
    text2, _, meta2 = ar.replay_payload(entry.items)
    assert text2.count("[Hermes: automatic retry") == 1 and text2.endswith(original)
    assert meta2[ar.REPLAY_METADATA_KEY]["attempt"] == 2 and meta2["display_text"] == "Retry: card"
    assert meta2[ar.REPLAY_METADATA_KEY]["failed_at"] == NOW - 4000


def test_a_replay_wake_refused_by_a_wall_that_still_holds_is_rearmed(timers, monkeypatch):
    monkeypatch.setattr(ar, "stale_code_reason", lambda: None)
    reset = ar.time.time() + 3600
    ar.replay_failed_wake("k", "[Hermes: automatic retry of 1 automated delivery that failed at 01:50 PT (c). "
                          "This is the only retry. Check the current state before redoing anything.]\n\nbody",
                          {"agent_error": "quota", "agent_failure": {"failure_resets_at": reset}},
                          "kanban_notice", _replay_meta(), "notice-closeout")
    assert ar.pending("k") and [i.text for i in ar._entries["k"].items] == ["body"]
    ar.reset()
    ar.replay_failed_wake("k", "body", {"agent_error": "quota", "agent_failure": {}}, "kanban_notice",
                          _replay_meta(), "notice-closeout")
    assert not ar.pending("k")
