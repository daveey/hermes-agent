"""Consumer fakes exercise the same coordinated activation boundary."""

import pytest


def test_latch_requires_all_readiness_and_rolls_back_failed_transition():
    from hermes_cli.fact_contract import Scope
    from hermes_cli.fact_fake import FakeStore

    store = FakeStore()
    scope = Scope("install", "profile")
    seen = []
    fail = [True]

    def panel(mode, generation):
        seen.append(("panel", mode, generation))
        if mode == "facts" and fail[0]:
            raise RuntimeError("legacy loop did not stop")

    registrations = []
    for name in ("sessions", "panel", "annotations", "board", "notifications"):
        callback = panel if name == "panel" else lambda mode, generation, n=name: seen.append((n, mode, generation))
        registrations.append(store.bind_consumer(name, scope, callback))
    with pytest.raises(RuntimeError, match="not ready"):
        store.activate(scope)
    for name in ("sessions", "panel", "annotations", "board", "notifications"):
        store.report_ready(name, scope, version=1, ready=True)
    with pytest.raises(RuntimeError, match="legacy loop"):
        store.activate(scope)
    assert ("sessions", "legacy", 1) in seen
    assert not any(name == "annotations" and mode == "facts" for name, mode, _generation in seen)
    fail[0] = False
    store.activate(scope)
    assert ("notifications", "facts", 2) in seen
    store.rollback(scope)
    assert seen[-1] == ("notifications", "legacy", 3)
    registrations[-1].close()
    with pytest.raises(RuntimeError, match="not ready"):
        store.activate(scope)
