"""The fork's nested rebind must restore upstream's live actor event and cwd together."""
from types import SimpleNamespace

from agent.runtime_cwd import scoped_session_cwd
from gateway.session_context import clear_session_vars, get_session_env, reset_session_vars, set_session_vars


def test_nested_rebind_restores_live_actor_event_and_cwd():
    reset_session_vars()
    event = SimpleNamespace(user_id="requester")
    outer = set_session_vars(session_key="same", user_id="requester", cwd="/outer", turn_event=event)
    try:
        inner = set_session_vars(session_key="same", user_id="helper", cwd="/inner")
        try:
            assert scoped_session_cwd() == "/inner"
            assert get_session_env("HERMES_SESSION_ACTOR_ID") == "helper"
        finally:
            clear_session_vars(inner)
        assert scoped_session_cwd() == "/outer"
        assert get_session_env("HERMES_SESSION_ACTOR_ID") == "requester"
        event.user_id = ""  # another sender's steer invalidates the single-actor proof
        assert get_session_env("HERMES_SESSION_ACTOR_ID") == ""
    finally:
        clear_session_vars(outer)
    assert scoped_session_cwd() == ""
    assert get_session_env("HERMES_SESSION_ACTOR_ID") == ""
    reset_session_vars()
