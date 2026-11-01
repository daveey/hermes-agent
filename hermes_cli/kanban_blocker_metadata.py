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
    """Preserve explicit scope; otherwise infer it only from provider-labelled wall lines in
    this park's own reason/checkpoint.

    The executor's identity (this run's receipt or stored error, the card's pins, profile
    defaults) is never borrowed: a delegated reviewer or later step can hit a wall on another
    provider, so an unlabelled wall stays unscoped (board-wide). ``conn``/``task_id`` stay in
    the signature for the callers.
    """
    from hermes_cli.kanban_shared_blockers import normalize_blocker_id, _provider

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
        scope = {**_error_scope(reason, _provider), **scope}
    meta.update(scope)
    return meta


def _error_scope(text, canon):
    """Scope named by wall lines (a wall marker AND a provider label on one line). Lines without
    a wall marker are prose; several providers are ambiguous; a label that is not a known MODEL
    provider (registry lookup, offline) names a service: all three give no scope (board-wide).
    Explicit ``blocker_provider`` metadata never passes through here."""
    lines = "\n".join(line for line in (text or "").splitlines() if _WALL.search(line))
    providers = {canon(m) for m in _PROVIDER.findall(lines)} - {None}
    if len(providers) != 1:
        return {}
    provider = providers.pop()
    if not _known_model_provider(provider):
        # e.g. "GitHub API rate limit exceeded (provider: github)": a service wall, not a model
        # route. Scoping to a provider no worker runs on would let every card past it.
        return {}
    result = {"blocker_provider": provider}
    models = set(_MODEL.findall(lines))
    if len(models) == 1:
        result["blocker_model"] = models.pop()
    return result


def _known_model_provider(provider):
    """Whether ``provider`` resolves in the model-provider registry (built-ins, overlays, plugin
    profiles, cached models.dev catalog; offline). Unknown or a lookup failure is False."""
    try:
        from hermes_cli.providers import get_provider
        return get_provider(provider, allow_network=False) is not None
    except Exception:
        return False
