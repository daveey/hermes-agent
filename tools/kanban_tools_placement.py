"""Placement refusals for ``kanban_create`` (split out of :mod:`tools.kanban_tools`). [daveey]"""
from __future__ import annotations

from typing import Optional

from tools.registry import tool_error


def _placement_refused(pin: str, why: str) -> str:
    """[daveey] A caller-pinned gateway cannot be honoured: say so instead of creating the card here."""
    return tool_error(
        f"placement={pin!r} was not honoured and no card was created: {why}. A pinned gateway is never "
        "silently replaced by a local card. Use placement='local' (or omit it) to create the card on this "
        "gateway, or create it on the target with `hermes-hub create-task <gateway> ...`.",
        code="placement_unavailable", placement=pin, created=False)


def _marker_placement_fields(conn, new_tid: str, marker_placement: Optional[dict], session_id: Optional[str]) -> dict:
    """Result fields for a card a placement marker kept here; a deferred card records its forward. [daveey]"""
    if not marker_placement:
        return {}
    if marker_placement.get("defer"):
        # Run on the routed gateway once the parents finish (kanban_placement_forward).
        from hermes_cli import kanban_placement_forward as _kpf
        from hermes_cli.profiles import current_profile_name as _cpn
        _kpf.record_deferral(conn, new_tid, target=marker_placement["defer"], origin_session=session_id,
                             origin_profile=_cpn("default") or "default")
        return {"placement": {"where": "local", "defer": marker_placement["defer"], "why": marker_placement["why"]},
                "note": (f"Created here as {new_tid} so it can wait on its parents; it runs on "
                         f"{marker_placement['defer']}, not here: when the parents finish the dispatcher "
                         f"forwards it there and this card completes when that one does.")}
    if marker_placement.get("runs_on"):
        return {"placement": dict(marker_placement)}
    return {"placement": {"where": marker_placement["where"], "why": marker_placement["why"]},
            "remedy": marker_placement["remedy"]}


def _runs_where(runs_on, pinned_machine, marker_placement) -> str:
    """Where a card created on this board runs, for the kanban_create result. Never raises."""
    if runs_on is not None:
        if pinned_machine:
            return f"{pinned_machine} via the board lease (runs_on={runs_on})"
        return f"a machine matching runs_on={runs_on} via the board lease"
    if marker_placement and marker_placement.get("defer"):
        return f"{marker_placement['defer']} once its parents finish"
    try:
        from hermes_cli import kanban_capability_registry as _reg
        gw = _reg.self_gateway()
    except Exception:
        gw = None
    return f"here ({gw})" if gw else "here"
