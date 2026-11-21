"""Machine-readable evidence at the ownership-card publication boundary."""
import logging

from tui_gateway.owner_forward import FORWARDABLE_LEDGER_STATES

logger = logging.getLogger(__name__)


def log_ownership_card(session: dict, error) -> None:
    """Use refusal provenance, never message text, to label an emitted ownership card."""
    status = getattr(error, "ledger_status", None)
    cause = getattr(error, "ownership_cause", None)
    if status in FORWARDABLE_LEDGER_STATES:
        cause = f"ledger_{status}"
    elif cause not in ("lease_foreign", "lease_self_orphan", "update_marker"):
        cause = "unknown"
    pid = getattr(error, "holder_pid", None)
    logger.warning("session_ownership_card shown session=%s cause=%s holder_pid=%s",
                   session.get("session_key") or "", cause, pid if isinstance(pid, int) and pid > 0 else 0)
