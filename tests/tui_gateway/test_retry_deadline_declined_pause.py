"""Repro (review only): a declined boundary pause after the durable API-error wait yields -> early provider call?"""
import threading, time
import httpx, pytest
from openai import RateLimitError
from agent import provider_retry_deadline, turn_api_error
from hermes_cli import admission_fence as af
from tui_gateway import update_pause as up
from tui_gateway.turn_marker import read_turn_marker, record_turn_start
from tests.tui_gateway import test_update_pause_model_request as helpers

home = helpers.home
_quiet = helpers._quiet
WAIT = 6.0

@pytest.mark.parametrize("mode", ["decline_hook", "opt_out", "jobs_stop_fail"])
def test_declined_pause_keeps_deadline(home, tmp_path, monkeypatch, mode):
    agent = helpers._make_agent(tmp_path)
    if mode == "jobs_stop_fail":
        monkeypatch.setattr(up, "stop_owned_jobs_for_update_pause", lambda killed, save: False)
    if mode == "opt_out":
        monkeypatch.setattr(helpers.server, "_load_cfg", lambda: {"desktop": {"auto_continue": {"enabled": False}}})
    record_turn_start(home, "session-key", "run the quick job")
    session = {"agent": agent, "session_key": "session-key", "profile_home": str(home), "history_lock": threading.Lock()}
    stamp, clear = up.provider_retry_hooks(session, "session-key", read_turn_marker(home, "session-key"))
    provider_retry_deadline.install(agent, stamp=stamp, clear=clear, restored=None)
    entered, issued, calls = threading.Event(), threading.Event(), []
    err = RateLimitError("rl", response=httpx.Response(429, request=httpx.Request("POST", "https://x/c")), body=None)
    def provider(*a, **k):
        calls.append(time.time())
        if len(calls) == 1:
            return helpers._response(helpers._call("call_1"))
        if len(calls) == 2:
            raise err
        return helpers._response()
    real_sleep = turn_api_error.interruptible_backoff_sleep
    def observed(*a, **k):
        entered.set(); assert issued.wait(5); return real_sleep(*a, **k)
    def pause():
        if entered.wait(5):
            af.set_hold(home); af.request_pause(home, expires_at=time.time() + 120); issued.set()
    agent._interruptible_api_call = provider
    monkeypatch.setattr("model_tools.handle_function_call", lambda *a, **k: "quick result")
    monkeypatch.setattr(turn_api_error, "compute_error_backoff", lambda *a, **k: WAIT)
    monkeypatch.setattr(turn_api_error, "interruptible_backoff_sleep", observed)
    reservation = af.reserve(home)
    th = threading.Thread(target=pause); th.start()
    real_hook = up.boundary_pause_hook(session, "session-key")
    agent.step_boundary_pause = (lambda: None) if mode == "decline_hook" else real_hook
    stop = up.start_mid_tool_cancel(session, "session-key", agent)
    try:
        result = agent.run_conversation("run the quick job")
    finally:
        stop(); th.join(6); reservation.close()
    print(mode, "paused=", result.get("paused_for_update"), "ncalls=", len(calls),
          "gap=", round(calls[2] - calls[1], 2) if len(calls) > 2 else None,
          "marker_deadline_left=", (read_turn_marker(home, "session-key") or {}).get("provider_retry_not_before", 0) - calls[-1])
    assert not result.get("paused_for_update")
    assert len(calls) == 3
    assert calls[2] - calls[1] >= WAIT - 0.2, f"{mode}: provider called {calls[2]-calls[1]:.2f}s after a {WAIT}s retry wait"
