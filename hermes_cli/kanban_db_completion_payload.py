"""Completion event data shared by Kanban notification consumers."""

from typing import Any, Optional

from hermes_cli.kanban_final_send import parse_final_sends, public_summary


def completed_event_payload(
    result: Optional[str], event_summary: Optional[str], verified_cards: list[str], metadata: Any,
    acceptance: Any = None,
) -> dict:
    """Keep the summary preview, addressed messages and artifacts on the durable event.

    Landing fields come only from the recorded PR acceptance receipt's validated ``landing``
    (``acceptance`` is ``prepare_acceptance``'s ``(snapshot, receipt, metadata)``): raw worker
    metadata never supplies ``pr_left_open`` / ``landing_owner``, and a MERGED PR has neither."""
    from hermes_cli.kanban_db import _cleaned_artifact_paths, _first_line

    payload: dict = {
        "notice_version": 1,
        "result_len": len(result) if result else 0,
        "summary": _first_line(public_summary(event_summary), 400) or None,
    }
    if verified_cards:
        payload["verified_cards"] = verified_cards
    if final_sends := parse_final_sends(event_summary):
        payload["final_sends"] = final_sends
    receipt = acceptance[1] if isinstance(acceptance, tuple) else None
    landing = receipt.get("landing") if isinstance(receipt, dict) else None
    if isinstance(landing, dict):
        from hermes_cli.kanban_pr_landing import left_open_reason
        if reason := left_open_reason(landing):
            payload["pr_left_open"] = {"reason": reason}
        elif isinstance(owner := landing.get("landing_owner"), str):
            payload["landing_owner"] = owner
    if isinstance(metadata, dict):
        if metadata.get("artifact_receipt"):
            payload["artifact_receipt"] = metadata["artifact_receipt"]
        cleaned = _cleaned_artifact_paths(metadata)
        if cleaned:
            payload["artifacts"] = cleaned
    return payload
