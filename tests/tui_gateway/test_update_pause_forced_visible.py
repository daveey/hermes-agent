"""[daveey] A turn a forced core update stopped (``FORCED_STOP_REASON``) does not continue on its own, so it must not
render as nothing: like a lost session lease (``prompt_turn._surface_system_interrupt``) it becomes a visible failure
telling the user to send the message again. A plain pause and a human Stop are unchanged."""

from __future__ import annotations

from tests.tui_gateway import test_lease_lost_turn_visible as lease
from tui_gateway import update_pause as up

emitted = lease.emitted


def test_forced_stop_renders_a_visible_failure_that_says_to_send_again(emitted):
    session = lease._run({"final_response": "", "messages": [], "interrupted": True, "completed": False,
                          "paused_for_update": up.FORCED_STOP_REASON})
    payload = lease._complete(emitted)
    assert payload["status"] == "error"
    assert payload["error"] == up.FORCED_STOP_MESSAGE
    assert "Stopped for a forced Hermes update. Send your message again to continue." in payload["text"]
    assert payload["error_surface"]["code"] == "core_update_forced_stop"
    assert payload["error_surface"]["layer"] == "gateway"
    assert not payload.get("paused_for_update")  # not announced as a pause that continues automatically
    assert session["inflight_turn"] is not None


def test_forced_stop_keeps_text_already_produced(emitted):
    lease._run({"final_response": "Half of it", "messages": [], "interrupted": True,
                "paused_for_update": up.FORCED_STOP_REASON})
    payload = lease._complete(emitted)
    assert payload["status"] == "error" and payload["partial"] is True and payload["text"] == "Half of it"


def test_a_plain_update_pause_is_not_turned_into_a_failure(emitted):
    lease._run({"final_response": "", "messages": [], "interrupted": True, "paused_for_update": up.PAUSE_REASON})
    assert lease._complete(emitted)["status"] == "interrupted"
