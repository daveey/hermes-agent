"""Unsupported websocket clients synchronously withdraw unsendable approvals."""
from tests.tui_gateway.test_protocol import _silent_ws, _ws_session, server

__all__ = ["server"]  # Shared pytest fixture, discovered by name.


def test_approval_for_a_ws_client_that_never_advertised_settles_the_queue_entry(server, monkeypatch):
    """A queue withdrawal must precede polling, regardless of host scheduling latency."""
    from tools import approval as approval_mod
    from tools import approval_gateway_wait as wait_mod

    peer = _silent_ws()
    _ws_session(server, "ws-old-approval", peer)
    monkeypatch.setattr(wait_mod._ctx, "_get_approval_timeout", lambda: 3)
    monkeypatch.setattr(wait_mod._ctx, "_fire_approval_hook", lambda name, **kw: None)
    # Assert synchronous withdrawal before the real wait, not a sub-second
    # budget that includes imports/hooks and OS scheduling on a loaded runner.
    poll = wait_mod._poll_event
    observed = []

    def already_set(event, session_key, **kwargs):
        observed.append(event.is_set())
        assert event.is_set(), "unsupported client left an approval waiting for a human"
        return poll(event, session_key, **kwargs)

    monkeypatch.setattr(wait_mod, "_poll_event", already_set)
    approval_mod.register_gateway_notify("ws-old-approval", lambda data: server._emit_approval_request("ws-old-approval", data))
    try:
        decision = wait_mod._await_gateway_decision(
            "ws-old-approval", approval_mod._gateway_notify_cbs["ws-old-approval"],
            {"command": "rm -rf build", "description": "", "pattern_key": "dangerous", "pattern_keys": ["dangerous"]})
    finally:
        approval_mod.unregister_gateway_notify("ws-old-approval")
    assert observed == [True]
    assert decision["choice"] is None and decision["cancelled"]
    assert peer.frames == []
    assert "ws-old-approval" not in approval_mod._gateway_queues
