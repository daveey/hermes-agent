"""Placement refusals for ``kanban_create`` (split out of :mod:`tools.kanban_tools`). [daveey]"""
from __future__ import annotations

import logging
from typing import Optional

from tools.registry import tool_error

logger = logging.getLogger(__name__)


def _effective_board_args(args, origin_intent):
    from hermes_cli import kanban_db as kb
    return args if origin_intent is not None else {**args, "board": args.get("board") or kb.get_current_board()}


def _courier_landing(conn, args, origin_intent, marker, runs_on, **effective):
    """A routed create landing here is judged by the receiving hub before its txn. [daveey]"""
    if origin_intent is None:
        return marker
    from hermes_cli.kanban_hub_admission_placement import courier_plan
    return courier_plan(conn, {**args, **effective}, runs_on, request_id=origin_intent.get("request_id")) or marker


def _origin_enrollment(args, origin, marker, **effective):
    """Chat ownership pins storage, not execution; never forward an origin-bound card."""
    if origin is None:
        return marker
    from hermes_cli import kanban_placement as kp
    from hermes_cli.kanban_worker_placement import creation_decision
    cfg, gateway = kp.config(), kp.reg.self_gateway()
    from hermes_cli.kanban_hub_admission_placement import apply
    view = {**args, **effective}
    # Execution routing is the same as a local create's (worker queue first); only storage is pinned here.
    decision = creation_decision(view, cfg, gateway,
                                 kp.canonical_pin(args.get("placement") or "auto", gateway, cfg), None)
    return apply(view, decision or {"where": "local", "why": "origin-bound storage"}, gateway, cfg)


def _landing_placement(conn, args, origin, origin_intent, marker, runs_on, *, title, **effective):
    """``(marker, runs_on)`` after origin-bound enrollment and the courier landing judgement. [daveey]

    A queued outcome (``runs_on: [<pool label>, ...]``) becomes the card's ``runs_on`` unless the caller gave one;
    t_01a1d0571c2f07d6: both entries used to take worker-first, which pins one worker.
    """
    marker = _origin_enrollment(args, origin, marker, **effective)
    marker = _courier_landing(conn, args, origin_intent, marker, runs_on, title=title, **effective)
    if runs_on is None and (marker or {}).get("runs_on"):
        runs_on = list(marker["runs_on"])
    return marker, runs_on


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
    """Preserve the discovered chat owner, including a foreign gateway.

    Discovery is not authorization: foreign sessions are verified on their own
    gateway by the authenticated forward adapter, never in the worker's state.db.
    Resolution errors must not turn a qualified origin into a plain local session.
    """
    from hermes_cli.kanban_db_notify import is_headless_session
    if creator_task_id:
        from hermes_cli import kanban_capability_registry as reg
        from hermes_cli.kanban_creator_chats import resolve_chats
        self_gw = reg.self_gateway()
        has_links = conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone()
        for owner in (resolve_chats(conn, creator_task_id) if has_links else []):
            gateway, session = owner["chat_gateway"], owner["chat_session"]
            if gateway and gateway != self_gw:
                return {"session_id": session, "origin_profile": owner.get("chat_profile") or "default",
                        "origin_gateway": gateway}
            if self_gw and gateway == self_gw and _persisted_session_id(session):
                return {"session_id": session, "origin_profile": owner.get("chat_profile") or None}
    if session_id and is_headless_session(session_id):
        return {"session_id": None, "origin_profile": None}
    return {"session_id": session_id, "origin_profile": None}


def _forwarding_fields(conn, origin_gateway, creator_task_id, self_gw, run_id_fn) -> dict:
    """Kwargs that forward a qualified foreign origin to the placement target; empty when the origin is local."""
    if not origin_gateway or origin_gateway == self_gw:
        return {}
    from hermes_cli.kanban_placement_provenance import forwarding_authority
    return {"origin_gateway": origin_gateway, **forwarding_authority(
        conn, creator_task_id, run_id_fn(creator_task_id) if creator_task_id else None, self_gw=self_gw)}


def _placement_refused(pin: str, why: str, *, hub_admission=None, remedy=None) -> str:
    """[daveey] A caller-pinned gateway cannot be honoured: say so instead of creating the card here.

    ``remedy`` replaces the generic alternatives when they cannot work: on the hub a denied stay-here card was
    told to "use placement='local'", which is what it had passed (t_01a1cf9334ff49bc)."""
    alternatives = remedy or (
        "Use placement='local' (or omit it) to create the card on this "
        "gateway, or create it on the target with `hermes-hub create-task <gateway> ...`.")
    return tool_error(
        f"placement={pin!r} was not honoured and no card was created: {why}. A pinned gateway is never "
        f"silently replaced by a local card. {alternatives}",
        code="placement_unavailable", placement=pin, created=False,
        **({"hub_admission": hub_admission} if hub_admission else {}))


def _admission_refused(marker_placement) -> None:
    """[daveey] Raise the refusal for a denied hub admission found after origin enrollment.

    ``_Reject`` carries the finished payload to ``_kanban_handler`` (no branch in ``_handle_create``)."""
    if not marker_placement or marker_placement.get("where") != "refuse":
        return
    from tools.kanban_tools import _Reject
    raise _Reject("", payload=_placement_refused(marker_placement.get("pin", "hub"), marker_placement.get("why", ""),
                                                 hub_admission=marker_placement.get("hub_admission"),
                                                 remedy=marker_placement.get("remedy")))


def _effective_requires(marker_placement, args: dict):
    """[daveey] Requirements to persist: a denied hub card carries ``hub:write`` from the planner."""
    return (marker_placement or {}).get("requires", args.get("requires"))


def _with_admission(placement: dict, marker_placement) -> dict:
    """[daveey] A machine-pin create reply keeps the hub admission verdict that let it through."""
    verdict = (marker_placement or {}).get("hub_admission")
    return {**placement, "hub_admission": verdict} if verdict else placement


def _local_placement(kp, view: dict, decision: dict, *, machine_ok, note_local_pin: bool) -> dict:
    """``placement`` for a card ``kp.decide`` kept on this gateway; every local decision says why. [daveey]

    Pins, "needs ..." and plain local reasons used to return nothing, which left 41 of 148 MacBook cards
    unattributable in the placement re-measure. A ``placement='local'`` pin on a card that would run
    elsewhere without the pin also gets a ``note`` naming that target (the card is still created here).
    """
    why = decision.get("why") or ""
    if decision.get("hub_admission"):
        # Admission already resolved the execution target; advisory re-deciding
        # would make another model call and could discard the effective contract.
        return dict(decision)
    if decision.get("placement_policy"):
        return {"where": "local", "placement_policy": decision["placement_policy"], "why": why}
    if decision.get("defer"):
        # Parents are local links: created here, forwarded to the target once they finish.
        return {"where": "local", "defer": decision["defer"], "why": why}
    if decision.get("runs_on"):
        logger.info("kanban placement: local, runs_on %s (%s)", decision["runs_on"], why)
        placement = {"where": "local", "runs_on": decision["runs_on"], "why": why}
        if decision.get("note"):  # a pool worker's unmet local marker: where the real need is met
            placement["note"] = decision["note"]
        return placement
    logger.info("kanban placement: local (%s)", why)
    if why.startswith("local marker"):
        return {"where": "local", "why": why, "remedy": kp.marker_remedy(why, None, view)}
    placement = {"where": "local", "why": why}
    if decision.get("note"):  # a pool worker's home-marker keep: what it holds, and how to free the card
        placement["note"] = decision["note"]
    if note_local_pin and why == kp.PINNED_LOCAL_WHY:
        # Unpinned means placement omitted: an explicit 'auto' would also drop a skill marker (marker_skills).
        unpinned = kp.decide({k: v for k, v in view.items() if k != "placement"}, machine_ok=machine_ok)
        where = unpinned.get("where")
        elsewhere = (unpinned.get("defer") or ", ".join(unpinned.get("runs_on") or ())) if where == "local" else where
        if elsewhere and where != "refuse":
            placement["note"] = kp.local_pin_note(elsewhere)
    return placement


def _denied_fields(conn, task_id: str, marker_placement: dict) -> Optional[dict]:
    """``placement`` + ``note`` for a card hub admission denied: the pool route it was persisted with,
    never the pre-judge ``where: local`` (self-heal t_01a1cfae6c9413c8). None for any other card. [daveey]"""
    import json
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_hub_admission_placement import denied_placement

    verdict = marker_placement.get("hub_admission")
    if not isinstance(verdict, dict) or verdict.get("admit") is not False:
        return None
    # runs_on is a Board column: an unsynced board has none (its denied cards are worker-first).
    cols = "runs_on, placement_policy" if "runs_on" in sync.table_columns(conn, "tasks") else "NULL, placement_policy"
    row = conn.execute(f"SELECT {cols} FROM tasks WHERE id = ?", (task_id,)).fetchone()
    report = denied_placement(verdict, runs_on=json.loads(row[0]) if row and row[0] else None,
                              placement_policy=row[1] if row else None)
    if report is None:
        return None
    note = report.pop("note")
    if marker_placement.get("requires") is not None:
        report["requires"] = marker_placement["requires"]
    return {"placement": report, "note": note}


def _marker_placement_fields(conn, new_tid: str, marker_placement: Optional[dict], session_id: Optional[str],
                             origin_profile: Optional[str] = None, origin_gateway: Optional[str] = None) -> dict:
    """Result fields for a card a placement marker kept here; a deferred card records its forward. [daveey]"""
    if not marker_placement:
        return {}
    if denied := _denied_fields(conn, new_tid, marker_placement):
        return denied
    if marker_placement.get("defer"):
        # Run on the routed gateway once the parents finish (kanban_placement_forward).
        from hermes_cli import kanban_placement_forward as _kpf
        from hermes_cli.profiles import current_profile_name as _cpn
        _kpf.record_deferral(conn, new_tid, target=marker_placement["defer"], origin_session=session_id,
                             origin_profile=origin_profile or _cpn("default") or "default",
                             origin_gateway=origin_gateway)
        return {"placement": {"where": "local", "defer": marker_placement["defer"], "why": marker_placement["why"]},
                "note": (f"Created here as {new_tid} so it can wait on its parents; it runs on "
                         f"{marker_placement['defer']}, not here: when the parents finish the dispatcher "
                         f"forwards it there and this card completes when that one does.")}
    if (marker_placement.get("runs_on") or marker_placement.get("placement_policy")
            or marker_placement.get("hub_admission")):
        enrolled = {"placement": dict(marker_placement)}
        if marker_placement.get("note"):
            enrolled["note"] = marker_placement["note"]  # a pool worker's unmet local marker (queue_decision)
        return enrolled
    out: dict = {"placement": {"where": marker_placement["where"], "why": marker_placement["why"]}}
    if marker_placement.get("remedy"):
        out["remedy"] = marker_placement["remedy"]
    if marker_placement.get("note"):
        out["note"] = marker_placement["note"]  # a local pin on a card with no local need
        if marker_placement["why"].startswith("home gateway marker"):
            out["placement"]["note"] = marker_placement["note"]  # a pool worker's home-marker keep
    return out


def _machine_placement(machine: str, runs_on: list[str]) -> dict:
    """Preserve the machine pin's reason when replacing the rewritten local-pin decision. [daveey]"""
    from hermes_cli.kanban_placement import pinned_machine_why

    why = pinned_machine_why(machine)
    logger.info("kanban placement: local, runs_on %s (%s)", runs_on, why)
    return {"where": "local", "runs_on": runs_on, "machine": machine, "why": why}


def _pin_drain_note(conn, task_id: str):
    """One sentence when every machine the new card can run on is draining for a core update; else None.

    The create reply promised "it runs on hub" while the hub's dispatcher held all claims for a drain, and the
    caller archived the card as lost (t_01a1cca8ed755919). Never raises. [daveey]"""
    try:
        from hermes_cli import kanban_queue_position as kqp
        place = kqp.placement(conn, task_id)
        if place is None or place.get("lane") != "machine" or not place.get("draining"):
            return None
        if not set(place["draining"]) >= set(place.get("machines") or []):
            return None
        return (kqp.headline(place) + ". It starts when the drain ends; `kanban_show` reports the wait. "
                "Re-place it only if it cannot wait.")
    except Exception:
        logger.debug("drain note for %s failed", task_id, exc_info=True)
        return None


def _pinned_note(conn, task_id: str, machine: str, runs_on: list[str]) -> str:
    """The kanban_create note for a card pinned to another machine's lease, naming a drain that holds it. [daveey]"""
    note = (f"Created here as {task_id}; it runs on {machine} via the board lease "
            f"(runs_on={runs_on}). The pin was honoured: do not re-create it.")
    if drain := _pin_drain_note(conn, task_id):  # the target can't claim it yet (t_01a1cca8ed755919)
        note += " " + drain
    return note


def _runs_where(runs_on, pinned_machine, marker_placement) -> str:
    """Where a card created on this board runs, for the kanban_create result. Never raises."""
    if marker_placement and marker_placement.get("placement_policy"):
        return "worker pool (waiting_for_worker_capacity; no local fallback)"
    if runs_on is not None:
        if pinned_machine:
            return f"{pinned_machine} via the board lease (runs_on={runs_on})"
        if ((marker_placement or {}).get("hub_admission") or {}).get("admit") is False:
            return f"worker pool: a machine matching runs_on={runs_on} via the board lease (hub admission denied)"
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
