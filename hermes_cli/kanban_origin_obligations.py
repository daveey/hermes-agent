"""Preserve qualified origin custody when a Board replica lacks its chat link.

A readable replica is not proof that the origin ledger has no obligations. This
adapter only adds missing obligations; it neither releases custody nor upgrades
unknown terminal evidence. Source timestamps and human requests are unchanged.
"""
from typing import Any, Iterable


def selected_remote_cards(ids: list[str] | None, gateway: str, statuses: tuple[str, ...],
                          stale_ok: bool) -> list[dict[str, Any]]:
    """Read custody once; retain replica evidence and requests if custody is unreadable."""
    from hermes_cli import kanban_chat_links as links, kanban_session_activity as activity
    from hermes_cli.profiles import get_active_profile_name

    try:
        origin = activity._remote_links(stale_ok=True) if stale_ok else activity._remote_links()
    except Exception:  # health: allow BLE001 -- partial evidence retained; no rows re-raises rather than fresh zero
        selected = links.replica_cards(ids, gateway, statuses, stale_ok=stale_ok, origin_cards=[])
        # An all-chat count read cannot bound which absent chats the unreadable
        # ledger owns. Fail that scope closed; per-chat lists retain known rows.
        if ids is None or not selected:
            raise
        return [{**c, "stale": True, "partial_source_unknown": "origin_ledger_unavailable"} for c in selected]
    selected = links.replica_cards(ids, gateway, statuses, stale_ok=stale_ok, origin_cards=origin)
    return preserve_origin_obligations(selected, origin, gateway=gateway,
                                       profile=get_active_profile_name(), ids=ids, statuses=statuses)


def preserve_origin_obligations(selected: list[dict[str, Any]], ledger: Iterable[dict[str, Any]], *,
                                gateway: str, profile: str, ids: list[str] | None,
                                statuses: tuple[str, ...]) -> list[dict[str, Any]]:
    from hermes_cli.kanban_db_notify import is_machine_session
    from hermes_cli.kanban_placement import is_placeholder_session

    out = list(selected)
    seen = {(c.get("session"), c.get("gateway"), c.get("profile"), c.get("board"), c.get("id")) for c in out}
    for card in ledger:
        if card.get("origin_gateway") != gateway or card.get("origin_profile") != profile:
            continue
        fields = ("session", "gateway", "profile", "board", "id")
        if not all(isinstance(card.get(k), str) and card[k].strip() for k in fields):
            continue
        if card["gateway"] == gateway or card.get("released") or not card.get("unfinished"):
            continue
        if ids is not None and card["session"] not in ids and card.get("origin_session") not in ids:
            continue
        if card.get("status") not in statuses and card.get("status") != "unknown":
            continue
        identity = tuple(card[k] for k in fields)
        if identity in seen:
            continue
        # Same rule as ``kanban_chat_links._ledger_links``: no chat custody for placeholder or machine
        # (worker/cron/one-shot/subagent) transcripts, so their ledger rows are not chat obligations.
        if is_placeholder_session(card["session"]) or is_machine_session(
                gateway, card["session"], local_gateway=gateway, profile=card.get("origin_profile")):
            continue
        seen.add(identity)
        out.append({**card, "session_id": card["session"], "remote": True, "source": "origin-obligation"})
    return out
