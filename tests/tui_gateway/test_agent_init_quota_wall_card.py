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


def test_turn_text_for_an_own_sentence_code_is_that_sentence():
    from tui_gateway.user_messages import turn_error_text
    surface = {"layer": "gateway", "code": "route_refused", "retryable": False, "message": HELD}
    assert turn_error_text("raw detail", surface) == HELD
    # Without the sentence it keeps the title/detail/hint shape, with route_refused's own title.
    assert turn_error_text("raw detail", {**surface, "message": ""}).startswith("This chat's model is unavailable.")


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


def test_a_new_build_clears_an_earlier_builds_route(tmp_path, monkeypatch):
    """A route recorded by an earlier failed build never labels a later build's failure (here: abandoned)."""
    from tests.tui_gateway.test_provider_not_configured_code import _fail_build, _run_build
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    session = _fail_build(monkeypatch, RuntimeError("unused"))
    monkeypatch.setattr(server, "_await_resume_history", lambda *a, **kw: False)
    monkeypatch.setattr(server, "write_json", lambda frame: True)
    session["agent_failure"] = {"failure_resets_at": RESET,
                                "failure_route": {"provider": "anthropic", "model": "claude-fable-5-2", "resets_at": RESET}}
    _run_build(session)
    assert session["agent_error"] == server.AGENT_BUILD_ABANDONED
    assert "failure_route" not in session["agent_failure"]


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
    expected = {"layer": "gateway", "code": "route_refused", "retryable": False, "message": HELD,
                "provider": "anthropic", "model": "claude-fable-5-2"}

    (payload,) = complete
    assert payload["status"] == "error"
    assert expected.items() <= payload["error_surface"].items()
    assert payload["error"] == HELD
    assert payload["text"] == HELD
    assert "failure_route" not in payload and "failure_resets_at" not in payload
    assert payload["reset_at"] == RESET

    assert [(r["role"], r["content"]) for r in rows] == [("user", "whats left"), ("assistant", ROUTE_REFUSED_TURN_NOTICE)]
    boundary = rows[-1]
    assert boundary["display_kind"] == FAILED_TURN_DISPLAY_KIND
    assert "Send it again" not in boundary["content"]
    assert _metadata(boundary)["error_surface"] == payload["error_surface"]


def test_malformed_route_record_keeps_the_generic_card(monkeypatch, tmp_path):
    complete, _rows = _turn_on_failed_build(
        monkeypatch, tmp_path, {"failure_route": {"provider": "anthropic"}}, str(_wall()))
    (payload,) = complete
    assert payload["error_surface"] == {"layer": "runtime", "code": "agent_init_failed", "retryable": True}


def test_control_other_build_failure_keeps_agent_init_failed(monkeypatch, tmp_path):
    from agent.turn_failure_copy import FAILED_TURN_NOTICE
    complete, rows = _turn_on_failed_build(monkeypatch, tmp_path, {}, "the session database is locked")
    (payload,) = complete
    assert payload["error_surface"] == {"layer": "runtime", "code": "agent_init_failed", "retryable": True}
    assert [(r["role"], r["content"]) for r in rows] == [("user", "whats left"), ("assistant", FAILED_TURN_NOTICE)]


# --- an all-walled fallback chain, raised through the real walker ------------------------------------------------
# tui_gateway/agent_factory.py ``_resolve_runtime_with_fallback`` builds a ProviderFallbackError only after the pin
# failed with an AuthError: the qualifying pin is a rate-limited Codex AuthError with a wait hint.

NOW = RESET - 3600  # Fri 11-20 14:19 PT: a 3600 s Codex wait ends at RESET (15:19 PT)
LUNA = 1795219245.0  # Fri 11-20 16:00 PT
CHAIN = ("⚠️ openai-codex/gpt-6-luna is rate-limited until Fri 11-20 15:19 PT, and its fallbacks are out too "
         "(anthropic/claude-fable-5-2 until Fri 11-20 16:00 PT). This chat is pinned to it; use /model to switch, "
         "or resend after Fri 11-20 15:19 PT.")


@pytest.fixture
def frozen_now(monkeypatch):
    import time
    monkeypatch.setattr(time, "time", lambda: NOW)


def _codex_429(retry_after=3600.0):
    from hermes_cli.auth import AuthError
    from hermes_cli.auth_constants import CODEX_RATE_LIMITED_CODE
    return AuthError("Codex provider quota exhausted (429). Credentials are still valid; retry after the usage "
                     "limit resets.", provider="openai-codex", code=CODEX_RATE_LIMITED_CODE,
                     relogin_required=False, retry_after=retry_after)


def _fable(reset=LUNA):
    return ("anthropic", "claude-fable-5-2", SubscriptionQuotaUnavailable("anthropic", "claude-fable-5-2", reset))


def _real_chain(monkeypatch, pin_error, *fallbacks, target_model="gpt-6-luna"):
    """The ProviderFallbackError the TUI build's own walker raises for this pin failure and these fallbacks."""
    from hermes_cli.fallback_errors import ProviderFallbackError
    pending = iter(error for _, _, error in fallbacks)

    def resolve(**kwargs):
        if kwargs.get("requested") == "openai-codex":
            raise pin_error
        raise next(pending)

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", resolve)
    monkeypatch.setattr(server, "_load_fallback_model", lambda: [{"provider": p, "model": m} for p, m, _ in fallbacks])
    with pytest.raises(ProviderFallbackError) as raised:
        server._resolve_runtime_with_fallback({"requested": "openai-codex", "target_model": target_model})
    return raised.value


def test_a_rate_limited_pin_with_walled_fallbacks_names_the_pin_and_the_earliest_reset(monkeypatch, frozen_now):
    from tui_gateway.user_messages import agent_init_failed_message
    assert agent_init_failed_message(_real_chain(monkeypatch, _codex_429(), _fable())) == CHAIN


def test_the_chain_resends_after_a_fallback_that_resets_first(monkeypatch, frozen_now):
    from tui_gateway.user_messages import agent_init_failed_message
    message = agent_init_failed_message(_real_chain(monkeypatch, _codex_429(), _fable(RESET - 1800)))
    assert message.startswith("⚠️ openai-codex/gpt-6-luna is rate-limited until Fri 11-20 15:19 PT,")
    assert "(anthropic/claude-fable-5-2 until Fri 11-20 14:49 PT)" in message
    assert message.endswith("resend after Fri 11-20 14:49 PT.")


def test_a_model_less_pin_takes_the_requested_model_or_is_named_by_provider_alone(monkeypatch, frozen_now):
    from tui_gateway.user_messages import agent_init_failed_message
    chain = _real_chain(monkeypatch, _codex_429(), _fable(), target_model=None)
    assert agent_init_failed_message(chain, "gpt-6-luna") == CHAIN
    assert agent_init_failed_message(chain).startswith("⚠️ openai-codex is rate-limited until Fri 11-20 15:19 PT,")


def test_a_fallback_that_repeats_a_route_is_listed_once_with_its_earlier_reset(monkeypatch, frozen_now):
    from tui_gateway.user_messages import agent_init_failed_message
    message = agent_init_failed_message(_real_chain(monkeypatch, _codex_429(), _fable(), _fable(RESET - 1800)))
    assert message.count("anthropic/claude-fable-5-2") == 1
    assert "(anthropic/claude-fable-5-2 until Fri 11-20 14:49 PT)" in message


@pytest.mark.parametrize("pin_error, fallback", [
    (None, _fable()),  # mixed: the pin's AuthError is a missing credential, not a rate limit
    ("no-wait", _fable()),  # a rate-limited pin with no wait hint
    ("429", _fable(float("nan"))),  # a walled fallback with no reset
    ("429", ("deepseek", "deepseek-v4-pro", RuntimeError("deepseek is down"))),  # a fallback that is not a quota
], ids=["mixed-pin", "no-wait-pin", "no-reset-fallback", "non-quota-fallback"])
def test_a_chain_that_is_not_all_walled_keeps_the_generic_message(monkeypatch, frozen_now, pin_error, fallback):
    """The pin's own error is never hidden behind a wall that does not cover the whole chain."""
    from hermes_cli.auth import AuthError
    from tui_gateway.user_messages import agent_init_failed_message
    pin = {None: AuthError("No Codex credentials stored", provider="openai-codex"),
           "no-wait": _codex_429(retry_after=None), "429": _codex_429()}[pin_error]
    message = agent_init_failed_message(_real_chain(monkeypatch, pin, fallback))
    assert message.startswith("Hermes could not start the assistant for this session. Details: ")
    assert "pinned to it" not in message


def test_a_chain_nested_in_a_chain_keeps_the_generic_message(monkeypatch, frozen_now):
    from hermes_cli.fallback_errors import ProviderFallbackError
    from tui_gateway.user_messages import agent_init_failed_message
    inner = _real_chain(monkeypatch, _codex_429(), _fable())
    outer = ProviderFallbackError([("openai-codex", "gpt-6-luna", inner), _fable()])
    assert agent_init_failed_message(outer).startswith("Hermes could not start the assistant")


def _chain_build(monkeypatch, tmp_path):
    """The deferred build failing on the real walker's chain: the session record and the client error events."""
    from tests.tui_gateway.test_credential_failure import _strict_build
    from tui_gateway.contracts import registry
    assert registry.STRICT  # ErrorPayload forbids unknown keys: a leaked failure_route raises here
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    chain = _real_chain(monkeypatch, _codex_429(), _fable(RESET - 1800))
    session, payloads, crashes = _strict_build(monkeypatch, chain)
    assert crashes == []
    return session, payloads


def test_the_chain_records_the_pin_and_earliest_reset_and_never_sends_them(tmp_path, monkeypatch, frozen_now):
    session, payloads = _chain_build(monkeypatch, tmp_path)
    route = session["agent_failure"]["failure_route"]
    assert (route["provider"], route["model"], route["resets_at"]) == ("openai-codex", "gpt-6-luna", RESET - 1800)
    assert [(w["model"], w["kind"]) for w in route["walls"]] == [("gpt-6-luna", "rate_limit"),
                                                                 ("claude-fable-5-2", "quota")]
    assert session["agent_failure"]["failure_resets_at"] == RESET - 1800  # replay waits for the first route back
    (payload,) = payloads
    assert payload["message"].endswith("resend after Fri 11-20 14:49 PT.")
    assert "failure_route" not in payload and "failure_resets_at" not in payload
    assert payload["reset_at"] == RESET - 1800
    data = server._wait_agent(session, "r")["error"]["data"] or {}
    assert "failure_route" not in data and "failure_resets_at" not in data


def test_the_chain_turn_closes_with_the_route_refused_card_naming_the_pin(tmp_path, monkeypatch, frozen_now):
    from agent.turn_failure_copy import ROUTE_REFUSED_TURN_NOTICE
    session, _payloads = _chain_build(monkeypatch, tmp_path / "build")
    (tmp_path / "turn").mkdir()
    complete, rows = _turn_on_failed_build(
        monkeypatch, tmp_path / "turn", session["agent_failure"], session["agent_error"])
    (payload,) = complete
    surface = payload["error_surface"]
    assert {"layer": "gateway", "code": "route_refused", "retryable": False, "provider": "openai-codex",
            "model": "gpt-6-luna"}.items() <= surface.items()
    assert surface["message"] == payload["text"] == payload["error"]
    assert surface["message"].startswith("⚠️ openai-codex/gpt-6-luna is rate-limited until Fri 11-20 15:19 PT,")
    assert "failure_route" not in payload
    assert [(r["role"], r["content"]) for r in rows] == [("user", "whats left"), ("assistant", ROUTE_REFUSED_TURN_NOTICE)]
    assert _metadata(rows[-1])["error_surface"] == surface


def test_a_wrapped_single_wall_does_not_fill_the_replay_clock(tmp_path, monkeypatch):
    """Only a chain fills ``failure_resets_at`` from its walls; a single wall's clock is the error's own."""
    from tests.tui_gateway.test_credential_failure import _strict_build
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    try:
        raise RuntimeError("agent build failed") from _wall()
    except RuntimeError as wrapped:
        session, _payloads, crashes = _strict_build(monkeypatch, wrapped)
    assert crashes == []
    assert session["agent_failure"]["failure_route"]["resets_at"] == RESET
    assert "failure_resets_at" not in session["agent_failure"]



def test_a_model_less_chain_pin_is_named_from_the_sessions_requested_model(tmp_path, monkeypatch, frozen_now):
    import threading
    from tests.tui_gateway.test_provider_not_configured_code import _fail_build, _run_build
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(server, "_resolve_startup_runtime", lambda: ("m", "openai-codex"))
    monkeypatch.setattr(server, "_load_cfg", dict)
    frames, crashes = [], []
    monkeypatch.setattr(server, "write_json", lambda frame: frames.append(frame) or True)
    monkeypatch.setattr(threading, "excepthook", lambda args: crashes.append(args.exc_value))
    chain = _real_chain(monkeypatch, _codex_429(), _fable(), target_model=None)
    session = _fail_build(monkeypatch, chain)
    session["model_override"] = {"provider": "openai-codex", "model": "gpt-6-luna"}
    _run_build(session)
    assert crashes == []
    assert session["agent_failure"]["failure_route"]["model"] == "gpt-6-luna"
    (error,) = [f["params"]["payload"] for f in frames if f["params"]["type"] == "error"]
    assert error["message"] == CHAIN
