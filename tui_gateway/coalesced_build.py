"""Run one expensive build at a time per key without serving anything stale. [daveey]

A caller never receives a build that started before it asked: if a build is running when it
arrives, it waits for that one to finish and shares the NEXT one with everyone else who arrived
meanwhile. Overlapping identical requests therefore cost at most two builds in a row instead of
one concurrent build each, and a caller that just changed something still sees its change.

The legacy ``projects.tree`` RPC had no such bound: a slow build was re-requested by the sidebar
poll before it finished, so seven identical builds ran at once and each took a minute.
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Hashable
from typing import Any

_WAIT_POLL_S = 1.0


class _State:
    __slots__ = ("started", "finished", "running", "value", "waiters")

    def __init__(self) -> None:
        self.started = 0  # builds begun
        self.finished = 0  # index of the newest build that completed successfully
        self.running = False
        self.value: Any = None
        self.waiters = 0


class Coalescer:
    """``run(key, build)``: the result of a ``build()`` that began after this call was made."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._states: dict[Hashable, _State] = {}

    def run(self, key: Hashable, build: Callable[[], Any]) -> Any:
        with self._cond:
            state = self._states.get(key)
            if state is None:
                state = self._states[key] = _State()
            need = state.started + 1  # the first build to begin after this request
            state.waiters += 1
            try:
                while state.running and state.finished < need:
                    self._cond.wait(_WAIT_POLL_S)
                if state.finished >= need:
                    value = state.value
                    self._leave(key, state)
                    return value
                state.started += 1
                mine = state.started
                state.running = True
            except BaseException:
                self._leave(key, state)
                raise
        try:
            value = build()
        except BaseException:
            # A failed build certifies nothing: every waiter retries for itself.
            with self._cond:
                state.running = False
                self._leave(key, state)
                self._cond.notify_all()
            raise
        with self._cond:
            state.running = False
            state.finished, state.value = mine, value
            self._leave(key, state)
            self._cond.notify_all()
        return value

    def _leave(self, key: Hashable, state: _State) -> None:
        """Forget an idle key: the shared value is not a cache and must not outlive its callers."""
        state.waiters -= 1
        if state.waiters <= 0 and not state.running and self._states.get(key) is state:
            del self._states[key]


project_tree = Coalescer()
