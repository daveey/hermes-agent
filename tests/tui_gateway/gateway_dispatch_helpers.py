"""Synchronous RPC dispatch for handler-unit tests."""
from tui_gateway import server
from tui_gateway.transport import bind_transport, reset_transport


def dispatch_sync(req: dict, transport=None) -> dict | None:
    """Run one RPC synchronously while preserving its transport binding.

    Voice RPCs run on the RPC pool and dispatch returns None. Handler-unit
    tests drive the handler inline rather than testing pool routing.
    """
    token = bind_transport(transport)
    try:
        return server.handle_request(req)
    finally:
        reset_transport(token)
