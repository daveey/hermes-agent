"""Withdraw unanswered Desktop approvals so an update can reach the next boundary."""
from tools import approval

NOTICE = ("Hermes is pausing this turn for an update. This approval was withdrawn unanswered; "
          "the command was not approved. Ask for approval again after the turn continues.")
FORCED_NOTICE = ("A forced Hermes update is stopping this turn. This approval was withdrawn unanswered; "
                 "the command was not approved. The turn does not continue automatically.")


def withdraw_for_update(session: dict, marker_key: str, agent) -> None:
    """Approval preparation runs before tool execution/in-flight registration.

    Use the queue's first-settlement lock, never manufacture a deny or consent.
    A batch can publish more approvals after this poll; subsequent polls must
    keep withdrawing until it reaches the normal durable boundary pause.
    """
    from tui_gateway import update_pause as up

    if getattr(agent, "step_boundary_pause", None) is None or getattr(agent, "_interrupt_requested", False):
        return
    pending = approval.list_gateway_approvals(marker_key)
    if not pending:
        return
    stops = bool(up.not_resumable_reason(session, marker_key))  # the forced pause ends it unpaused
    if stops and not up.pause_forced(session):
        return
    for item in pending:
        if approval.withdraw_gateway_approval(marker_key, item["request_id"], FORCED_NOTICE if stops else NOTICE):
            up.logger.info("core-update pause: session %s withdrew pending approval %s unanswered",
                           marker_key, item["request_id"])
