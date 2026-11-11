"""``hermes kanban`` handoff-metadata flag parsing and completion-receipt refusals. [daveey]

Split from the ``hermes_cli.kanban`` facade. A ``completion_receipt`` is checked structurally
when the flag is parsed, so a malformed receipt is refused before any goal judge or write;
pending-owner admission still runs inside canonical ``complete_task``.
"""
from __future__ import annotations

import json
from typing import Optional

from hermes_cli.kanban_output import _err


def parse_metadata_flag(raw: Optional[str]) -> tuple[Optional[dict], int]:
    """Parse ``--metadata`` JSON; returns ``(dict|None, rc)`` with rc=2 on error."""
    if not raw:
        return None, 0
    try:
        metadata = json.loads(raw)
        if not isinstance(metadata, dict):
            raise ValueError("must be a JSON object")
        if "completion_receipt" in metadata:
            from hermes_completion_receipt import validate_completion_receipt
            validate_completion_receipt(metadata["completion_receipt"])
    except (ValueError, json.JSONDecodeError) as exc:
        return None, _err(f"kanban: --metadata: {exc}", 2)
    return metadata, 0


def receipt_refusal(tid: str, exc: Exception) -> str:
    """The CLI line for a receipt admission/preservation refusal (nothing was written).

    Re-raises any other ``ValueError`` so unrelated failures keep their old behavior.
    """
    from hermes_cli import kanban_db as kb

    if isinstance(exc, kb.ArtifactPreservationError) or str(exc).startswith("completion_receipt"):
        return f"cannot complete {tid}: {exc}"
    raise exc
