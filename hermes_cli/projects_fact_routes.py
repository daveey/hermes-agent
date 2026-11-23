"""Fact fast path for project tree routes, outside legacy scan/coalescing wrappers.

Startup injects a provider resolving the authorized request context entirely in
memory. It returns a bound read adapter, Unavailable, or None for legacy. It must
never turn a missing facts binding into legacy fallback. No owner is acquired on
this path; the shared host owns scope authorization and lifecycle.
"""
from __future__ import annotations

import functools
import inspect
import json
from contextlib import nullcontext

from fastapi import HTTPException
from fastapi.responses import Response
from hermes_cli.projection_request import RequestBinding

_installed = None
_request = RequestBinding("projects_fact_request")


def request(profile):
    """Called inside native authorization and before fast path or legacy lease."""
    return _request.pin(profile, _installed)


def install(v1, provider) -> None:
    global _installed
    _installed = (v1, provider)


def read(view, selector, *, decoded=False):
    selected = _request.get(selector.get("profile"), _installed)
    if selected is None:
        return None
    v1, bound = selected
    if bound is None:
        return None
    result = bound if isinstance(bound, v1.Unavailable) else bound.read(view, selector)
    if result is None:
        return None
    if isinstance(result, v1.Ready):
        return json.loads(result.encoded) if decoded else Response(result.encoded, media_type="application/json")
    raise HTTPException(503, detail={"code": "facts_unavailable", "reason": result.reason},
                        headers={"Retry-After": "1"})


def route(function):
    """Wrap BOTH exports of the existing cached tree route; leave rollback intact."""
    signature = inspect.signature(function)

    def selector(args, kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        return {"surface": "profiles", **bound.arguments}

    @functools.wraps(function)
    def synchronous(*args, **kwargs):
        selected = selector(args, kwargs)
        with _request.pin(selected.get("profile"), _installed):
            result = read("tree", selected, decoded=True)
            if result is not None:
                return result
            with legacy(selected.get("profile")):
                return function(*args, **kwargs)

    @functools.wraps(function.http_endpoint)
    async def asynchronous(*args, **kwargs):
        selected = selector(args, kwargs)
        with _request.pin(selected.get("profile"), _installed):
            result = read("tree", selected)
            if result is not None:
                return result
            with legacy(selected.get("profile")):
                return await function.http_endpoint(*args, **kwargs)

    synchronous.http_endpoint = asynchronous
    return synchronous


def legacy(profile):
    """Lease the concrete read beneath caching/coalescing and native auth."""
    selected = _request.get(profile, _installed)
    if selected is None:
        return nullcontext()
    v1, bound = selected
    if bound is None:
        return nullcontext()
    if isinstance(bound, v1.Unavailable):
        raise HTTPException(503, detail={"code": "facts_unavailable", "reason": bound.reason})
    return bound.legacy()


def legacy_reader(function):
    signature = inspect.signature(function)

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        arguments = signature.bind(*args, **kwargs)
        arguments.apply_defaults()
        with legacy(arguments.arguments.get("profile")):
            return function(*args, **kwargs)
    return wrapped
