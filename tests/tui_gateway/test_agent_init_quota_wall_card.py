"""A chat pinned to a quota-walled subscription route whose agent BUILD hit the wall reads like the pre-build refusal.

2026-11-20 (Desktop, anthropic/claude-fable-5-2): the build raised ``SubscriptionQuotaUnavailable`` and the user got a
toast with the raw epoch reset and `hermes setup` advice, a "Couldn't start this chat ... Retry" card and a "Send it
again" boundary. The pre-build refusal of the same pin (``agent.model_routing_walls.held_reply``) already says the reset
in PT and /model; the build-time path now says the same, as a non-retryable ``route_refused`` card."""
import json

import pytest

from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
from tui_gateway import server

RESET = 1795216785.57385  # Fri 11-20 15:19 PT
HELD = ("⚠️ anthropic/claude-fable-5-2 subscription quota is out until Fri 11-20 15:19 PT. "
        "This chat is pinned to it; use /model to switch, or resend after the reset.")


@pytest.fixture(autouse=True)
def isolate_recovery(monkeypatch):
    # This worktree's gitdir belongs to the live installation; imports must not probe it.
    monkeypatch.setattr("hermes_cli._early_recovery.restore_interrupted_pull", lambda *a, **kw: False)


def _wall():
    return SubscriptionQuotaUnavailable("anthropic", "claude-fable-5-2", RESET)


# --- (a) one sentence for the pre-build and build-time refusal --------------------------------------------------

def test_held_reply_is_the_walled_pin_reply():
    from agent.model_routing_walls import held_reply, walled_pin_reply
    assert walled_pin_reply("anthropic", "claude-fable-5-2", RESET) == HELD
    assert held_reply(_wall()) == HELD


# --- (b) the build-failure toast ---------------------------------------------------------------------------------

def test_walled_build_message_names_the_reset_in_pt_and_model_not_an_epoch_or_setup():
    from tui_gateway.user_messages import agent_init_failed_message
    message = agent_init_failed_message(_wall())
    assert message == HELD
    assert "1795216785" not in message and "hermes setup" not in message


def test_walled_build_behind_a_fallback_chain_names_the_terminal_route():
    from hermes_cli.fallback_errors import ProviderFallbackError
    from tui_gateway.user_messages import agent_init_failed_message
    chain = ProviderFallbackError([("anthropic", "claude-fable-5-2", _wall())])
    assert agent_init_failed_message(chain) == HELD


@pytest.mark.parametrize("exc", [
    RuntimeError("the session database is locked"),
    SubscriptionQuotaUnavailable("anthropic", "claude-fable-5-2", float("nan")),  # no usable reset
], ids=["unrelated", "quota-without-reset"])
def test_other_build_failures_keep_the_generic_message(exc):
    from tui_gateway.user_messages import agent_init_failed_message
    message = agent_init_failed_message(exc)
    assert message.startswith("Hermes could not start the assistant for this session. Details: ")
    assert "hermes setup" in message


# --- (c) the route is recorded server-side and never reaches a client ---------------------------------------------

def test_walled_build_records_its_route_and_never_sends_it(tmp_path, monkeypatch):
    from tests.tui_gateway.test_credential_failure import _strict_build
    from tui_gateway.contracts import registry
    assert registry.STRICT  # ErrorPayload forbids unknown keys: a leaked failure_route raises here
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    session, payloads, crashes = _strict_build(monkeypatch, _wall())
    assert crashes == []
    assert session["agent_failure"]["failure_route"] == {
        "provider": "anthropic", "model": "claude-fable-5-2", "resets_at": RESET}
    (payload,) = payloads
    assert payload["message"] == HELD
    assert "failure_route" not in payload and "failure_resets_at" not in payload
    assert payload["reset_at"] == RESET
    data = server._wait_agent(session, "r")["error"]["data"] or {}
    assert "failure_route" not in data and "failure_resets_at" not in data


def test_unrelated_build_failure_records_no_route(tmp_path, monkeypatch):
    from tests.tui_gateway.test_credential_failure import _strict_build
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    session, _payloads, crashes = _strict_build(monkeypatch, RuntimeError("the session database is locked"))
    assert crashes == []
    assert "failure_route" not in (session.get("agent_failure") or {})


# --- (d) the turn waiting on the build closes as a route refusal -------------------------------------------------

def _turn_on_failed_build(monkeypatch, tmp_path, agent_failure, error_message):
    """Submit a turn whose deferred build failed; return the client frames and the durable transcript rows."""
    from tests.tui_gateway.test_submit_time_user_row import _desktop_session
    from hermes_state import SessionDB
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key = _desktop_session(monkeypatch, db)
    session = server._sessions[sid]
    frames = []
    monkeypatch.setattr(server, "write_json", lambda frame: frames.append(frame) or True)
    monkeypatch.setattr(server, "_wait_agent_for_prompt",
                        lambda _session, _rid, _sid: {"error": {"message": error_message}})
    try:
        session["agent_failure"] = agent_failure
        with session["history_lock"]:
            session["running"] = True
            server._start_inflight_turn(session, "whats left")
        assert server._persist_session_row_for_submit("rid", session, "whats left", None) is None
        server._run_after_agent_ready("rid", sid, session, "whats left", None, None, None)
        rows = db.get_messages_as_conversation(key, include_inactive=True)
    finally:
        server._sessions.pop(sid, None)
        db.close()
    complete = [f["params"]["payload"] for f in frames
                if f.get("params", {}).get("type") == "message.complete"]
    return complete, rows


def _metadata(row):
    meta = row.get("display_metadata")
    return json.loads(meta) if isinstance(meta, str) else (meta or {})


def test_walled_build_turn_closes_with_the_route_refused_card_and_no_send_it_again(monkeypatch, tmp_path):
    from agent.turn_failure_copy import FAILED_TURN_DISPLAY_KIND, ROUTE_REFUSED_TURN_NOTICE
    failure = {"failure_resets_at": RESET,
               "failure_route": {"provider": "anthropic", "model": "claude-fable-5-2", "resets_at": RESET}}
    complete, rows = _turn_on_failed_build(monkeypatch, tmp_path, failure, str(_wall()))
    expected = {"layer": "gateway", "code": "route_refused", "retryable": False, "message": HELD}

    (payload,) = complete
    assert payload["status"] == "error"
    assert payload["error_surface"] == expected
    assert payload["error"] == HELD
    assert "1795216785" not in payload["text"] and "/model" in payload["text"]
    assert "failure_route" not in payload and "failure_resets_at" not in payload
    assert payload["reset_at"] == RESET

    assert [(r["role"], r["content"]) for r in rows] == [("user", "whats left"), ("assistant", ROUTE_REFUSED_TURN_NOTICE)]
    boundary = rows[-1]
    assert boundary["display_kind"] == FAILED_TURN_DISPLAY_KIND
    assert "Send it again" not in boundary["content"]
    assert _metadata(boundary)["error_surface"] == expected


def test_control_other_build_failure_keeps_agent_init_failed(monkeypatch, tmp_path):
    from agent.turn_failure_copy import FAILED_TURN_NOTICE
    complete, rows = _turn_on_failed_build(monkeypatch, tmp_path, {}, "the session database is locked")
    (payload,) = complete
    assert payload["error_surface"] == {"layer": "runtime", "code": "agent_init_failed", "retryable": True}
    assert [(r["role"], r["content"]) for r in rows] == [("user", "whats left"), ("assistant", FAILED_TURN_NOTICE)]
