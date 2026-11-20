"""Owner forwarding cannot transfer an unexpired retry epoch as bare prompt text."""
import time

from tests.tui_gateway import test_update_pause_ledger_owner as owner
from tui_gateway import server, turn_marker

env = owner.env


def test_owner_forward_keeps_deadline_locally_until_expiry(env, monkeypatch):
    marker = turn_marker.read_turn_marker(env.home, "chat")
    deadline = time.time() + 600
    assert turn_marker.stamp_provider_retry_deadline(env.home, "chat", deadline, marker)
    marker = turn_marker.read_turn_marker(env.home, "chat")
    session = owner._session(env.home)
    assert server._paused_turn_to_ledger_owner("sid", session, "chat", marker, owner._ledger_refusal()) is False
    assert env.forwarded == []
    assert turn_marker.read_turn_marker(env.home, "chat") == marker
    monkeypatch.setattr(server, "time", owner.types.SimpleNamespace(time=lambda: deadline + 1))
    assert server._paused_turn_to_ledger_owner("sid", session, "chat", marker, owner._ledger_refusal()) is True
    assert len(env.forwarded) == 1
    assert turn_marker.read_turn_marker(env.home, "chat") is None
