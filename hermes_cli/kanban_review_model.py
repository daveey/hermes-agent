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
    """Concrete author models for the current implementation epoch.

    Read each run independently: a live worker result supersedes its launch receipt.
    A real review separates epochs only after new concrete implementation evidence;
    review-only retries and missing receipts preserve the previous authors.
    Related source cards are traversed with a visited set to tolerate cyclic links.
    """
    task = getattr(task, "_routing_source", task)
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_review_sources import source_reader
    seen = set()

    def collect(conn, card, bounded=True):
        if card.id in seen:
            return set()
        seen.add(card.id)
        downstream = _downstream(card)
        stored = kb.get_task(conn, card.id)
        skilled = "sdlc-review" in (getattr(stored, "skills", None) or [])
        models = set()
        pin = _conservative_pin(conn, card)
        boundary = False
        if not downstream and not skilled:
            models, boundary = _current_authors(conn, card.id, bounded)
        # Preserve conservative pin provenance on the first epoch and when the
        # current epoch has no concrete receipt; an old pin is not a new author.
        if pin and not is_class_selector(pin) and not downstream and (not boundary or not models):
            models.add(pin)
        if downstream:
            # Declared provenance counts as evidence of its own: the change came from outside the board.
            models.update(declared_implementers(getattr(stored, "body", None), getattr(stored, "assignee", None)))
            created = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='created' "
                                   "ORDER BY id LIMIT 1", (card.id,)).fetchone()
            creator = _payload(created[0] if created else None).get("creator_task_id")
            sources = lookup.children(card.id)
            if isinstance(creator, str) and creator:
                sources.append(creator)
            for source in sources:
                source_conn, related = lookup(source)
                # The exact-head diff holds every epoch's commits: keep full history.
                models.update(collect(source_conn, related, bounded=False))
        return models

    with source_reader(conn) as lookup:
        return collect(conn, task)


def _conservative_pin(conn, card):
    """Retired policy remains diagnostic evidence, under the same epoch rules.

    A subsequent explicit class policy supersedes this fallback. Only the latest
    clear is relevant, just as replacing a stored pin never accumulated authors.
    """
    pin = str(getattr(card, "model_override", None) or "").strip()
    if pin:
        return pin
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='model_override_cleared' "
                       "ORDER BY id DESC LIMIT 1", (card.id,)).fetchone()
    payload = _payload(row[0] if row else None)
    previous = payload.get("previous")
    if payload.get("reason") != "model_policy_no_pins" or not isinstance(previous, dict):
        return ""
    value = previous.get("model_override")
    return value.strip() if isinstance(value, str) else ""


def _current_authors(conn, task_id, bounded=True):
    from hermes_cli.kanban_review_artifacts import artifact_author_runs
    delivered = artifact_author_runs(conn, task_id) if bounded else None
    if delivered is None:
        return _legacy_current_authors(conn, task_id, bounded)
    models = {model for rid in delivered
              if (model := (_run_model(conn, task_id, rid, "worker_result", "route_model")
                            or _run_model(conn, task_id, rid, "run_route", "model")))}
    return models, False


def _legacy_current_authors(conn, task_id, bounded=True):
    """Authors since the review preceding the latest implementation, plus epoch boundary.

    Consecutive review retries keep the same author set. A pre-spawn refusal is
    not a review boundary: nobody inspected the head. Run IDs are local and read
    on the same connection, including when source_reader selects a replica.
    """
    from hermes_cli.kanban_review_retry import rehandoff_runs
    repeats = rehandoff_runs(conn, task_id)
    models = set()
    unknown_author = False
    for row in conn.execute("SELECT id, outcome FROM task_runs WHERE task_id=? ORDER BY id DESC",
                            (task_id,)).fetchall():
        events = conn.execute("SELECT kind, payload FROM task_events WHERE task_id=? AND run_id=? "
                              "ORDER BY id DESC", (task_id, row[0])).fetchall()
        # Dispatch claims before resolving a model. The claim already records
        # its source lane, even if resolution refuses before any route receipt.
        reviewing = any(
            (event[0] in ("model_tier", "run_route") and _payload(event[1]).get("lane") == "review")
            or (event[0] == "claimed" and _payload(event[1]).get("source_status") == "review")
            for event in events)

        if reviewing:
            # Launch/result receipts prove execution, not that anyone judged the
            # head. Parked and crashed reviewers must not release its authors.
            verdict = row[1] in ("changes_requested", "completed")
            if bounded and verdict and models and not unknown_author:
                return models, True
            continue
        if row[0] in repeats:
            continue
        model = (_run_model(conn, task_id, row[0], "worker_result", "route_model")
                 or _run_model(conn, task_id, row[0], "run_route", "model"))
        if model:
            models.add(model)
        else:
            # An unknown author anywhere in this epoch retains earlier evidence
            # and the concrete pin, even alongside known current authors.
            unknown_author = True
    return models, False


def is_review_run(conn, task, lane) -> bool:
    return (lane == "review" or "sdlc-review" in (getattr(task, "skills", None) or [])
            or _downstream(task))


def validate_pace_offer(model, excluded):
    """Compatibility hook: model diversity is not an offer-admission criterion."""
    # Compatibility hook: equality no longer refuses an offer. Expiry, provider
    # holds and mandatory pins are still checked by the owning callers.


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
# registered model instead of spawning a worker that dies at startup.
OVERRIDE_REFUSED = "review_override_refused"


class ReviewModelUnavailable(ModelRoutingError):
    """No eligible reviewer route is configured."""


class ReviewRouteDeferred(ReviewModelUnavailable):
    """Independent reviewers exist, but this host must wait for a known deadline."""

    def __init__(self, detail, until):
        super().__init__(detail)
        self.until = until


class ReviewModelsExhausted(ReviewModelUnavailable):
    """No configured reviewer candidate exists; retrying cannot add a route."""


def resolution_error(exc):
    """Keep deadline and structural-exhaustion refusals typed through the tier's error wrapper."""
    if isinstance(exc, ReviewRouteDeferred):
        return exc
    if isinstance(exc, ReviewModelsExhausted):
        return ReviewModelsExhausted(f"Review model resolution failed: {exc}")
    return ReviewModelUnavailable(f"Review model resolution failed: {exc}")


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
    from hermes_cli.kanban_model_neutral import validate_review
    metadata = _handoff(conn, task)[1]
    # Historical concrete overrides are evidence, not future dispatch authority.
    from agent.model_routing import is_class_selector
    if not is_class_selector(str(metadata.get("review_model") or "")):
        return None
    return validate_review({"review_model": metadata["review_model"], "review_provider": "auto"})


def validate_review_override(metadata):
    """Validate new review routing requirements before ending the author run."""
    from hermes_cli.kanban_model_neutral import validate_review
    if not isinstance(metadata, dict):
        raise ReviewModelUnavailable('Review handoff metadata must be a mapping')
    try:
        return validate_review(metadata)
    except ValueError as exc:
        raise ReviewModelUnavailable(str(exc)) from exc


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
    if isinstance(exc, ReviewRouteDeferred):
        return True, False, None  # a wall appearing after preflight never exhausts reviewer admission
    if isinstance(exc, ReviewModelsExhausted):
        return False, True, {"admission_refusal": "review_models_exhausted", "infrastructure": True}
    if isinstance(exc, ReviewModelUnavailable) and deferral_exhausted(conn, task_id, str(exc)):
        return False, True, {"admission_refusal": "review_model_unavailable", "deferrals": DEFERRAL_LIMIT}
    from hermes_cli.kanban_pace_reroute import OfferLost  # a lapsed pace move: requeue for a fresh judgment
    return isinstance(exc, (RestartSafeScopeUnavailable, ReviewModelUnavailable, OfferLost)), False, None


def independent_routes(pin, cfg, assignee, *, level="deep"):
    """Eligible reviewer routes at *level*, with concrete strong first for deep.

    The legacy pin/author-set argument is ignored; attribution is diagnostic only.
    Read-only; an unreadable routing policy contributes no policy candidates.
    """
    # Model diversity and concrete strong-model config are retired. Registry
    # class/capability eligibility alone orders candidates, including same-model reviews.
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
    for route in eligible_routes(policy, level, class_requirements(config), eligible=lambda r: is_provider_enabled(
            providers.get(r["provider"], {}))):
        yield route["model"], route["provider"]


def resolve_review_model(conn, task, cfg):
    """Select a real review route without requiring different model provenance."""
    import math
    import time
    from hermes_cli import kanban_provider_walls as pw
    # Separate execution is enforced by verdict custody, not model identity.
    override = _override(conn, task)
    assignee = getattr(task, "assignee", None)
    refusal = registry_refusal(override[0], override[1], assignee) if override else None
    class_override = bool(override and is_class_selector(override[0]))
    if override and class_override and not refusal:
        from hermes_cli.kanban_model_tier import class_unroutable_reason
        from hermes_cli.kanban_model_assignment import _assignee_config
        why = class_unroutable_reason(override[0], _assignee_config(assignee) or {})
        if why:
            raise ReviewModelUnavailable(f"Review model {override[0]} is unroutable: {why}")
    # Model attribution is audit-only; distinct execution is enforced by verdict custody.
    from hermes_cli.kanban_llm_pacing_dispatch import held_pools
    now = time.time()
    walls = pw.active_walls(now=now)
    holds = held_pools(cfg.get("llm_pacing"), now)
    waits = []

    def _unavailable(model, provider):
        norm = pw._norm(provider)
        if not norm or norm == "auto":
            return True
        live_wall = pw.live_quota_wall(norm, model)
        sources = (walls.get(norm), holds.get(norm), live_wall)
        def finite_future(value):
            return (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) and value > now)
        # Pool membership still refuses the route when its reset is unknown or
        # elapsed. A second finite wall cannot prove when that hold will clear.
        if norm in holds and not finite_future(holds[norm]):
            return True
        if live_wall is True or (isinstance(live_wall, (int, float)) and not math.isfinite(live_wall)):
            return True
        deadlines = [value for value in sources if finite_future(value)]
        if deadlines:
            if registry_refusal(model, provider, assignee) is None:
                waits.append(max(deadlines))  # a route needs ALL its overlapping walls/holds to clear
            return True
        return False

    # Honor a concrete handoff override unless its pool is held or walled.
    override_moved = False
    if override and refusal is None and not class_override:
        if not _unavailable(override[0], override[1]):
            return override
        override_moved = True
    # Historical reviewer pins are audit evidence, not candidates or mandates.
    # Authorship is diagnostic; separate execution is enforced by verdict custody.
    candidates = independent_routes(set(), cfg, assignee)
    independent_candidate = False
    for model, provider in candidates:
        independent_candidate = True
        if _unavailable(model, provider) or (override and registry_refusal(model, provider, assignee)):
            continue
        reason = (OVERRIDE_REFUSED if refusal else "review_override" if class_override or override_moved
                  else "review_unpinned")
        return model, provider, reason
    # Refused overrides retain their existing registered-deep-only recovery contract.
    for model, provider in (() if refusal else independent_routes(set(), cfg, assignee, level="standard")):
        independent_candidate = True
        if _unavailable(model, provider) or (override and registry_refusal(model, provider, assignee)):
            continue
        return model, provider, "review_tier_fallback"
    levels = "deep" if refusal else "deep or standard"
    detail = f"No eligible {levels} review route"
    if refusal and override:
        detail = f"review_model {override[0]!r} is refused here ({refusal.code}); {detail}"
    if not independent_candidate and not override and not _downstream(task):
        # Keep the normal profile/class tier route when no concrete review policy
        # is configured. Missing attribution must not disable legacy allocation.
        from hermes_cli.kanban_model_assignment import _assignee_config
        policy = (_assignee_config(assignee) or {}).get("model_routing") or {}
        if not policy.get("enabled", False):
            return None
    _raise_no_route(detail, waits, independent_candidate or override_moved)


def _raise_no_route(detail, waits, had_candidate):
    """Typed refusal: a deadline defers, a structurally empty set exhausts, anything else retries."""
    if waits:
        raise ReviewRouteDeferred(detail, min(waits))
    if not had_candidate:
        raise ReviewModelsExhausted(f"{detail}; infrastructure blocker: configure an independent reviewer")
    raise ReviewModelUnavailable(detail)
