"""Provider scope for shared-blocker observations, not for the task's future route."""
from __future__ import annotations

import re

FIELDS = ("blocker_provider", "blocker_model", "blocker_lane")
_WALL = re.compile(r"ModelRoutingError|\b(?:insufficient_quota|provider quota|quota exceeded|rate.limit(?:ed)?|usage limit)\b", re.IGNORECASE)
_PROVIDER = re.compile(r"\bprovider\s*[:=]\s*['\"]?([\w.-]+)", re.IGNORECASE)
_MODEL = re.compile(r"\bmodel\s*[:=]\s*['\"]?([\w./:-]+)", re.IGNORECASE)


def scope_fields(values):
    """Validate optional strings at the API boundary; blank means absent."""
    result = {}
    for key in FIELDS:
        value = values.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        if value and value.strip():
            result[key] = value.strip()
    return result


def blocker_metadata(conn, task_id, *, reason=None, metadata=None, blocker_id=None,
                     blocker_provider=None, blocker_model=None, blocker_lane=None):
    """Preserve explicit scope; infer only from an identified error or this run's error.

    A park's prose may describe a later reviewer/delegate's wall. Never assign the
    current run's provider merely because its checkpoint mentions a routing error.
    Only a stored error on this exact run can use its historical routing receipt.
    No current task pins, profile defaults, or past-run errors participate.
    """
    from hermes_cli.kanban_shared_blockers import normalize_blocker_id, _provider, _run_route

    meta = dict(metadata or {})
    explicit = scope_fields(dict(zip(FIELDS, (blocker_provider, blocker_model, blocker_lane))))
    scope = scope_fields(meta)
    scope.update(explicit)
    for key in FIELDS:
        meta.pop(key, None)
    prior_id = meta.pop("blocker_id", None)
    bid = normalize_blocker_id(blocker_id or prior_id)
    if not bid:
        return meta
    if meta.get("clear_blocker"):
        raise ValueError("clear_blocker cannot be combined with blocker_id")
    meta["blocker_id"] = bid
    if not scope.get("blocker_provider"):
        inferred = _infer_scope(conn, task_id, reason, _provider, _run_route)
        scope = {**inferred, **scope}
    meta.update(scope)
    return meta


def _error_scope(text, canon):
    # Provider-labelled error evidence can describe a different, later step.
    lines = "\n".join(line for line in (text or "").splitlines() if _WALL.search(line))
    providers = {canon(m) for m in _PROVIDER.findall(lines)} - {None}
    if len(providers) != 1:
        return {}
    result = {"blocker_provider": providers.pop()}
    models = set(_MODEL.findall(lines))
    if len(models) == 1:
        result["blocker_model"] = models.pop()
    return result


def _infer_scope(conn, task_id, reason, canon, route):
    observed = _error_scope(reason, canon)
    if observed:
        return observed
    row = conn.execute(
        "SELECT r.id, r.error FROM task_runs r JOIN tasks t ON t.current_run_id=r.id "
        "WHERE t.id=? AND r.task_id=t.id AND r.ended_at IS NULL", (task_id,)).fetchone()
    if row is None or not _WALL.search(row["error"] or ""):
        return {}
    observed = _error_scope(row["error"], canon)
    if observed:
        return observed
    if _PROVIDER.search(row["error"]):
        return {}  # ambiguous error identity must not fall back to the worker route
    model, provider, lane = route(conn, task_id, row["id"])
    if not provider:
        return {}
    result = {"blocker_provider": provider, "blocker_lane": lane}
    if model:
        result["blocker_model"] = model
    return result
