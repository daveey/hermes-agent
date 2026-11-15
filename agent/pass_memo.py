"""Memos that live for one request/pass only.

A picker pass repeats the same lookup hundreds of times (the stored Codex login per model, the
provider-profile id map per capability probe). Caching those across passes would need every input
named (files, env, registry mutations that bypass registration); a memo scoped to one pass needs
none: the next pass starts empty, so any change is seen on the next call, and no clock is involved.
"""
from __future__ import annotations

import contextlib
import contextvars
from typing import Any, Callable, Iterator


class PassMemo:
    """``with memo.scope(): ... memo.get(key, compute)`` computes once per key inside the scope.

    Outside a scope, ``get`` just computes. A scope that has exited is closed for good, so a context
    copied inside it (``copy_context()`` workers outliving the pass) never reads or fills it.
    """

    def __init__(self, name: str) -> None:
        self._var: contextvars.ContextVar[dict | None] = contextvars.ContextVar(name, default=None)

    @contextlib.contextmanager
    def scope(self) -> Iterator[None]:
        outer = self._var.get()
        if outer is not None and outer["open"]:  # nested: share the outer pass
            yield
            return
        state: dict[str, Any] = {"open": True, "values": {}}
        token = self._var.set(state)
        try:
            yield
        finally:
            state["open"] = False
            state["values"].clear()
            self._var.reset(token)

    def get(self, key: Any, compute: Callable[[], Any]) -> Any:
        state = self._var.get()
        if state is None or not state["open"]:
            return compute()
        values = state["values"]
        try:
            return values[key]
        except KeyError:
            pass
        value = values[key] = compute()
        return value
