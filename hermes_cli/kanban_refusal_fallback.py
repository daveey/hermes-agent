"""A provider safety refusal retries the same card, unchanged, on another provider of its class. [daveey]

David, 2026-11-23 ("automatic retries"): a verified provider safety refusal used to send the card to triage with a
``cause: safety_refusal`` restriction no ordinary release clears. Another provider of the same class may well
accept the same work, so the refusal now stops only the provider that gave it:

1. The refusing run's concrete provider is its worker_result ``route_provider`` (a class selector's worker
   allocates the provider), else its ``run_route`` receipt's provider. A card's *refused set* is the providers of
   all its ``safety_refused`` runs (``task_runs.metadata.refused_provider``, else re-derived from the run's own
   receipts, so a Board-closed run needs no extra field). Nothing new is stored.
2. :func:`plan` at the refusal: while a provider of the card's class level is eligible (enabled, serving on this
   host's live pool, not walled: the checks ratio/paced reroute use) and not refused, the run still ends
   ``safety_refused`` (kept), ``safety_refusal_fallback`` is recorded and the card returns to ``ready`` with NO
   safety restriction (:func:`kanban_refusal_receipt.restricted` stays false).
3. :func:`reroute` (inside ``resolve_dispatch_tier``): a card with a refused set never starts on a refused
   provider; it moves to an eligible peer, tier_reason ``<reason>+refusal:<providers>``. :func:`admission`
   fails closed before the claim when none is left: today's safety triage, ``refused_providers`` listed.
4. Everything else is as before: no rewording, no injected note, never the same provider twice, a refusal whose
   provider is unknown (or a review run) is stopped exactly as before, and non-refusal failures are untouched.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

EVENT = "safety_refusal_fallback"
REASON = "refusal"
EXHAUSTED = "refusal_exhausted"


def _norm(provider: Any) -> str:
    return str(provider or "").strip().lower()


def _concrete(provider: Any) -> str:
    value = _norm(provider)
    return "" if value in ("", "auto", "none") else value


def _payload(raw: Any) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def run_provider(conn, task_id: str, run_id: Optional[int], result: Optional[dict] = None) -> str:
    """The concrete provider that ran *run_id* ('' = unknown): its worker_result route, else its run_route."""
    if run_id is None:
        return ""
    if result and _concrete(result.get("route_provider")):
        return _concrete(result["route_provider"])
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='worker_result' "
                       "ORDER BY id DESC", (task_id, run_id)).fetchall()
    for (raw,) in row:
        if (found := _concrete(_payload(raw).get("route_provider"))):
            return found
    for (raw,) in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='run_route' "
                               "ORDER BY id DESC", (task_id, run_id)):
        return _concrete(_payload(raw).get("provider"))
    return ""


def refused_providers(conn, task_id: str) -> list[str]:
    """Providers of this card's ``safety_refused`` runs, oldest first, no repeats."""
    out: list[str] = []
    for run_id, raw in conn.execute("SELECT id, metadata FROM task_runs WHERE task_id=? AND outcome='safety_refused' "
                                    "ORDER BY id", (task_id,)).fetchall():
        provider = _concrete(_payload(raw).get("refused_provider")) or run_provider(conn, task_id, run_id)
        if provider and provider not in out:
            out.append(provider)
    return out


def _class_model(conn, task, lane: str) -> str:
    """The model whose class level bounds the card's peers: a concrete pin's own level, else the routed class."""
    from agent.model_routing import is_class_selector
    from hermes_cli.kanban_model_neutral import routing_view
    from hermes_cli.kanban_model_tier import resolve_model_tier
    raw = getattr(task, "_routing_source", task)
    pin = str(getattr(raw, "model_override", None) or "").strip()
    if pin and not is_class_selector(pin):
        return pin
    return str(resolve_model_tier(conn, routing_view(task), lane=lane)[0] or "")


def eligible_routes(conn, task, lane: str = "ready") -> list[tuple[str, str]]:
    """``[(model, provider)]`` of the card's class level (see :func:`class_peers`). Read-only."""
    return class_peers(task, _class_model(conn, task, lane))


def class_peers(task, model: str) -> list[tuple[str, str]]:
    """``[(model, provider)]`` of ``model``'s class level, registry order: enabled, serving on this host's live
    pool and not walled (``kanban_ratio_routing`` / ``kanban_paced_reroute`` eligibility). ``task`` supplies the
    assignee whose routing policy applies. Shared with the subagent retry (``delegate_tool_refusal``)."""
    from hermes_cli import kanban_provider_walls as pw
    from hermes_cli.kanban_ratio_routing import _pool_routes
    walled = pw.active_walls(now=time.time())
    out: list[tuple[str, str]] = []
    for pool, models in _pool_routes(task, model):
        for candidate in models:
            if (not pw.wall_until(walled, pool, candidate) and not pw.live_quota_wall(pool, candidate)
                    and pw.live_pool_serving(pool, candidate)):
                out.append((candidate, pool))
                break
    return out


def _next_route(conn, task, refused: list[str], lane: str = "ready") -> Optional[tuple[str, str]]:
    try:
        return next(((m, p) for m, p in eligible_routes(conn, task, lane) if p not in refused), None)
    except Exception:  # health: allow BLE001 -- an unreadable class policy means no proven peer: fail closed
        logger.warning("kanban refusal fallback: eligibility unreadable for %s", getattr(task, "id", "?"), exc_info=True)
        return None


def _run_lane(conn, task_id: str, run_id: Optional[int]) -> str:
    for (raw,) in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='run_route' "
                               "ORDER BY id DESC", (task_id, run_id)):
        return str(_payload(raw).get("lane") or "ready")
    return "ready"


def plan(conn, row, result: dict) -> dict:
    """The decision for a verified refusal of ``row``'s current run: ``{refused_provider, refused_providers, next}``.

    ``next`` is ``(model, provider)`` only when the refusing provider is proven and an unrefused peer is eligible;
    anything else keeps today's stop. Read-only."""
    from hermes_cli import kanban_db as kb
    task_id, run_id = row["id"], row["current_run_id"]
    provider = run_provider(conn, task_id, run_id, result)
    refused = refused_providers(conn, task_id)
    if provider and provider not in refused:
        refused.append(provider)
    out: dict = {"refused_provider": provider or None, "refused_providers": refused, "next": None}
    task = kb.get_task(conn, task_id)
    if not provider or task is None:
        return out
    lane = _run_lane(conn, task_id, run_id)
    from hermes_cli.kanban_review_model import is_review_run
    if is_review_run(conn, task, lane):
        return out  # an independent reviewer is chosen by the review resolver, not by class peers
    out["next"] = _next_route(conn, task, refused, lane)
    return out


def fallback_message(refused: str, nxt: str) -> str:
    return f"Provider {refused} refused; retrying unchanged on {nxt}"


def fallback_payload(decision: dict, payload: dict, run_uid: Optional[str]) -> dict:
    model, provider = decision["next"]
    out = {"refused_provider": decision["refused_provider"], "refused_providers": decision["refused_providers"],
           "next_provider": provider, "next_model": model,
           "message": fallback_message(decision["refused_provider"], provider)}
    for key in ("source_result_event_id", "source_result_event_uid"):
        if payload.get(key) is not None:
            out[key] = payload[key]
    if run_uid:
        out["run_uid"] = run_uid
    return out


def requeue(conn, row, decision: dict, payload: dict) -> None:
    """Local board: end the refused run (record kept) and make the card ready, unchanged and unrestricted.

    Called inside the reclaim transaction after the run's CAS has been revalidated by the caller."""
    from hermes_cli import kanban_db as kb
    task, run = row["id"], row["current_run_id"]
    from hermes_cli import kanban_db_sync as sync
    event = fallback_payload(decision, payload, sync.run_uid(conn, run))  # None on an unsynced board
    meta = {**payload, "refused_provider": decision["refused_provider"],
            "refused_providers": decision["refused_providers"], "fallback_provider": event["next_provider"],
            "reason": event["message"]}
    kb._end_run(conn, task, outcome="safety_refused", status="safety_refused", error=event["message"], metadata=meta)
    kb._append_event(conn, task, "safety_refused", meta, run_id=run)
    kb._append_event(conn, task, EVENT, event, run_id=run)


def _triage(conn, task_id: str, run_id: Optional[int], payload: dict) -> None:
    """Today's safety stop events (shared by the reclaim path and the claim-time fail-closed path)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_spend_decision as decision
    if (stop := decision.pending_episode(conn, task_id)) is not None:
        payload["episode"] = stop.get("uid") or str(stop["id"])
        kb._append_event(conn, task_id, "decision_hold_restricted", payload, run_id=run_id)
    else:
        kb._append_event(conn, task_id, decision.EVENT, payload, run_id=run_id)
    kb._append_event(conn, task_id, "safety_refused", payload, run_id=run_id)
    decision.safe_ensure_producer(conn, task_id)


def admission(conn, task_id: str, result, *, dry_run: bool) -> bool:
    """Pre-claim: True when a card with a refused set has no unrefused eligible provider left.

    A local board is then stopped as today (triage + safety restriction, ``refused_providers`` listed); a
    primary board's rows belong to the Board, so the card is only refused here (not claimed)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_refusal_receipt import restricted
    refused = refused_providers(conn, task_id)
    if not refused:
        return False
    task = kb.get_task(conn, task_id)
    if task is None or restricted(conn, task_id):
        return False
    from hermes_cli.kanban_review_model import is_review_run
    if is_review_run(conn, task, "ready") or _next_route(conn, task, refused) is not None:
        return False
    result.skipped_ineligible.append((task_id, EXHAUSTED))
    if dry_run or sync.board_mode(conn) == "primary":
        return True
    with kb.write_txn(conn):
        changed = conn.execute("UPDATE tasks SET status='triage', claim_lock=NULL, claim_expires=NULL, "
                               "last_failure_error='Provider safety refusal; every eligible provider refused' "
                               "WHERE id=? AND status='ready' AND current_run_id IS NULL", (task_id,)).rowcount
        if changed:
            last = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='safety_refused' "
                                "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
            payload = {**_payload(last[0] if last else None), "cause": "safety_refusal", "accounting_boundary": False,
                       "reason": "Provider safety refusal; every eligible provider refused",
                       "release_authority": "unavailable", "refused_providers": refused,
                       "next_step": "Owning supervisor must resolve the scope/authority boundary; "
                                    "do not rerun refused work"}
            _triage(conn, task_id, None, payload)
    return True


def reroute(conn, task, route: tuple, *, lane: str) -> tuple:
    """``route`` (model, provider, reason) moved off every refused provider (module rule 3); unchanged otherwise.

    A class selector's worker would allocate the provider itself, so a card with a refused set always gets a
    concrete unrefused peer. No peer left: the reason gains ``+refusal_exhausted`` (admission already fails closed
    before the claim; the spawn path refuses on it)."""
    from hermes_cli import kanban_provider_walls as pw
    from agent.model_routing import is_class_selector
    task_id = getattr(task, "id", None)
    if not task_id:
        return route
    refused = refused_providers(conn, task_id)
    if not refused:
        return route
    model, provider, reason = route
    from hermes_cli.kanban_review_model import is_review_run
    if is_review_run(conn, task, lane):
        return route
    concrete = _concrete(provider) if model and not is_class_selector(model) else ""
    if concrete and concrete not in refused:
        return route
    peers = _next_route(conn, task, refused, lane)
    tag = ",".join(refused)
    if peers is None:
        return model, provider, f"{reason}+{EXHAUSTED}:{tag}"
    return peers[0], pw._norm(peers[1]), f"{reason}+{REASON}:{tag}"
