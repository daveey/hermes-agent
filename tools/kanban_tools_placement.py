"""Placement refusals for ``kanban_create`` (split out of :mod:`tools.kanban_tools`). [daveey]"""
from __future__ import annotations

from tools.registry import tool_error


def _placement_refused(pin: str, why: str) -> str:
    """[daveey] A caller-pinned gateway cannot be honoured: say so instead of creating the card here."""
    return tool_error(
        f"placement={pin!r} was not honoured and no card was created: {why}. A pinned gateway is never "
        "silently replaced by a local card. Use placement='local' (or omit it) to create the card on this "
        "gateway, or create it on the target with `hermes-hub create-task <gateway> ...`.",
        code="placement_unavailable", placement=pin, created=False)
