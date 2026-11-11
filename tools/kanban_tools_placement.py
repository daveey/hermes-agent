"""Placement refusals for ``kanban_create`` (split out of :mod:`tools.kanban_tools`). [daveey]"""
from __future__ import annotations

import logging
from typing import Optional

from tools.registry import tool_error

logger = logging.getLogger(__name__)


def _effective_board_args(args, origin_intent):
    from hermes_cli import kanban_db as kb
    return args if origin_intent is not None else {**args, "board": args.get("board") or kb.get_current_board()}


def _origin_enrollment(args, origin, marker, **effective):
    """Chat ownership pins storage, not execution; never forward an origin-bound card."""
    if origin is None:
        return marker
    from hermes_cli import kanban_placement as kp
    from hermes_cli.kanban_worker_placement import creation_decision
    cfg, gateway = kp.config(), kp.reg.self_gateway()
    return creation_decision({**args, **effective}, cfg, gateway,
                             kp.canonical_pin(args.get("placement") or "auto", gateway, cfg), None)


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


def _placement_origin(conn, creator_task_id: Optional[str], session_id: Optional[str]) -> dict:
    """``{session_id, origin_profile}`` a card routed to another gateway carries as its origin chat. [daveey]

    The receiving gateway never sees a worker's creator card (it lives on this board), so it cannot walk to the
    creator's chat itself. A dispatcher worker sends the nearest real chat ``resolve_chats`` finds here: on THIS
    gateway only, because ``hermes-hub create-task`` binds an origin on the sending gateway. A worker transcript is
    never an origin (no chat reads it back); anything else keeps the caller's session. ``origin_profile`` None
    means this process's profile.
    """
    from hermes_cli.kanban_db_notify import is_headless_session
    if creator_task_id:
        try:  # never raises: placement itself decides what an identity failure means
            from hermes_cli import kanban_capability_registry as reg
            from hermes_cli.kanban_creator_chats import resolve_chats
            self_gw = reg.self_gateway()
            has_links = conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone()
            for owner in (resolve_chats(conn, creator_task_id) if has_links else []):
                if self_gw and owner["chat_gateway"] == self_gw and _persisted_session_id(owner["chat_session"]):
                    return {"session_id": owner["chat_session"], "origin_profile": owner.get("chat_profile") or None}
        except Exception:
            logger.warning("Could not resolve the creator chat for a placed card", exc_info=True)
    if session_id and is_headless_session(session_id):
        return {"session_id": None, "origin_profile": None}
    return {"session_id": session_id, "origin_profile": None}


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
    if decision.get("placement_policy"):
        return {"where": "local", "placement_policy": decision["placement_policy"], "why": why}
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


def _marker_placement_fields(conn, new_tid: str, marker_placement: Optional[dict], session_id: Optional[str],
                             origin_profile: Optional[str] = None) -> dict:
    """Result fields for a card a placement marker kept here; a deferred card records its forward. [daveey]"""
    if not marker_placement:
        return {}
    if marker_placement.get("defer"):
        # Run on the routed gateway once the parents finish (kanban_placement_forward).
        from hermes_cli import kanban_placement_forward as _kpf
        from hermes_cli.profiles import current_profile_name as _cpn
        _kpf.record_deferral(conn, new_tid, target=marker_placement["defer"], origin_session=session_id,
                             origin_profile=origin_profile or _cpn("default") or "default")
        return {"placement": {"where": "local", "defer": marker_placement["defer"], "why": marker_placement["why"]},
                "note": (f"Created here as {new_tid} so it can wait on its parents; it runs on "
                         f"{marker_placement['defer']}, not here: when the parents finish the dispatcher "
                         f"forwards it there and this card completes when that one does.")}
    if marker_placement.get("runs_on") or marker_placement.get("placement_policy"):
        return {"placement": dict(marker_placement)}
    out: dict = {"placement": {"where": marker_placement["where"], "why": marker_placement["why"]}}
    if marker_placement.get("remedy"):
        out["remedy"] = marker_placement["remedy"]
    if marker_placement.get("note"):
        out["note"] = marker_placement["note"]  # a local pin on a card with no local need
    return out


def _machine_placement(machine: str, runs_on: list[str]) -> dict:
    """Preserve the machine pin's reason when replacing the rewritten local-pin decision. [daveey]"""
    from hermes_cli.kanban_placement import pinned_machine_why

    why = pinned_machine_why(machine)
    logger.info("kanban placement: local, runs_on %s (%s)", runs_on, why)
    return {"where": "local", "runs_on": runs_on, "machine": machine, "why": why}


def _runs_where(runs_on, pinned_machine, marker_placement) -> str:
    """Where a card created on this board runs, for the kanban_create result. Never raises."""
    if marker_placement and marker_placement.get("placement_policy"):
        return "worker pool (waiting_for_worker_capacity; no local fallback)"
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


def _create_session_id(args: dict, origin_intent: Optional[dict], self_task) -> Optional[str]:
    """The new card's session. The worker/API runtime may be transient; the owning task's origin is durable.
    The ambient id is the request-scoped ContextVar binding, not the process-global os.environ: in a
    multi-session gateway the env holds the LAST agent built, and an id that never reached its ``sessions``
    row is provenance pointing at nothing."""
    if origin_intent is not None:
        # Courier retries use only their durable envelope, never the receiving
        # process's current session/tenant/project. Creator provenance remains
        # unchanged even if its session is compressed or subsequently removed.
        return args.get("session_id")
    from gateway.session_context import get_session_env
    from tools.async_delegation import _current_origin_session_id
    return (_persisted_session_id(args.get("session_id"))
            # The parent card's session too: a card placed from another gateway carries
            # a placeholder (or a chat that lives elsewhere), and handing that on as a
            # child's origin made the owner's bind fail (kanban-69709bcf16). [daveey]
            or _persisted_session_id(self_task.session_id if self_task else None)
            or _persisted_session_id(_current_origin_session_id())
            or _persisted_session_id(get_session_env("HERMES_SESSION_ID", "")))
