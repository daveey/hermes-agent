"""Placement refusals for ``kanban_create`` (split out of :mod:`tools.kanban_tools`). [daveey]"""
from __future__ import annotations

import logging
from typing import Optional

from tools.registry import tool_error

logger = logging.getLogger(__name__)


def _persisted_session_id(session_id: Optional[str]) -> Optional[str]:
    """Return a session id only when it is a real chat: present in this profile's state.db and not a
    ``kanban.placement`` creator placeholder (a stray notice can give the placeholder a state.db row,
    so presence alone does not prove a chat; defect kanban-69709bcf16). [daveey]"""
    if not session_id:
        return None
    try:
        from hermes_cli.kanban_placement import is_placeholder_session
        if is_placeholder_session(session_id):
            return None
    except Exception:
        logger.debug("Could not check Kanban placement placeholder", exc_info=True)
    try:
        from hermes_state import SessionDB
        from hermes_constants import get_hermes_home

        state = SessionDB(db_path=get_hermes_home() / "state.db", read_only=True)
    except Exception:  # state.db may not exist for a CLI/dashboard invocation
        logger.debug("Could not open state.db to verify Kanban provenance", exc_info=True)
        return None
    try:
        return session_id if state.get_session(session_id) else None
    finally:
        state.close()


def _placement_refused(pin: str, why: str) -> str:
    """[daveey] A caller-pinned gateway cannot be honoured: say so instead of creating the card here."""
    return tool_error(
        f"placement={pin!r} was not honoured and no card was created: {why}. A pinned gateway is never "
        "silently replaced by a local card. Use placement='local' (or omit it) to create the card on this "
        "gateway, or create it on the target with `hermes-hub create-task <gateway> ...`.",
        code="placement_unavailable", placement=pin, created=False)


def _local_placement(kp, view: dict, decision: dict, *, machine_ok, note_local_pin: bool) -> dict:
    """``placement`` for a card ``kp.decide`` kept on this gateway; every local decision says why. [daveey]

    Pins, "needs ..." and plain local reasons used to return nothing, which left 41 of 148 MacBook cards
    unattributable in the placement re-measure. A ``placement='local'`` pin on a card that would run
    elsewhere without the pin also gets a ``note`` naming that target (the card is still created here).
    """
    why = decision.get("why") or ""
    if decision.get("defer"):
        # Parents are local links: created here, forwarded to the target once they finish.
        return {"where": "local", "defer": decision["defer"], "why": why}
    if decision.get("runs_on"):
        logger.info("kanban placement: local, runs_on %s (%s)", decision["runs_on"], why)
        return {"where": "local", "runs_on": decision["runs_on"], "why": why}
    logger.info("kanban placement: local (%s)", why)
    if why.startswith("local marker"):
        return {"where": "local", "why": why, "remedy": kp.LOCAL_MARKER_REMEDY}
    placement = {"where": "local", "why": why}
    if note_local_pin and why == kp.PINNED_LOCAL_WHY:
        unpinned = kp.decide({**view, "placement": "auto"}, machine_ok=machine_ok)
        where = unpinned.get("where")
        elsewhere = (unpinned.get("defer") or ", ".join(unpinned.get("runs_on") or ())) if where == "local" else where
        if elsewhere and where != "refuse":
            placement["note"] = kp.local_pin_note(elsewhere)
    return placement


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
    out: dict = {"placement": {"where": marker_placement["where"], "why": marker_placement["why"]}}
    if marker_placement.get("remedy"):
        out["remedy"] = marker_placement["remedy"]
    if marker_placement.get("note"):
        out["note"] = marker_placement["note"]  # a local pin on a card with no local need
    return out


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
