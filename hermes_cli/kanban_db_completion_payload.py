"""Completion event data shared by Kanban notification consumers."""

from typing import Any, Optional

from hermes_cli.kanban_final_send import parse_final_sends, public_summary


def completed_event_payload(
    result: Optional[str], event_summary: Optional[str], verified_cards: list[str], metadata: Any,
) -> dict:
    """Keep the summary preview, addressed messages and artifacts on the durable event."""
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
    if isinstance(metadata, dict):
        cleaned = _cleaned_artifact_paths(metadata)
        if cleaned:
            payload["artifacts"] = cleaned
    return payload
