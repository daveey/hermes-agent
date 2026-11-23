"""Deterministically driven shared-store test double, without I/O or workers. [daveey]"""

from hermes_cli.fact_fake_lifecycle import FakeLifecycle
from hermes_cli.fact_store import FactStore


class FakeStore(FactStore):
    """Uses real publication semantics; tests explicitly call drain for delivery."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._lifecycle = FakeLifecycle()

    def bind_consumer(self, name, scope, on_mode):
        return self._lifecycle.bind_consumer(name, scope, on_mode)

    def report_ready(self, name, scope, *, version=1, ready=True):
        self._lifecycle.report_ready(name, scope, version=version, ready=ready)

    def activate(self, scope):
        self._lifecycle.activate(scope)

    def rollback(self, scope):
        self._lifecycle.rollback(scope)
