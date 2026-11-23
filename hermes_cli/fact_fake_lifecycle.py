"""Deterministic consumer lifecycle for the shared-store integration fake. [daveey]

This test driver is NOT the production host's version/health/config activation gate.
"""

from collections.abc import Callable
from dataclasses import dataclass

from hermes_cli.fact_contract import Scope

CONSUMERS = ("sessions", "panel", "annotations", "board", "notifications")


@dataclass
class Registration:
    remove: Callable[[], None]
    closed: bool = False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.remove()


class FakeLifecycle:
    def __init__(self):
        self.callbacks = {}
        self.ready = {}
        self.generation = 0

    def bind_consumer(self, name: str, scope: Scope, on_mode) -> Registration:
        key = scope, name
        if key in self.callbacks:
            raise ValueError("consumer already bound")
        self.callbacks[key] = on_mode
        on_mode("legacy", self.generation)

        def remove():
            self.callbacks.pop(key, None)
            self.ready.pop(key, None)

        return Registration(remove)

    def report_ready(self, name, scope, *, version=1, ready=True):
        self.ready[scope, name] = version == 1 and ready

    def activate(self, scope):
        keys = [(scope, name) for name in CONSUMERS]
        if not all(key in self.callbacks and self.ready.get(key) for key in keys):
            raise RuntimeError("consumer set not ready")
        self.generation += 1
        transitioned = []
        succeeded = False
        try:
            for key in keys:
                transitioned.append(key)
                self.callbacks[key]("facts", self.generation)
            succeeded = True
        finally:
            if not succeeded:
                for key in reversed(transitioned):
                    self.callbacks[key]("legacy", self.generation)

    def rollback(self, scope):
        self.generation += 1
        for name in CONSUMERS:
            callback = self.callbacks.get((scope, name))
            if callback is not None:
                callback("legacy", self.generation)
