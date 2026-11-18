"""Observe transport endpoints before the existing trusted-proxy interpretation."""
from __future__ import annotations

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

# [daveey] Idle keep-alive lifetime of an HTTP connection on every Hermes listener. uvicorn's default (5 s)
# is shorter than the gap between a remote Desktop's polls, and the server's FIN crosses the SSH tunnel
# only after Desktop has already picked that pooled socket for its next request: on hermes-hub
# (2026-11-13) 197 requests in 7 min were written into server-closed sockets, each a `socket hang up`
# that marked the whole remote down. The side that closes an idle connection must be the client, so this
# stays well above Desktop's pooled-socket idle timeout (CLIENT_KEEPALIVE_IDLE_MS in api-transport.ts).
KEEP_ALIVE_IDLE_S = 120


def transport_observed_app(app, *, proxy_headers: bool, forwarded_allow_ips):
    """Compose raw observation outside Uvicorn's unchanged proxy middleware.

    Pass ``proxy_headers=False`` to Config: its automatic wrapper would otherwise run
    outside this observer and discard the original peer. Authentication still sees the
    same interpreted client/scheme as before; only provenance reads the raw observation.
    """
    inner = ProxyHeadersMiddleware(app, trusted_hosts=forwarded_allow_ips) if proxy_headers else app
    from hermes_cli.http_body_accounting import wrap_http_body_accounting

    # Body-byte diagnostics wrap the interpreted app. No loop_accounting scope:
    # inner ``accounted`` route passes remain the CPU names.
    inner = wrap_http_body_accounting(inner)

    async def observed(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = dict(scope)
            scope["hermes.raw_transport"] = (scope.get("client"), scope.get("server"))
        await inner(scope, receive, send)

    return observed
