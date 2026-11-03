"""Optional native observation seam: no-op unless a trusted observer entry loaded ``observer_runtime``. [daveey]

An observed (capsule) launch imports ``observer_runtime`` from its trusted entry before any Hermes module.
Every other process (ordinary workers, CLI, gateway) never has it, so each hook here resolves the module at
call time and falls back to the unobserved behaviour. A capsule-required card can't reach an unobserved
worker: the native owner refuses it first (``gateway.session_kanban_capsule``).
"""
from __future__ import annotations

import sys
from functools import wraps

_MODULE = 'observer_runtime'


def _observer():
    return sys.modules.get(_MODULE)


def _decorator(name):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            observer = _observer()
            if observer is None:
                return fn(*args, **kwargs)
            return getattr(observer, name)(fn)(*args, **kwargs)
        return wrapped
    return decorate


sdk_request = _decorator('sdk_request')
terminal_commit = _decorator('terminal_commit')
unsupported_summary = _decorator('unsupported_summary')


def active() -> bool:
    observer = _observer()
    return bool(observer is not None and observer.active())


def persisted(agent, success) -> None:
    observer = _observer()
    if observer is not None:
        observer.persisted(agent, success)


def terminal(agent, name, call_id, result, observed, blocked) -> None:
    observer = _observer()
    if observer is not None:
        observer.terminal(agent, name, call_id, result, observed, blocked)


def finish_oneshot(rc):
    observer = _observer()
    return rc if observer is None else observer.finish_oneshot(rc)


def open_codex_stream(agent, client, kwargs):
    """Codex SSE open: observed by ``observer_codex`` only inside an observed launch."""
    if _observer() is None:
        return client.responses.create(**kwargs)
    from observer_codex import open_stream
    return open_stream(agent, client, kwargs)
