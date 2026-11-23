"""The one predicate for "this card is an open human ask", and the ``needs_you`` row field built from it.

[daveey] The sidebar badge (``kanban_blocked``), the stored session outcome (``_card_items`` decisions) and the
Desktop stripe each used to work out "is a human being asked something?" separately, so a card could count in one
and not the other and the stripe went non-red while the row still showed blocked cards. Every consumer now calls
``card_is_open_human_ask``; the row stamps ``needs_you`` from the same snapshot as ``kanban_blocked`` and the Desktop
paints red whenever it is non-empty (``sessionStatusStripe``).
"""

from __future__ import annotations

from typing import Any, Iterable

BLOCKED_STATUSES = ("blocked", "triage")


def card_is_open_human_ask(status: Any, block_kind: Any, human_request: Any) -> bool:
    """A card parked (``blocked``/``triage``) on a validated ``human_request``. ``human_request`` may be the decoded
    dict or its stored JSON text; anything undecodable is not an ask."""
    if status not in BLOCKED_STATUSES:
        return False
    # The status-independent leaf is shared with the annotation cache's board rows (``_board_rows`` stores this
    # verdict beside the status and gates on status where it reads it), so there is one decode/validate path.
    from hermes_cli.kanban_session_wait_rows import _stored_needs_human

    return bool(_stored_needs_human(human_request, block_kind))


def needs_you(card: dict) -> bool:
    """Current card attention, never a historical block kind or an implied consent.

    Projected wait evidence and raw lane cards share this entry point. Legacy
    scheduled device probes carry presentation-only attention, not approval.
    """
    status = card.get("status")
    if status not in ("blocked", "triage", "scheduled") or card.get("stale"):
        return False
    import json

    def obj(value):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return {}
        return value if isinstance(value, dict) else {}

    wait = obj(card.get("wait"))
    if wait.get("wait_state"):
        return wait["wait_state"] == "human"
    human = obj(card.get("human_request"))
    if card_is_open_human_ask(status, card.get("block_kind"), human):
        return True
    if status != "scheduled":
        return False
    if obj(card.get("wakes")).get("blocks_for_human") and human.get("action"):
        return True
    from hermes_cli.kanban_wait_info import owner_device_step

    park = obj(card.get("park"))
    for event in reversed(card.get("events") or ()):
        if event.get("kind") in ("scheduled", "continued"):
            park = obj(event.get("payload"))
            break
        if event.get("kind") in ("claimed", "unblocked", "completed", "archived"):
            break
    return bool(owner_device_step(park.get("wake_check")))


def open_human_asks(card_refs: Iterable[str], count: int) -> list[dict[str, str]]:
    """``needs_you`` entries for one row: one ``card`` entry per open ask. ``card_refs`` are the owner-qualified
    keys (``<board>/<id>``) of the blocked cards of the row's lineage, from the snapshot that supplies ``count``
    (``kanban_blocked``). When the snapshot knows fewer keys than it counted (an older replica row without a key), the
    entries are padded with a ref-less one, so ``len(needs_you)`` never undercounts the badge.

    Shape (deliberately narrowed): entries are ``kind == "card"`` only and ``question`` is empty. The count snapshot
    carries card keys, not the ``human_request`` text, and reading it would add a board scan per list. In-chat
    prompts/approvals and a stuck review reach the same red invariant on the Desktop from the row's execution
    observations and ``review_wait`` (``kanbanStripeCounts`` / ``hasOpenHumanAsk``), not from this list."""
    refs = sorted({str(r) for r in card_refs if r})
    out = [{"kind": "card", "ref": ref, "question": ""} for ref in refs]
    out += [{"kind": "card", "ref": "", "question": ""} for _ in range(max(0, int(count) - len(refs)))]
    return out


def stamp_needs_you(row: dict, ids: Iterable[str], remote_keys: dict) -> None:
    """Stamp ``row['needs_you']`` from the snapshot that stamped ``kanban_blocked`` (same ids, same predicate).
    Absent when empty, like every other count: the Desktop then falls back to ``kanban_blocked``, also absent."""
    asks = open_human_asks(
        (key for sid in ids for key, bucket, _ in remote_keys.get(sid, ()) if bucket == "blocked"),
        int(row.get("kanban_blocked") or 0))
    if asks:
        row["needs_you"] = asks
    else:
        row.pop("needs_you", None)
