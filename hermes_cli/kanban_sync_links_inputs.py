"""Pure inputs of a sync-loop chat-links pass (``kanban_sync_client._links_inputs``): this gateway's origin-ledger
slices and the configuration signature. Moved from kanban_sync_client.py (FILE_LINES cap). [daveey]"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Mapping, Optional


def ledger_slices(cards: Iterable[Mapping[str, Any]], gateway: str) -> dict[str, frozenset]:
    """Per board, every field of this gateway's origin-ledger rows that a links pass reads (``ledger_boards``,
    ``kanban_chat_links._ledger_links``). Compared by value: a re-projection with the same rows is no change."""
    out: dict[str, set] = {}
    for c in cards:
        if not c.get("board") or c.get("origin_gateway") not in (None, gateway):
            continue
        out.setdefault(str(c["board"]), set()).add(
            (c.get("id"), c.get("session"), c.get("via"), c.get("origin_profile"), bool(c.get("released"))))
    return {board: frozenset(rows) for board, rows in out.items()}


def links_signature(gateway: str, placement: Mapping[str, Any]) -> Optional[tuple]:
    """The configuration a links pass reads besides its board: gateway names, profile, homes, placement config."""
    try:
        from hermes_constants import get_hermes_home
        from hermes_cli.kanban_origin import gateway_names
        from hermes_cli.profiles import get_active_profile_name
        return (gateway, gateway_names()[0], get_active_profile_name(), str(get_hermes_home()),
                json.dumps(placement, sort_keys=True, default=str))
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None
