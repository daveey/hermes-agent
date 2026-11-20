"""A turn refused for a retryable reason is replayed through the crash path, not left on "send it again". [daveey]"""

from __future__ import annotations

import threading
import types

import pytest

from tui_gateway import server
from tui_gateway.turn_marker import read_turn_marker, record_turn_start

from .test_auto_continue import _InlineThread, _session

LEASE = "Chat ownership lease is unavailable"


@pytest.fixture()
def emits(monkeypatch):
    captured: list = []
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None: captured.append((event, sid, payload)))
    return captured


@pytest.fixture()
def marker_home(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    return tmp_path


@pytest.fixture()
def turn_env(monkeypatch, tmp_path, marker_home):
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    # Inline turn execution must not inline the process-lifetime ledger daemon.
    from hermes_cli import kanban_session_activity as activity
    monkeypatch.setattr(activity, "_refresh_links_in_background", lambda scope: None)
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda sid, session: None)
    monkeypatch.setattr(server, "_session_cwd", lambda session: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda session: None)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None)
    monkeypatch.setattr(server, "_sync_session_key_after_compress", lambda *a, **k: None)
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})


class _Timers(list):
    """Records the wait instead of running it."""

    def __call__(self, delay, fn):
        self.append((delay, fn))
        return types.SimpleNamespace(start=lambda: None, daemon=False)


@pytest.fixture()
def timers(monkeypatch):
    fake = _Timers()
    monkeypatch.setattr(threading, "Timer", fake)
    monkeypatch.setattr(server, "_REFUSED_RETRY_DELAYS_S", (45.0, 180.0))
    monkeypatch.setattr(server, "_active_goal_manager", lambda session: None)
    monkeypatch.setattr(server, "_auto_continue_config", lambda: (True, 900.0, 2))
    return fake


def _refused(message, **kwargs):
    return {"failed": True, "error": LEASE, "final_response": None}


def _agent(run=_refused):
    return types.SimpleNamespace(session_id="session-key", run_conversation=run, clear_interrupt=lambda: None)


def test_retryable_refusal_keeps_the_marker_and_waits(emits, turn_env, marker_home, timers):
    session = _session(agent=_agent(), running=True)
    server._run_prompt_submit("rid", "sid", session, "archive my receipts")
    marker = read_turn_marker(marker_home, "session-key")
    assert marker["prompt"] == "archive my receipts" and marker["attempts"] == 0
    assert [t[0] for t in timers] == [45.0]


def test_non_retryable_failure_clears_the_marker_and_never_waits(emits, turn_env, marker_home, timers):
    def bad_model(message, **kwargs):
        return {"failed": True, "error": "HTTP 400: claude-opus-5-5 is not a valid model ID", "final_response": None,
                "failure_reason": "invalid_request", "failure_retryable": False}

    session = _session(agent=_agent(bad_model), running=True)
    server._run_prompt_submit("rid", "sid", session, "archive my receipts")
    assert read_turn_marker(marker_home, "session-key") is None and not timers


@pytest.mark.parametrize("error,retryable", [
    (ValueError("Account selection requires a live main chat"), False),
    (TypeError("invalid local request"), False),
    (TimeoutError("request timed out"), True),
])
def test_terminal_error_auto_continue_follows_settlement(
        emits, turn_env, marker_home, timers, error, retryable):
    from agent.error_classifier import classify_api_error
    from agent.turn_api_error import settle_unrecovered_error
    from agent.turn_retry_state import TurnRetryState
    from tests.agent.test_nonretryable_result_carries_verdict import _Agent

    classified = classify_api_error(error, provider="custom", model="m")
    # The initial generic classifier is permissive; settlement knows local validation cannot recover.
    assert classified.retryable is True

    terminal_agent = _Agent()
    terminal_agent.provider = "custom"
    terminal_agent.base_url = "https://example.com/v1"
    terminal_agent.api_mode = "chat_completions"
    terminal_agent._auto_recovery_cycles = 0

    def run(message, **kwargs):
        verdict = settle_unrecovered_error(
            terminal_agent, api_error=error, classified=classified, _retry=TurnRetryState(),
            status_code=None, error_msg=str(error), is_context_length_error=False,
            is_rate_limited=False, _is_zai_coding_overload=False, _provider="custom",
            _base="https://example.com/v1", _model="m", messages=[], api_messages=[],
            api_kwargs=None, active_system_prompt="", conversation_history=[], approx_tokens=10,
            retry_count=1, max_retries=1, compression_attempts=0, api_call_count=1,
        )
        assert verdict.action == "return"
        return verdict.result

    session = _session(agent=_agent(run), running=True)
    server._run_prompt_submit("rid", "sid", session, "archive my receipts")
    marker = read_turn_marker(marker_home, "session-key")
    assert (marker is not None) is retryable
    assert [t[0] for t in timers] == ([45.0] if retryable else [])
    if not retryable:
        assert server._maybe_schedule_auto_continue("sid", session, "session-key") is None
    terminal = [payload for event, _, payload in emits if event == "message.complete"][-1]
    assert terminal["status"] == "error"
    assert terminal["error"] == str(error)
    assert terminal["error_surface"]["retryable"] is retryable


def test_a_replay_waits_longer_and_the_last_one_gives_up(emits, turn_env, marker_home, timers):
    session = _session(agent=_agent(), running=True, _auto_continue_attempt=1, _auto_continue_prompt="p")
    server._run_prompt_submit("rid", "sid", session, server._auto_continue_note("p"), display_kind="auto_continue")
    assert read_turn_marker(marker_home, "session-key")["attempts"] == 1
    assert [t[0] for t in timers] == [180.0]

    timers.clear()
    session = _session(agent=_agent(), running=True, _auto_continue_attempt=2, _auto_continue_prompt="p")
    server._run_prompt_submit("rid", "sid", session, server._auto_continue_note("p"), display_kind="auto_continue")
    assert read_turn_marker(marker_home, "session-key") is None and not timers


@pytest.fixture()
def replay(monkeypatch, timers):
    """The automation-turn replay switched on (tests/tui_gateway/conftest.py keeps it off elsewhere)."""
    import importlib.util
    mod = None
    if importlib.util.find_spec("tui_gateway.automation_replay") is not None:
        from tui_gateway import automation_replay as mod
        monkeypatch.setattr(mod, "ENABLED", True)
        mod.reset()
    yield mod
    if mod is not None:
        mod.reset()


KINDS = ["kanban_notice", "process_complete"]
TASK = "t_0123456789abcdef"
META = {"display_text": f"Kanban {TASK} done", "task_ids": [TASK]}
OK = {"final_response": "handled"}


def _closing_agent(results, lines):
    """A fake agent whose failed turns close their durable tail the way ``_close_durable_failed_turn`` does: the
    host's copy override when one is installed, else the user-turn boundary."""
    from agent.turn_failure_copy import FAILED_TURN_NOTICE
    agent = types.SimpleNamespace(session_id="session-key", clear_interrupt=lambda: None)
    pending = iter(results)

    def run(message, **kwargs):
        result = dict(next(pending))
        if result.get("failed"):
            override = getattr(agent, "_failed_turn_notice_override", None)
            lines.append((override(result) if callable(override) else None) or FAILED_TURN_NOTICE)
        return result
    agent.run_conversation = run
    return agent


def _wake_into(session, calls, monkeypatch):
    """``_wake_session_turn`` stand-in: records the replay and runs it as a turn in ``session``."""
    def wake(key, text, *, display_kind=None, display_metadata=None, label="wake", build_timeout=120.0,
             profiles=None):
        calls.append({"key": key, "text": text, "display_kind": display_kind, "meta": display_metadata,
                      "label": label})
        session["running"] = True
        server._run_prompt_submit("rid", "sid", session, text, display_kind=display_kind,
                                  display_metadata=display_metadata)
        return "started"
    monkeypatch.setattr(server, "_wake_session_turn", wake)


def _deliver(session, kind, text="card done"):
    session["running"] = True
    server._run_prompt_submit("rid", "sid", session, text, display_kind=kind, display_metadata=dict(META))


def test_successful_prompt_uses_its_context_start_for_replay_cancellation(
        emits, turn_env, marker_home, timers, replay, monkeypatch):
    home = marker_home / "profiles" / "owner"
    foreign = marker_home / "profiles" / "foreign"
    clock = {"now": 100.0}
    monkeypatch.setattr(replay.time, "time", lambda: clock["now"])
    monkeypatch.setattr(replay, "stale_code_reason", lambda: None)

    def schedule(text, *, persisted=True, now=90, profile=home):
        replay.schedule("session-key", text=text, display_kind="kanban_notice", display_metadata={},
                        due=1000, cause="quota", persisted=persisted, now=now, profile_home=profile)

    schedule("carried")
    schedule("unpersisted", persisted=False)
    schedule("foreign", profile=foreign)

    def run(message, **kwargs):
        schedule("joined after context", now=110)
        clock["now"] = 120.0
        return dict(OK)

    session = _session(agent=_agent(run), running=True, profile_home=str(home))
    assert server._run_prompt_submit("rid", "sid", session, "continue")
    terminal = [payload for event, _, payload in emits if event == "message.complete"][-1]
    assert terminal["status"] == "complete"
    assert [i.text for i in replay._entries[(str(home), "session-key")].items] == [
        "unpersisted", "joined after context"]
    assert [i.text for i in replay._entries[(str(foreign), "session-key")].items] == ["foreign"]


@pytest.mark.parametrize("kind", KINDS)
def test_refused_automation_turn_is_replayed_once_after_the_delay(
        emits, turn_env, marker_home, timers, replay, monkeypatch, kind):
    lines, calls = [], []
    session = _session(agent=_closing_agent([_refused("x"), OK], lines), running=True)
    _wake_into(session, calls, monkeypatch)
    _deliver(session, kind)
    assert len(timers) == 1 and timers[0][0] >= 60.0
    assert "Send it again" not in lines[0] and "Retrying once at" in lines[0] and LEASE in lines[0]
    assert read_turn_marker(marker_home, "session-key") is None  # the user-turn replay stays out of it
    timers[0][1]()
    assert len(calls) == 1
    call = calls[0]
    assert call["key"] == "session-key" and call["display_kind"] == kind and call["label"] == "automation-replay"
    assert "card done" in call["text"] and call["meta"]["automation_replay"]["attempt"] == 1
    timers[0][1]()  # a stale second fire finds nothing
    assert len(calls) == 1 and len(timers) == 1


@pytest.mark.parametrize("kind", KINDS)
def test_a_second_refusal_gives_up_and_leaves_no_timers(
        emits, turn_env, marker_home, timers, replay, monkeypatch, kind):
    lines, calls = [], []
    session = _session(agent=_closing_agent([_refused("x"), _refused("x")], lines), running=True)
    _wake_into(session, calls, monkeypatch)
    _deliver(session, kind)
    timers[0][1]()
    assert len(calls) == 1 and len(lines) == 2
    assert "Gave up after one retry" in lines[1] and "Send it again" not in lines[1]
    assert len(timers) == 1 and not replay.pending("session-key")


@pytest.mark.parametrize("kind", KINDS)
def test_a_later_successful_turn_cancels_the_pending_replay(
        emits, turn_env, marker_home, timers, replay, monkeypatch, kind):
    lines, calls = [], []
    session = _session(agent=_closing_agent([_refused("x"), OK], lines), running=True)
    _wake_into(session, calls, monkeypatch)
    _deliver(session, kind)
    assert len(timers) == 1
    session["running"] = True
    server._run_prompt_submit("rid", "sid", session, "what's next?")  # a person's turn; it carries the notice
    assert not replay.pending("session-key")
    timers[0][1]()
    assert not calls


def test_repeated_refusals_never_loop(emits, turn_env, marker_home, timers, replay, monkeypatch):
    lines, calls = [], []
    session = _session(agent=_closing_agent([_refused("x")] * 8, lines), running=True)
    _wake_into(session, calls, monkeypatch)
    for n, kind in enumerate(KINDS + KINDS):  # four deliveries fail: they share one replay
        _deliver(session, kind, text=f"delivery {n}")
    assert len(timers) == 1
    for _ in range(3):
        timers[-1][1]()
    assert len(calls) == 1 and all(f"delivery {n}" in calls[0]["text"] for n in range(4))
    assert len(timers) == 1 and not replay.pending("session-key")
    assert "Gave up after one retry" in lines[-1]


def test_a_quota_failure_replays_at_the_recorded_reset(emits, turn_env, marker_home, timers, replay, monkeypatch):
    import time
    resets_at = time.time() + 7200
    lines = []
    quota = {"failed": True, "error": "Subscription quota unavailable", "final_response": None,
             "failure_reason": "rate_limit", "failure_retryable": True, "failure_resets_at": resets_at}
    session = _session(agent=_closing_agent([quota], lines), running=True)
    _deliver(session, "kanban_notice")
    assert len(timers) == 1 and 7100 < timers[0][0] <= 7200
    assert "Retrying once at" in lines[0] and "Send it again" not in lines[0]


def test_a_user_turn_keeps_its_own_replay(emits, turn_env, marker_home, timers, replay):
    session = _session(agent=_agent(), running=True)
    server._run_prompt_submit("rid", "sid", session, "archive my receipts")
    assert [t[0] for t in timers] == [45.0] and not replay.pending("session-key")


def _failed_build_session(monkeypatch, failure=None):
    session = _session(agent=None, running=False)
    session.update(agent=None, agent_error="ModelRoutingError: No eligible deep routes under billing/capability "
                   "policy", agent_failure=failure or {}, agent_ready=threading.Event())
    session["agent_ready"].set()
    monkeypatch.setattr(server, "_find_live_session_by_key", lambda key: ("sid", session))
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, sess: None)
    monkeypatch.setattr(server, "_restart_completed_failed_agent_build", lambda *a: False)
    return session


@pytest.mark.parametrize("label", ["owner-wake", "kanban-sweep-wake"])
def test_a_wake_whose_build_failed_gets_exactly_one_later_retry(timers, replay, monkeypatch, label):
    _failed_build_session(monkeypatch)
    real, calls = server._wake_session_turn, []
    monkeypatch.setattr(server, "_wake_session_turn", lambda *a, **k: calls.append((a, k)) or real(*a, **k))
    verdict = real("session-key", f"Owner wake: {TASK} waits on you", display_kind="kanban_notice",
                   display_metadata={"owner_wake": True, "task_ids": [TASK]}, label=label)
    assert verdict == "error"  # the caller's contract is unchanged ("deferred (error)")
    assert len(timers) == 1 and timers[0][0] >= 60.0
    timers[0][1]()
    assert len(calls) == 1 and calls[0][1]["label"] == "automation-replay"
    assert calls[0][1]["display_metadata"]["automation_replay"]["attempt"] == 1
    assert len(timers) == 1 and not replay.pending("session-key")  # the replay failed too: given up


def test_a_wake_build_quota_failure_waits_for_the_reset(timers, replay, monkeypatch):
    import time
    _failed_build_session(monkeypatch, failure={"failure_resets_at": time.time() + 3600})
    server._wake_session_turn("session-key", f"Owner wake: {TASK}", display_kind="kanban_notice",
                              display_metadata={"owner_wake": True}, label="owner-wake")
    assert len(timers) == 1 and 3500 < timers[0][0] <= 3600


def test_a_closeout_wake_whose_build_failed_gets_its_one_retry(timers, replay, monkeypatch):
    # notice_guarantee stamps the claimed row "error" and never offers it again: the replay is its only retry.
    _failed_build_session(monkeypatch)
    real, calls = server._wake_session_turn, []
    monkeypatch.setattr(server, "_wake_session_turn", lambda *a, **k: calls.append((a, k)) or real(*a, **k))
    closeout = {"display_text": "Close-out turn", "closeout": {"state": "self", "of": 7}}
    assert real("session-key", "close out", display_kind="kanban_notice", display_metadata=closeout,
                label="notice-closeout") == "error"
    assert len(timers) == 1 and timers[0][0] >= 60.0
    timers[0][1]()
    meta = calls[0][1]["display_metadata"]
    assert len(calls) == 1 and meta["closeout"] == {"state": "self", "of": 7} and meta["automation_replay"]
    assert len(timers) == 1 and not replay.pending("session-key")


def test_a_wake_retries_a_build_that_already_failed(timers, replay, monkeypatch):
    _failed_build_session(monkeypatch)
    restarted = []
    monkeypatch.setattr(server, "_restart_completed_failed_agent_build", lambda *a: restarted.append(a) or False)
    server._wake_session_turn("session-key", "notice", display_kind="kanban_notice", label="owner-wake")
    assert restarted and restarted[0][0] == "sid"


def test_a_goal_keeps_its_own_retry(emits, turn_env, marker_home, timers, monkeypatch):
    monkeypatch.setattr(server, "_active_goal_manager", lambda session: object())
    session = _session(agent=_agent(), running=True)
    server._run_prompt_submit("rid", "sid", session, "archive my receipts")
    assert read_turn_marker(marker_home, "session-key") is None and not timers


def test_a_second_refused_message_joins_the_first(emits, turn_env, marker_home, timers):
    session = _session(agent=_agent(), running=True)
    server._run_prompt_submit("rid", "sid", session, "archive waymo")
    session["running"] = True
    server._run_prompt_submit("rid", "sid", session, "and mercury")
    assert read_turn_marker(marker_home, "session-key")["prompt"] == "archive waymo\n\nand mercury"


def test_a_new_message_that_works_ends_the_replay(emits, turn_env, marker_home, timers, monkeypatch):
    calls = []
    results = iter([_refused("x"), {"final_response": "done"}])
    session = _session(agent=_agent(lambda message, **kw: next(results)), running=True)
    server._run_prompt_submit("rid", "sid", session, "archive waymo")
    session["running"] = True
    server._run_prompt_submit("rid", "sid", session, "something else")
    assert read_turn_marker(marker_home, "session-key") is None
    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", lambda *a: calls.append(a))
    monkeypatch.setattr("tui_gateway.update_pause._detect_skew", lambda: None)
    monkeypatch.setattr("tui_gateway.turn_outcome.refresh_turn_outcome", lambda *a, **kw: None)
    timers[0][1]()
    assert len(calls) == 1  # it looks, and the guard finds no marker


def _armed(marker_home, timers, monkeypatch):
    session = _session(agent=_agent(), running=False, _refused_turn_delay=45.0)
    record_turn_start(marker_home, "session-key", "archive waymo")
    server._schedule_refused_turn_retry("sid", session, "session-key")
    return session, timers[-1][1]


def test_the_wait_replays_through_auto_continue(emits, turn_env, marker_home, timers, monkeypatch):
    seen = []
    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", lambda *a: seen.append(a) or {"attempt": 1})
    monkeypatch.setattr("tui_gateway.update_pause._detect_skew", lambda: None)
    session, fire = _armed(marker_home, timers, monkeypatch)
    fire()
    assert seen == [("sid", session, "session-key")]


def test_a_skewed_backend_leaves_the_marker_for_the_boot_sweep(emits, turn_env, marker_home, timers, monkeypatch):
    seen = []
    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", lambda *a: seen.append(a))
    monkeypatch.setattr("tui_gateway.update_pause._detect_skew", lambda: "skew")
    _, fire = _armed(marker_home, timers, monkeypatch)
    fire()
    assert not seen and read_turn_marker(marker_home, "session-key")["prompt"] == "archive waymo"


def test_a_marker_that_aged_out_settles_the_outcome(emits, turn_env, marker_home, timers, monkeypatch):
    refreshed = []
    monkeypatch.setattr("tui_gateway.update_pause._detect_skew", lambda: None)
    monkeypatch.setattr("tui_gateway.turn_outcome.refresh_turn_outcome", lambda *a, **kw: refreshed.append(a))
    _, fire = _armed(marker_home, timers, monkeypatch)

    def aged_out(sid, sess, key):
        server.clear_turn_marker(marker_home, key)

    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", aged_out)
    fire()
    assert len(refreshed) == 1
