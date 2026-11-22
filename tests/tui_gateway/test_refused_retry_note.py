"""A live retryable refusal is not evidence that the backend crashed."""

import pytest

from tui_gateway import server
from tui_gateway.turn_marker import record_turn_start
from tests.tui_gateway import test_refused_turn_retry as helpers
from tests.tui_gateway.test_auto_continue import _session

emits = helpers.emits
turn_env = helpers.turn_env
marker_home = helpers.marker_home
timers = helpers.timers


@pytest.mark.parametrize("refused", [True, False])
def test_live_refusal_retry_and_crash_replay_have_distinct_notes(
        emits, turn_env, marker_home, timers, monkeypatch, refused):
    session = _session(agent=helpers._agent(), _refused_turn_delay=45.0)
    record_turn_start(marker_home, "session-key", "archive receipts")
    seen = []
    monkeypatch.setattr("tui_gateway.update_pause._detect_skew", lambda: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a: None)
    monkeypatch.setattr(server, "_wait_agent", lambda *a, **k: None)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a, **k: None)
    monkeypatch.setattr(server, "_adopt_stored_continuation_source", lambda *a: None)
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **k: seen.append((a[3], k)))
    if refused:
        server._schedule_refused_turn_retry("sid", session, "session-key")
        timers[-1][1]()
    else:
        server._maybe_schedule_auto_continue("sid", session, "session-key")
    text, kwargs = seen[0]
    assert text.startswith("[System note: Your previous turn was interrupted mid-run")
    assert text.endswith("archive receipts")
    assert kwargs["display_kind"] == "auto_continue"
    assert session["_auto_continue_attempt"] == 1
    assert ("backend process stopped" in text) is not refused
    assert ("the provider refused the turn with a retryable error" in text) is refused
    assert ("the backend did not stop" in text) is refused

