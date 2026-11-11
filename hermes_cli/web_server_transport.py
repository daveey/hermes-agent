"""Observe transport endpoints before the existing trusted-proxy interpretation."""
from __future__ import annotations

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware


def transport_observed_app(app, *, proxy_headers: bool, forwarded_allow_ips):
    """Compose raw observation outside Uvicorn's unchanged proxy middleware.

    Pass ``proxy_headers=False`` to Config: its automatic wrapper would otherwise run
    outside this observer and discard the original peer. Authentication still sees the
    same interpreted client/scheme as before; only provenance reads the raw observation.
    """
    inner = ProxyHeadersMiddleware(app, trusted_hosts=forwarded_allow_ips) if proxy_headers else app

    async def observed(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = dict(scope)
            scope["hermes.raw_transport"] = (scope.get("client"), scope.get("server"))
        await inner(scope, receive, send)

    return observed
