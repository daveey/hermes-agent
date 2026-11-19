"""Review-only model selection; implementation pins are not reviewer pins. [daveey]"""
from __future__ import annotations

import json
import re
from typing import Any

from agent.model_routing import ModelRoutingError, eligible_routes, is_class_selector, validate_policy

_DOWNSTREAM_REVIEW = re.compile(r"\A\W*independent\b[^\n]*\breview\b", re.IGNORECASE)
# [daveey] A downstream review card's own declaration of who implemented the change, for work no Kanban card wrote
# (a Claude Code session landing through ``hermes-core-wt ship``): one line ``<!-- implementer-models: a, b -->``
# in the card body, written by the filing tool from the commits' Co-Authored-By trailers. Concrete model ids only.
_DECLARED = re.compile(r"^<!-- implementer-models: ([^>\n]*) -->\r?$", re.MULTILINE)
_MODEL_ID = re.compile(r"[a-z0-9][a-z0-9._-]{1,79}")


def declared_implementers(body, assignee=None) -> set[str]:
    """Models a review card's body declares as the change's implementers (``_DECLARED``). [daveey]

    Void (an empty set, as if absent) when any entry on any marker line is malformed (``_MODEL_ID``: no class
    selector, no space or slash), is not a route model of the assignee's enabled routing policy
    (``_routable_models``: positive membership, so a host with no policy accepts no declaration), or is refused by
    the managed registry (``registry_refusal``). A name that excludes no real route must not lift the fail-closed
    refusal while the review could still run on the model that wrote the change (reviews 5971146857 and the Claude
    Code review of 3525a48bb8: ``registry_refusal`` alone refuses nothing on an unmanaged host)."""
    found: set[str] = set()
    for match in _DECLARED.finditer(body or ""):
        names = [name.strip() for name in match.group(1).split(",")]
        if any(not _MODEL_ID.fullmatch(name) for name in names):
            return set()
        found.update(names)
    if not found:
        return found
    known = _routable_models(assignee)
    if any(name not in known for name in found):
        return set()
    from hermes_cli.kanban_model_tier import infer_provider
    if any(registry_refusal(name, infer_provider(name), assignee) is not None for name in found):
        return set()
    return found


def _routable_models(assignee) -> set[str]:
    """Every route model of the enabled routing policy a worker for *assignee* on this host reads (any level); empty
    when there is none or it cannot be read, which voids any declaration (``declared_implementers``). [daveey]"""
    from hermes_cli.kanban_model_assignment import _assignee_config
    try:
        config = _assignee_config(assignee) or {}
        policy = config.get("model_routing") or {}
        validate_policy(policy)
    except Exception:  # health: allow BLE001 -- [daveey] an unreadable routing config names no model: the declaration is void, the review fails closed
        return set()
    if not policy.get("enabled", False):
        return set()
    return {str(route.get("model") or "") for routes in (policy.get("routes") or {}).values()
            for route in (routes or []) if isinstance(route, dict)} - {""}


def _downstream(task):
    return bool(_DOWNSTREAM_REVIEW.search(getattr(task, "title", "") or ""))


def _payload(raw):
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def implementer_models(conn, task) -> set[str]:
    """All concrete author models, never a downstream review card's reviewer pin.

    Read each run independently: a live worker result supersedes its launch receipt.
    Related source cards are traversed with a visited set to tolerate cyclic links.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_review_sources import source_reader
    seen = set()

    def collect(conn, card):
        if card.id in seen:
            return set()
        seen.add(card.id)
        downstream = _downstream(card)
        stored = kb.get_task(conn, card.id)
        skilled = "sdlc-review" in (getattr(stored, "skills", None) or [])
        models = set()
        pin = str(getattr(card, "model_override", None) or "").strip()
        if pin and not is_class_selector(pin) and not downstream:
            models.add(pin)
        if not downstream and not skilled:
            for row in conn.execute("SELECT id FROM task_runs WHERE task_id=?", (card.id,)).fetchall():
                events = conn.execute(
                    "SELECT kind, payload FROM task_events WHERE task_id=? AND run_id=? "
                    "AND kind IN ('model_tier', 'run_route') ORDER BY id DESC", (card.id, row[0])).fetchall()
                if any(_payload(event[1]).get("lane") == "review" for event in events):
                    continue
                model = (_run_model(conn, card.id, row[0], "worker_result", "route_model")
                         or _run_model(conn, card.id, row[0], "run_route", "model"))
                if model:
                    models.add(model)
        if downstream:
            # Declared provenance counts as evidence of its own: the change came from outside the board.
            models.update(declared_implementers(getattr(stored, "body", None), getattr(stored, "assignee", None)))
            created = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='created' "
                                   "ORDER BY id LIMIT 1", (card.id,)).fetchone()
            creator = _payload(created[0] if created else None).get("creator_task_id")
            sources = [row[0] for row in conn.execute(
                "SELECT child_id FROM task_links WHERE parent_id=?", (card.id,)).fetchall()]
            if isinstance(creator, str) and creator:
                sources.append(creator)
            for source in sources:
                source_conn, related = lookup(source)
                models.update(collect(source_conn, related))
        return models

    with source_reader(conn) as lookup:
        return collect(conn, task)


def is_review_run(conn, task, lane) -> bool:
    return (lane == "review" or "sdlc-review" in (getattr(task, "skills", None) or [])
            or _downstream(task))


def validate_pace_offer(model, excluded):
    """Refuse stale or unsafe offers before recording any reviewer route."""
    if excluded and (model in excluded or is_class_selector(model)):
        from hermes_cli.kanban_pace_reroute import OfferLost
        raise OfferLost("pace reroute would violate reviewer independence")


def mandatory_reviewer_pin(task) -> bool:
    """A downstream review card whose concrete pin is mandatory: its reviewer is that model or nothing.

    Every reroute (wall, quota episode, pacing hold, pace offer) must leave it; it is never moved silently."""
    pin = str(getattr(task, "model_override", None) or "").strip()
    if not (_downstream(task) and pin and not is_class_selector(pin)):
        return False
    from hermes_cli.kanban_pin_reroute import is_mandatory_pin
    return bool(is_mandatory_pin(task))


def redact_review_value(value: Any) -> Any:
    """Redact secrets at the domain boundary for durable review handoffs."""
    if isinstance(value, str):
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(value, force=True)
    if isinstance(value, dict):
        return {key: redact_review_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_review_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_review_value(item) for item in value)
    return value


# model_tier reason when this host's routing registry refuses the handoff's review_model: the review runs on a
# registered model distinct from the implementer instead of spawning a worker that dies at startup.
OVERRIDE_REFUSED = "review_override_refused"


class ReviewModelUnavailable(ModelRoutingError):
    """No independent reviewer is configured; do not spawn the implementer's model."""


def _handoff(conn, task):
    """``(run_id, metadata)`` of the latest review handoff, or ``(None, {})``."""
    # Metadata belongs to the handoff run, not to the Task row. Read only the
    # latest handoff so a subsequent review can deliberately remove an override.
    row = conn.execute(
        "SELECT id, metadata FROM task_runs WHERE task_id=? AND outcome='review_requested' "
        "ORDER BY id DESC LIMIT 1", (task.id,),
    ).fetchone()
    metadata = json.loads(row[1]) if row and row[1] else {}
    if not isinstance(metadata, dict):
        raise ReviewModelUnavailable("Review handoff metadata must be a mapping")
    return (row[0] if row else None), metadata


def _override(conn, task):
    return validate_review_override(_handoff(conn, task)[1])


def validate_review_override(metadata):
    """Shared ingress/dispatch contract, including legacy automatic class routing."""
    model = metadata.get("review_model")
    if model is None:
        return None
    if not isinstance(model, str) or not model.strip():
        raise ReviewModelUnavailable("review_model must be a nonempty model identifier")
    from hermes_cli.kanban_model_tier import infer_provider
    provider = metadata.get("review_provider")
    if provider is not None and (not isinstance(provider, str) or not provider.strip()):
        raise ReviewModelUnavailable("review_provider must be a nonempty provider identifier")
    model = model.strip()
    provider = (provider or ("auto" if is_class_selector(model) else infer_provider(model)))
    if is_class_selector(model):
        if provider not in ("auto", "board-default"):
            raise ReviewModelUnavailable("A review class requires provider auto")
        provider = "auto"  # Historical handoffs used the board-default sentinel.
    return model, provider, "review_override"


def registry_refusal(model, provider, assignee):
    """The worker's startup pin check (managed routing registry), run before spawn. [daveey]

    Returns the ``ModelPinNotInRegistryError`` a worker for *assignee* on this host would die of, else None."""
    from hermes_cli.kanban_model_assignment import ModelPinNotInRegistryError, _assignee_config, _check_managed_pin
    try:
        _check_managed_pin(model, (provider or "").strip() or None, _assignee_config(assignee))
    except ModelPinNotInRegistryError as exc:
        return exc
    return None


def refused_override(conn, task):
    """``{model, provider, code, allowed}`` when this host refuses the handoff's review_model, else None."""
    override = _override(conn, task)
    if override is None:
        return None
    refusal = registry_refusal(override[0], override[1], getattr(task, "assignee", None))
    if refusal is None:
        return None
    return {"model": override[0], "provider": override[1], "code": refusal.code, "allowed": refusal.allowed}


def _run_model(conn, task_id, run_id, kind, key):
    """Concrete model named by *run_id*'s latest *kind* event under *key*, else None."""
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind=? "
                       "ORDER BY id DESC LIMIT 1", (task_id, run_id, kind)).fetchone()
    try:
        payload = json.loads(row[0]) if row and row[0] else {}
    except (TypeError, ValueError):
        payload = {}
    model = str(payload.get(key) or "").strip() if isinstance(payload, dict) else ""
    return model if model and not is_class_selector(model) else None


def _implementer_model(conn, task):
    """Concrete model the implementer ran, most direct evidence first. [daveey]

    The handoff run's ``worker_result.route_model`` (the live agent's model: the only concrete name when the
    run was class-routed, e.g. ``kanban.strong_model: class:deep``), then its ``run_route`` receipt (under
    ``pinned+pin_quota_reroute`` that is the peer that ran, not the pin), and only then the card's concrete pin.
    Preferring the pin let ``_distinct_deep(pin)`` pick the rerouted implementer as its own reviewer."""
    run_id = _handoff(conn, task)[0]
    if run_id is not None:
        for kind, key in (("worker_result", "route_model"), ("run_route", "model")):
            model = _run_model(conn, task.id, run_id, kind, key)
            if model:
                return model
    if _downstream(task):
        return None
    pin = str(getattr(task, "model_override", None) or "").strip()
    return pin if pin and not is_class_selector(pin) else None


# Identical review-admission refusals in a row (spaced by ``infrastructure_cooldown``) before the card leaves
# the silent infrastructure-retry loop for a visible block. [daveey]
DEFERRAL_LIMIT = 3


def deferral_exhausted(conn, task_id, error):
    """True when *error* would be the ``DEFERRAL_LIMIT``-th identical ReviewModelUnavailable deferral in a row.

    Such a refusal is ``infrastructure`` (failures stay 0, the breaker never trips), so a cause that never
    clears (no concrete implementer receipt, no registered independent reviewer) deferred the card forever."""
    import sqlite3
    try:
        rows = conn.execute("SELECT outcome, error FROM task_runs WHERE task_id=? AND ended_at IS NOT NULL "
                            "ORDER BY id DESC LIMIT ?", (task_id, DEFERRAL_LIMIT - 1)).fetchall()
    except sqlite3.Error:
        return False  # unreadable history keeps the plain infrastructure deferral
    return len(rows) == DEFERRAL_LIMIT - 1 and all(
        row[0] == "spawn_failed" and (row[1] or "") == str(error)[:500] for row in rows)


def admission_refusal(conn, task_id, exc):
    """``(infrastructure, force_trip, gave_up extra)`` for a spawn that failed before any worker ran. [daveey]

    A host that cannot place the worker (no restart-safe scope) or an unavailable independent reviewer is
    infrastructure: retried with the budget untouched, until ``deferral_exhausted`` sends the review to a block."""
    from tools.process_registry import RestartSafeScopeUnavailable
    if isinstance(exc, ReviewModelUnavailable) and deferral_exhausted(conn, task_id, str(exc)):
        return False, True, {"admission_refusal": "review_model_unavailable", "deferrals": DEFERRAL_LIMIT}
    from hermes_cli.kanban_pace_reroute import OfferLost  # a lapsed pace move: requeue for a fresh judgment
    return isinstance(exc, (RestartSafeScopeUnavailable, ReviewModelUnavailable, OfferLost)), False, None


def independent_routes(pin, cfg, assignee):
    """Concrete strong tier followed by eligible deep routes, excluding every author model.

    Accept a single model for legacy callers, or the complete implementer set.
    Read-only; an unreadable routing policy contributes no deep candidates.
    """
    excluded = {pin} if isinstance(pin, str) else set(pin or ())
    strong = str(cfg.get("strong_model") or "").strip()
    if strong and strong not in excluded and not is_class_selector(strong):
        from hermes_cli.kanban_model_tier import infer_provider
        yield strong, str(cfg.get("strong_provider") or "").strip() or infer_provider(strong)
    from agent.model_routing_agent import class_requirements
    from hermes_cli.config import is_provider_enabled
    from hermes_cli.kanban_model_assignment import _assignee_config
    config = _assignee_config(assignee) or {}
    policy = config.get("model_routing") or {}
    try:
        validate_policy(policy)
    except ModelRoutingError:  # no provable alternate; the caller keeps its route
        return
    if not policy.get("enabled", False):
        return
    providers = config.get("providers") or {}
    for route in eligible_routes(policy, "deep", class_requirements(config), eligible=lambda r: is_provider_enabled(
            providers.get(r["provider"], {}))):
        if route["model"] not in excluded:
            yield route["model"], route["provider"]


def resolve_review_model(conn, task, cfg):
    """Concrete independent reviewer when author evidence exists; legacy routing otherwise."""
    import time
    from hermes_cli import kanban_provider_walls as pw
    excluded = implementer_models(conn, task)
    override = _override(conn, task)
    assignee = getattr(task, "assignee", None)
    refusal = registry_refusal(override[0], override[1], assignee) if override else None
    class_override = bool(override and is_class_selector(override[0]))
    if override and class_override and excluded and not refusal:
        from hermes_cli.kanban_model_tier import class_unroutable_reason
        from hermes_cli.kanban_model_assignment import _assignee_config
        why = class_unroutable_reason(override[0], _assignee_config(assignee) or {})
        if why:
            raise ReviewModelUnavailable(f"Review model {override[0]} is unroutable: {why}")
    if not excluded:
        if _downstream(task):
            raise ReviewModelUnavailable("Downstream review implementer evidence is unavailable")
        # Legacy same-card reviews may have an explicit handoff but no recorded author.
        if override and refusal is None:
            return override
        if refusal and override:
            raise ReviewModelUnavailable(
                f"review_model {override[0]!r} is refused here ({refusal.code}) and the implementer's model is unknown")
        if str(getattr(task, "model_override", None) or "").strip():
            raise ReviewModelUnavailable("A class pin does not identify the implementer; set review_model in the handoff")
        return None
    from hermes_cli.kanban_llm_pacing_dispatch import held_pools
    from hermes_cli.kanban_model_tier import infer_provider
    from hermes_cli.kanban_pin_reroute import is_mandatory_pin
    now = time.time()
    walled = {**pw.active_walls(now=now), **held_pools(cfg.get("llm_pacing"), now)}

    def _unavailable(model, provider):
        norm = pw._norm(provider)
        return not norm or norm == "auto" or norm in walled or bool(pw.live_quota_wall(norm, model))

    # A concrete, independent handoff override is honored unless its pool is held or walled (the fast return
    # used to skip that check and run the reviewer into the hold/wall): then the next independent route takes it.
    override_moved = False
    if override and refusal is None and not class_override and override[0] not in excluded:
        if not _unavailable(override[0], override[1]):
            return override
        override_moved = True
    # A downstream card's pin names its reviewer, not an author, so it never joins the author set. A
    # mandatory reviewer pin is never swapped for another model: it is used as-is when independent (a held or
    # walled pool is then the pool's/governor's wait, as for every mandatory pin) and refused when it authored.
    pin = str(getattr(task, "model_override", None) or "").strip()
    candidates = independent_routes(excluded, cfg, assignee)
    if _downstream(task) and pin and not is_class_selector(pin):
        pin_route = (pin, getattr(task, "provider_override", None) or infer_provider(pin))
        if is_mandatory_pin(task):
            if pin in excluded:
                raise ReviewModelUnavailable(
                    f"Mandatory reviewer pin {pin!r} is one of the implementer models {sorted(excluded)!r}")
            return (*pin_route, "review_unpinned")
        if pin not in excluded:
            from itertools import chain
            candidates = chain([pin_route], candidates)
    for model, provider in candidates:
        if _unavailable(model, provider) or (override and registry_refusal(model, provider, assignee)):
            continue
        reason = (OVERRIDE_REFUSED if refusal else "review_override" if class_override or override_moved
                  else "review_unpinned")
        return model, provider, reason
    detail = f"No eligible deep review model distinct from {sorted(excluded)!r}"
    if refusal and override:
        detail = f"review_model {override[0]!r} is refused here ({refusal.code}); {detail}"
    raise ReviewModelUnavailable(detail)
