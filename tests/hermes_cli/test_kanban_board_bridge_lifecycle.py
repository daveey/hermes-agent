"""Bounded readiness and cleanup for the real Board integration peer."""
import queue
from types import SimpleNamespace

import pytest

from tests.hermes_cli.kanban_real_board_bridge import BoardBridge


def test_ready_peer_returns_key():
    bridge = SimpleNamespace(ready=queue.Queue())
    bridge.ready.put({"key": "fixture-key"})
    process = SimpleNamespace(poll=lambda: None)
    assert BoardBridge.wait_ready(bridge, process, timeout=1) == {"key": "fixture-key"}


def test_exited_peer_fails_without_waiting_for_readiness():
    class NoWaitQueue:
        def get_nowait(self):
            raise queue.Empty

        def get(self, **kwargs):
            pytest.fail("already-exited peer must not wait")

    bridge = SimpleNamespace(ready=NoWaitQueue())
    process = SimpleNamespace(poll=lambda: 7)
    with pytest.raises(RuntimeError, match="exited.*7"):
        BoardBridge.wait_ready(bridge, process, timeout=60)


def test_live_peer_readiness_timeout_is_bounded():
    bridge = SimpleNamespace(ready=queue.Queue())
    process = SimpleNamespace(poll=lambda: None)
    with pytest.raises(TimeoutError, match="not ready"):
        BoardBridge.wait_ready(bridge, process, timeout=0)


def test_peer_becomes_ready_during_wait():
    class DelayedQueue:
        def get_nowait(self):
            raise queue.Empty

        def get(self, **kwargs):
            return {"key": "delayed-key"}

    bridge = SimpleNamespace(ready=DelayedQueue())
    process = SimpleNamespace(poll=lambda: None)
    assert BoardBridge.wait_ready(bridge, process, timeout=1) == {"key": "delayed-key"}
