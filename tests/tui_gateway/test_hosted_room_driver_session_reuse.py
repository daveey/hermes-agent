"""Canonical session reuse must not depend on the CI runner's scheduling speed."""

import threading
import time
from pathlib import Path

from tests.tui_gateway.test_hosted_room_driver_runtime import (
    PROFILE,
    ROOM_SESSION_SOURCE,
    FakeSessionRPC,
    _admit,
    _identity,
    _runtime,
    db as db,  # noqa: PLC0414 — explicitly re-export the shared pytest fixture
    state,
)


def test_existing_canonical_session_is_resumed_not_duplicated(db: Path):
    identity = _identity()
    _admit(db, identity)
    rpc = FakeSessionRPC()
    session_id = rpc.add_session()
    # Session reuse is not a lease-expiry test: scheduler stalls must not fence it.
    now = time.time()
    settled = threading.Event()
    runtime = _runtime(
        db, rpc, clock=lambda: now, publish_terminal=lambda *_: settled.set(),
    )
    runtime.start()
    try:
        assert settled.wait(30.0), runtime.status()
        assert state.get_task(db, identity)["status"] == "settled"
    finally:
        assert runtime.stop(timeout=30.0)

    assert not [call for call in rpc.calls if call[0] == "create"]
    resume = next(params for method, params in rpc.calls if method == "resume")
    assert resume == {
        "profile": PROFILE,
        "session_id": session_id,
        "source": ROOM_SESSION_SOURCE,
    }
