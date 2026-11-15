"""Alternation-repair helpers for durable user rows: one row per Kanban notice, and own-row bookkeeping."""

from __future__ import annotations

from typing import Any, Optional
import hashlib


def _remember_own_row(survivor: dict[str, Any]) -> None:
    """An assistant fold rewrites *survivor*'s text, so on a reload without row ids its own row no longer
    matches it by content. Record its loaded fields first, once."""
    from agent.context_compressor import _DB_PERSISTED_MARKER
    from agent.conversation_compression_archive import OWN_ROW, RETIRED_DURABLE_ROWS, retired_row_payload

    recorded = survivor.get(RETIRED_DURABLE_ROWS) or ()
    if (survivor.get(_DB_PERSISTED_MARKER) and not isinstance(survivor.get("_row_id"), int)
            and not any(isinstance(row, dict) and row.get(OWN_ROW) for row in recorded)):
        survivor.setdefault(RETIRED_DURABLE_ROWS, []).append({**retired_row_payload(survivor), OWN_ROW: True})


def _count_unnamed_row(survivor: dict[str, Any], retired: dict[str, Any]) -> None:
    """On a reload without row ids nothing names *retired*'s durable row once the repair takes the dict
    out of the list, so *survivor* counts it. A dict that counts rows was loaded too: a merge may have
    popped its marker since. Call before the merge rewrites the survivor: *retired*'s own fields are
    recorded so the commit can name its row."""
    from agent.context_compressor import _DB_PERSISTED_MARKER
    from agent.conversation_compression_archive import (
        OWN_ROW, RETIRED_DURABLE_ROWS, UNNAMED_DURABLE_ROWS, retired_row_payload)

    def loaded(message: dict[str, Any]) -> bool:
        return bool(message.get(_DB_PERSISTED_MARKER) or message.get(UNNAMED_DURABLE_ROWS))

    if loaded(survivor) and loaded(retired) and not isinstance(retired.get("_row_id"), int):
        survivor[UNNAMED_DURABLE_ROWS] = int(survivor.get(UNNAMED_DURABLE_ROWS) or 0) + 1
        # A previously folded dict already records its original row. The transfer in
        # _remember_absorbed_row retires that record; its synthetic text/calls never existed in storage.
        if not any(row.get(OWN_ROW) for row in retired.get(RETIRED_DURABLE_ROWS) or ()):
            survivor.setdefault(RETIRED_DURABLE_ROWS, []).append(retired_row_payload(retired))


def _notice_id(msg: Any) -> Optional[str]:
    meta = msg.get("display_metadata") if isinstance(msg, dict) else None
    if (not isinstance(meta, dict) or "notice_constituents_v1" in meta
            or msg.get("role") != "user" or msg.get("display_kind") != "kanban_notice"):
        return None
    value = meta.get("notice_id")
    return value if isinstance(value, str) and value else None


# Only offsets and notice identities live in display metadata. Model-only bytes stay
# exclusively in api_content, never in a display-facing metadata payload.
NOTICE_PARTS = "notice_constituents_v1"
NOTICE_TURN_OVERRIDE = "_merged_notice_turn_override"


def _join_parts(parts: list[dict], field: str) -> str:
    return "\n\n".join(p[field] for p in parts if p[field])


def _constituents(row: dict) -> list[dict]:
    meta = row.get("display_metadata") or {}
    spans = meta.get(NOTICE_PARTS) if isinstance(meta, dict) else None
    display, model = row.get("content", ""), row.get("api_content", row.get("content", ""))
    if isinstance(spans, list) and spans and isinstance(model, str):
        try:
            parts = []
            for span in spans:
                a, b, c, d = span["offsets"]
                if not all(type(n) is int for n in (a, b, c, d)) or not (0 <= a <= b <= len(display) and 0 <= c <= d <= len(model)):
                    break
                nid = span.get("notice_id")
                if nid is not None and (not isinstance(nid, str) or not nid):
                    break
                if span.get("display_sha256") != hashlib.sha256(display[a:b].encode("utf-8", "surrogatepass")).hexdigest():
                    break
                parts.append({"content": display[a:b], "api_content": model[c:d], "notice_id": nid})
            else:
                if _join_parts(parts, "content") == display and _join_parts(parts, "api_content") == model:
                    return parts
        except (KeyError, TypeError, ValueError):
            pass
    # An opaque premerged carrier cannot safely be classified as a single notice.
    from agent.conversation_compression_archive import MERGED_DURABLE_ROWS
    from agent.message_metadata import MERGED_TURN_PREFIX

    merged = any(row.get(k) for k in (NOTICE_PARTS, MERGED_TURN_PREFIX, MERGED_DURABLE_ROWS, "_absorbed_row_ids"))
    nid = None if spans or merged else _notice_id(row)
    return [{"content": display, "api_content": model if nid and isinstance(model, str) else display, "notice_id": nid}]


def merge_notice_constituents(prev: dict, msg: dict) -> Optional[tuple[str, str, dict]]:
    """Merge identified constituents, never infer notice boundaries from text."""
    parts = _constituents(prev) + _constituents(msg)
    if not any(p["notice_id"] for p in parts):
        return None
    kept, positions = [], {}
    for part in parts:
        nid = part["notice_id"]
        if nid is not None and nid in positions:
            index = positions[nid]
            if kept[index]["api_content"] == kept[index]["content"]:
                kept[index] = part
        else:
            if nid is not None:
                positions[nid] = len(kept)
            kept.append(part)
    display, model = _join_parts(kept, "content"), _join_parts(kept, "api_content")
    spans, display_end, model_end = [], 0, 0
    for part in kept:
        a = display_end + (2 if display_end and part["content"] else 0)
        c = model_end + (2 if model_end and part["api_content"] else 0)
        display_end, model_end = a + len(part["content"]), c + len(part["api_content"])
        spans.append({"offsets": [a, display_end, c, model_end], "notice_id": part["notice_id"],
                      "display_sha256": hashlib.sha256(part["content"].encode("utf-8", "surrogatepass")).hexdigest()})
    meta = prev.get("display_metadata")
    return display, model, {**(meta if isinstance(meta, dict) else {}), NOTICE_PARTS: spans}


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
