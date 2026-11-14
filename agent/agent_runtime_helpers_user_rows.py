"""Alternation-repair helpers for durable user rows: one row per Kanban notice, and own-row bookkeeping."""

from __future__ import annotations

from typing import Any, Optional


def _remember_own_row(survivor: dict[str, Any]) -> None:
    """An assistant fold rewrites *survivor*'s text, so on a reload without row ids its own row no longer
    matches it by content. Record its loaded fields first, once."""
    from agent.context_compressor import _DB_PERSISTED_MARKER
    from agent.conversation_compression_archive import OWN_ROW, RETIRED_DURABLE_ROWS, retired_row_payload

    recorded = survivor.get(RETIRED_DURABLE_ROWS) or ()
    if (survivor.get(_DB_PERSISTED_MARKER) and not isinstance(survivor.get("_row_id"), int)
            and not any(isinstance(row, dict) and row.get(OWN_ROW) for row in recorded)):
        survivor.setdefault(RETIRED_DURABLE_ROWS, []).append({**retired_row_payload(survivor), OWN_ROW: True})


def _notice_id(msg: Any) -> Optional[str]:
    meta = msg.get("display_metadata") if isinstance(msg, dict) else None
    if not isinstance(meta, dict) or msg.get("role") != "user" or msg.get("display_kind") != "kanban_notice":
        return None
    value = meta.get("notice_id")
    return value if isinstance(value, str) and value else None


def keep_one_notice_row(prev: dict[str, Any], msg: Any) -> Optional[dict[str, Any]]:
    """Two adjacent user rows of ONE Kanban notice (a passive row, then the turn it woke): the row to keep, else None.

    [daveey] Concatenating them (the plain user-user merge) made the model read the notice twice and dropped the
    turn's api_content (its ask-gate live-state block) with the stale sidecar (t_01a1db370b459fab). The row
    carrying api_content is kept (the woken turn's), else the first; the other is retired onto it, unfolded."""
    from agent.agent_runtime_helpers import _count_unnamed_row, _remember_absorbed_row
    from agent.conversation_compression_archive import MERGED_DURABLE_ROWS
    from agent.message_metadata import MERGED_TURN_PREFIX

    if not isinstance(msg, dict) or (nid := _notice_id(prev)) is None or _notice_id(msg) != nid:
        return None
    # A previous plain merge retains the first notice's identity while adding other input.
    # Never supersede that input merely because its carrier has the same notice_id.
    if prev.get("content") != msg.get("content") and any(
        row.get(key) for row in (prev, msg)
        for key in (MERGED_TURN_PREFIX, MERGED_DURABLE_ROWS, "_absorbed_row_ids")
    ):
        return None
    keep, drop = (msg, prev) if ("api_content" in msg and "api_content" not in prev) else (prev, msg)
    _count_unnamed_row(keep, drop)
    _remember_absorbed_row(keep, drop, folded=False)
    return keep
