"""Restriction-only classification of Kanban-native delegation lineage.

A managed Kanban worker (and every descendant it builds) may start NEW delegated work only on a logical
model class, never on a concrete model/provider pin, endpoint or transport override, and never with one of the
finite destination/auth names below in a request override (``_AUTH_HEADERS``, ``_AUTH_QUERY``).  That list is an
enumerated refusal, not a general credential detector; credentials the allocator resolves for the selected route
are not delegation overrides and are untouched.  This module
holds the pure logical checks and a process-local weak identity map from live agent objects to a restriction
token.  Nothing here grants authority: ``WorkerCustody.validated`` (creator rights, live lease) is untouched,
and the token is never serialized, read from the environment, or accepted from a request field.
"""
from __future__ import annotations

import os
import threading
import weakref
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Optional

from agent.model_routing import CLASSES


class RestrictionError(ValueError):
    """A new Kanban-origin delegation asked for something other than a logical class."""


class _Token:
    """Identity-only restriction marker shared by a native root and its descendants."""
    __slots__ = ()


@dataclass(frozen=True, eq=False)
class LogicalRequest:
    """What a new delegation logically asked for; dynamic allocation happens later, inside AIAgent."""
    model_class: str
    tuning: Mapping[str, Any]
    origin: _Token


_LOCK = threading.RLock()  # a weakref drop callback can run while a registration holds it
_ENTRIES: dict[int, tuple[weakref.ref, _Token]] = {}

_TRANSPORT_KEYS = ("base_url", "api_key", "api_mode", "command", "args", "acp_command", "acp_args")
_ROUTING_KEYS = frozenset({"model", "provider", *_TRANSPORT_KEYS, "models", "route", "fallbacks"})
_WIRE_CONTAINERS = ("extra_body", "extra_query")
_AUTH_HEADERS = frozenset({"authorization", "x-api-key", "api-key", "host", "x-goog-api-key", "cookie",
                           "proxy-authorization", "openai-organization"})  # matched case-insensitively
_AUTH_QUERY = frozenset({"key", "api-key"})  # extra_query only; api_key is already a routing key
_KEPT_TUNING = ("request_overrides", "reasoning_effort", "service_tier", "label")


def _registered(ident: int) -> bool:
    with _LOCK:
        return ident in _ENTRIES


def _lookup(agent: Any) -> Optional[_Token]:
    entry = _ENTRIES.get(id(agent))
    return entry[1] if entry is not None and entry[0]() is agent else None


def _put(agent: Any, token: _Token) -> None:
    ident = id(agent)

    def drop(ref, ident=ident):
        with _LOCK:
            if (entry := _ENTRIES.get(ident)) is not None and entry[0] is ref:
                del _ENTRIES[ident]
    try:
        _ENTRIES[ident] = (weakref.ref(agent, drop), token)
    except TypeError as exc:
        raise RestrictionError("invalid_delegation_parent") from exc


def register_root(agent: Any, original: Any) -> None:
    """Trusted seam, called right after ``WorkerCustody.bind_agent``: classify the exact bound agent."""
    from agent.origin_worker_custody import WorkerCustody
    bound = getattr(agent, "_native_origin_worker_custody", None)
    store = getattr(agent, "_session_db", None)
    if (type(original) is not WorkerCustody or type(bound) is not WorkerCustody or bound is original
            or original.agent is not None or bound.agent is not agent or store is None or bound.store is not store
            or bound._handoff is not original._handoff or not original._handoff.used
            or bound.session_id != original.session_id or getattr(agent, "session_id", None) != bound.session_id
            or bound.pid != os.getpid() or tuple(sorted(store.scope.items())) != bound.execution_scope):
        raise RestrictionError("invalid_worker_origin_registration")
    with _LOCK:
        if _lookup(agent) is not None:
            raise RestrictionError("duplicate_worker_origin_registration")
        _put(agent, _Token())


def register_child(parent: Any, child: Any, token: Any) -> None:
    """Classify a newly built descendant; ``token`` must be exactly the parent's registered token."""
    with _LOCK:
        if token is None or _lookup(parent) is not token or _lookup(child) is not None:
            raise RestrictionError("invalid_delegation_lineage")
        _put(child, token)


def origin_for(agent: Any) -> Optional[_Token]:
    """The agent's restriction token, or None for an unclassified (interactive/cron) parent.

    A natively bound worker with no registration is an invariant failure, never an interactive fallback."""
    with _LOCK:
        token = _lookup(agent)
    if token is not None:
        return token
    from agent.origin_worker_custody import WorkerCustody
    bound = getattr(agent, "_native_origin_worker_custody", None)
    if type(bound) is WorkerCustody and bound.agent is agent:
        raise RestrictionError("Kanban worker delegation lineage is unregistered; refusing new delegation.")
    return None


# ── pure logical checks ──────────────────────────────────────────────────────────────────────


def parent_class(parent: Any) -> str:
    meta = getattr(parent, "model_routing", None)
    level = meta.get("requested_class") if isinstance(meta, dict) else None
    return f"class:{level}" if level in CLASSES else "class:standard"


def is_class(model: Any) -> bool:
    return isinstance(model, str) and model.startswith("class:") and model[len("class:"):] in CLASSES


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, (str, list, tuple, dict)) and len(value) == 0)


def _where(index: Optional[int]) -> str:
    return "" if index is None else f"Task {index}: "


def _refuse(index: Optional[int], what: str) -> RestrictionError:
    return RestrictionError(
        f"{_where(index)}{what}. New delegations from a Kanban worker take a model class "
        f"(class:{'|class:'.join(sorted(CLASSES))}) with provider auto; concrete models, providers, endpoints, "
        f"routing overrides and the listed destination/auth headers and query keys are not accepted.")


def class_for_selectors(source: Mapping[str, Any], parent: Any, index: Optional[int] = None) -> str:
    """The validated logical class for a task/config/request mapping; refuses every concrete selector."""
    model, provider = source.get("model"), source.get("provider")
    if model is not None and not isinstance(model, str):
        raise _refuse(index, "model must be a string class selector")
    if provider is not None and not isinstance(provider, str):
        raise _refuse(index, "provider must be a string")
    if model and not is_class(model):
        raise _refuse(index, f"model {model!r} is not a configured class")
    if provider not in (None, "", "auto"):
        raise _refuse(index, f"provider {provider!r} is a concrete pin")
    for key in _TRANSPORT_KEYS:
        if not _blank(source.get(key)):
            raise _refuse(index, f"{key} is a transport override")
    return model or parent_class(parent)


def check_request_overrides(overrides: Any, index: Optional[int] = None) -> None:
    """Refuse routing keys at the top level and in the SDK merge containers, and the listed auth names in
    ``extra_headers`` / ``extra_query``; other tuning is untouched."""
    if overrides is None:
        return
    if not isinstance(overrides, Mapping):
        raise _refuse(index, "request_overrides must be a mapping")
    for key in overrides:
        if str(key).lower() in _ROUTING_KEYS:
            raise _refuse(index, f"request_overrides.{key} is a routing override")
    for name in _WIRE_CONTAINERS:
        inner = overrides.get(name)
        if inner is None:
            continue
        if not isinstance(inner, Mapping):
            raise _refuse(index, f"request_overrides.{name} must be a mapping")
        for key in inner:
            if str(key).lower() in _ROUTING_KEYS:
                raise _refuse(index, f"request_overrides.{name}.{key} is a routing override")
            if name == "extra_query" and str(key).lower() in _AUTH_QUERY:
                raise _refuse(index, f"request_overrides.{name}.{key} is a destination/auth override")
    headers = overrides.get("extra_headers")
    if headers is not None:
        if not isinstance(headers, Mapping):
            raise _refuse(index, "request_overrides.extra_headers must be a mapping")
        for key in headers:
            if str(key).lower() in _AUTH_HEADERS:
                raise _refuse(index, f"request_overrides.extra_headers.{key} is a destination/auth override")


def check_built_child(child: Any, tier: Any = None) -> None:
    """Post-construction recheck of a built child's ``request_overrides``, judged by where each key came from.

    Construction merges the allocator-selected custom route's own ``custom_providers[].extra_body`` under the
    caller's.  That operator route config is not a delegation override, so a routing key in the final
    ``extra_body`` is admitted only when that route's config (re-read here for the child's concrete
    provider/model/endpoint) holds the same key with the same value.  Caller authority is judged on its own
    record: the body captured before the merge and the tier overlay refuse any routing key, whether or not its
    value equals the route config.  Everything else in the final mapping gets the full check, so a key added or
    replaced after the merge still refuses.  Nothing is removed from what the child sends."""
    check_request_overrides(tier)
    caller = getattr(child, "_caller_extra_body", None)
    if caller:
        check_request_overrides({"extra_body": caller})
    final = getattr(child, "request_overrides", None)
    if not isinstance(final, Mapping) or not isinstance(final.get("extra_body"), Mapping):
        return check_request_overrides(final)
    body = final["extra_body"]
    from agent.agent_init import _custom_provider_extra_body_for_agent
    providers = getattr(child, "_custom_providers", None)
    trusted = _custom_provider_extra_body_for_agent(
        provider=str(getattr(child, "provider", "") or ""), model=str(getattr(child, "model", "") or ""),
        base_url=str(getattr(child, "base_url", "") or ""),
        custom_providers=providers if isinstance(providers, list) else []) or {}
    unowned = {key: value for key, value in body.items()
               if not (str(key).lower() in _ROUTING_KEYS and key in trusted and trusted[key] == value)}
    check_request_overrides({**final, "extra_body": unowned})


def preflight_tasks(parent: Any, tasks: Any) -> list[str]:
    """Class for every task (whole-batch refusal); non-list/non-dict shapes keep their native handling."""
    if not isinstance(tasks, list):
        return []
    return [class_for_selectors(t, parent, i) for i, t in enumerate(tasks) if isinstance(t, Mapping)]


def canonical_route(route: Mapping[str, Any], model_class: str, index: Optional[int] = None) -> dict[str, Any]:
    """Backend/default route -> its logical class, discarding stored concrete selectors and endpoints.

    Active override pins refuse instead of disappearing; non-routing tuning is preserved."""
    check_request_overrides(route.get("request_overrides"), index)
    out = {key: route[key] for key in _KEPT_TUNING if route.get(key) is not None}
    return {**out, "model": model_class, "provider": "auto", "fallback_providers": []}


def class_for_config(cfg: Mapping[str, Any], parent: Any) -> tuple[str, dict[str, Any]]:
    """An explicit credentials_cfg/lifecycle block is a NEW logical request: validate, then canonicalize."""
    model_class = class_for_selectors(cfg, parent)
    return model_class, canonical_route(cfg, model_class)


def logical_request(model_class: str, route: Mapping[str, Any], token: _Token) -> LogicalRequest:
    tuning = {k: route[k] for k in _KEPT_TUNING if route.get(k) is not None}
    return LogicalRequest(model_class, MappingProxyType(tuning), token)


def fence_build(parent: Any, logical: Any, **merged: Any) -> Optional[_Token]:
    """Builder's first operation: the merged builder kwargs must still equal the staged logical request."""
    token = origin_for(parent)
    if token is None and logical is None:
        return None
    if token is None or not isinstance(logical, LogicalRequest) or logical.origin is not token:
        raise RestrictionError("Kanban delegation build refused: missing or mismatched logical request.")
    if merged.get("model") != logical.model_class or merged.get("override_provider") not in (None, "", "auto"):
        raise _refuse(None, "the build selection differs from the validated class request")
    for key in ("override_base_url", "override_api_key", "override_api_mode", "override_acp_command",
                "override_acp_args"):
        if not _blank(merged.get(key)):
            raise _refuse(None, f"{key} is a transport override")
    check_request_overrides(merged.get("override_request_overrides"))
    return token
