"""A pinned route's pre-turn refusal carries its own sentence to the client's error card.

The router refusing a chat's explicit pin (a subscription wall until a known reset) is stamped
``route_refused`` on the gateway layer. Without the sentence on the descriptor, Desktop showed the
gateway layer's generic "internal problem ... send your message again" card, advice that can never
work until the reset, and never named the fix (/model).
"""
from agent.error_surface import build_error_surface_from_result
from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
from agent.turn_failure_copy import FAILED_TURN_NOTICE, ROUTE_REFUSED_TURN_NOTICE
from gateway.config import Platform
from gateway.run_turn_runner import _resolution_failure_result
from gateway.session_settlement_recovery import completion_payload, gateway_refusal_boundary

RESET = 1794981540.0


def _wall():
    return SubscriptionQuotaUnavailable("openai-codex", "gpt-6-astra", RESET)


def test_local_route_refusal_result_carries_its_sentence_as_the_card_body():
    result = _resolution_failure_result(_wall(), Platform.LOCAL)

    text = result["final_response"]
    assert "openai-codex/gpt-6-astra" in text and "/model" in text
    assert str(RESET) not in text  # the reset is named in PT, never as an epoch
    assert (result["failed"], result["failure_reason"], result["failure_retryable"]) == (True, "route_refused", False)
    # The raw resolver message stays the error (the card's details), the sentence is the card's body.
    assert result["error"] == str(_wall())
    surface = result["error_surface"]
    assert (surface["layer"], surface["code"], surface["retryable"]) == ("gateway", "route_refused", False)
    assert surface["message"] == text


def test_settlement_commits_the_refusal_and_its_card_as_the_failed_turn_boundary():
    """A reopened chat redraws the card from the boundary row's display_metadata; the boundary
    itself never tells the user to send again (a resend repeats the refusal until the reset)."""
    result = _resolution_failure_result(_wall(), Platform.LOCAL)

    boundary = gateway_refusal_boundary({"result": result}, "failed")
    assert boundary is not None
    notice, metadata = boundary

    assert notice == ROUTE_REFUSED_TURN_NOTICE
    assert "Send it again" not in notice
    assert metadata["error"] == result["error"]
    assert metadata["error_surface"]["code"] == "route_refused"
    assert metadata["error_surface"]["message"] == result["final_response"]


def test_messaging_boundary_notice_drops_the_resend_advice():
    from gateway.run_turn_persistence import GatewayTurnPersistenceMixin

    persistence = GatewayTurnPersistenceMixin()
    refused = {"failed": True, "failure_reason": "route_refused", "messages": []}
    other = {"failed": True, "failure_reason": "loop_error", "messages": []}

    assert persistence._hmwa_failed_turn_notice(refused) == ROUTE_REFUSED_TURN_NOTICE
    assert persistence._hmwa_failed_turn_notice(other) == FAILED_TURN_NOTICE


def test_other_gateway_refusals_keep_their_error_as_the_boundary():
    """Control: a cron-session refusal (no refusal_notice) still writes its error as the boundary."""
    captured = {"result": {"error": "This chat runs a scheduled job.",
                           "error_surface": {"layer": "gateway", "code": "cron_session_read_only"}}}

    boundary = gateway_refusal_boundary(captured, "failed")
    assert boundary is not None and boundary[0] == "This chat runs a scheduled job."


def test_live_completion_frame_carries_the_descriptor_with_the_sentence():
    result = _resolution_failure_result(_wall(), Platform.LOCAL)
    row = {"admission_id": "adm-1"}

    complete = completion_payload(row, {"outcome": "failed"}, result["final_response"], {"result": result})

    assert complete["status"] == "error"
    assert complete["error_surface"]["code"] == "route_refused"
    assert complete["error_surface"]["message"] == result["final_response"]


def test_messaging_platform_quota_wall_keeps_its_old_shape():
    """Control: off the local surface the wall stays an auth_permanent verdict with no descriptor."""
    exc = _wall()
    result = _resolution_failure_result(exc, Platform.TELEGRAM)

    assert result["failure_reason"] == "auth_permanent"
    assert result["error"] == str(exc)
    assert "error_surface" not in result


def test_only_own_sentence_refusals_put_their_reply_on_the_descriptor():
    """Control: a genuine runtime failure's reply is not promoted to the card body."""
    route = build_error_surface_from_result(
        {"failed": True, "error": "x", "failure_reason": "route_refused", "final_response": "  ⚠️ pinned wall  "})
    loop = build_error_surface_from_result(
        {"failed": True, "error": "boom", "failure_reason": "loop_error", "final_response": "partial words"})

    assert route is not None and loop is not None
    assert route["message"] == "⚠️ pinned wall"
    assert "message" not in loop
