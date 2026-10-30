"""Route-agnostic non-interactive (bearer-token) auth seam for the dashboard.

Any machine-credential provider plugs in here. A route opts in by registering its exact path via
:func:`register_token_route`; only registered paths are token-authable, so the auth surface of
existing routes never widens. :func:`token_auth_middleware` runs OUTERMOST (installed last) and
owns the decision for a token route: a recognised token attaches ``request.state.token_principal``
+ ``token_authenticated`` (the cookie gates honour that flag and never bounce to /login);
otherwise 401, or 503 when a provider's backing store was unreachable. Fails closed.
"""
from __future__ import annotations

import logging
import threading
from typing import Awaitable, Callable, Optional, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from hermes_cli.dashboard_auth import list_token_providers
from hermes_cli.dashboard_auth.audit import AuditEvent, audit_log
from hermes_cli.dashboard_auth.base import ProviderError, TokenPrincipal
from hermes_cli.dashboard_auth.request_utils import (
    client_ip as _client_ip, extract_bearer as extract_bearer_token, unreachable_response)

_log = logging.getLogger(__name__)

_token_routes: set[str] = set()  # exact paths that accept bearer-token auth
# Exact paths whose core service callers present the deployment session token
# (``X-Hermes-Session-Token``) with no cookie, mapped to the constant-time check that verifies it.
_session_token_routes: dict[str, Callable[[Request], bool]] = {}
_lock = threading.Lock()

SESSION_TOKEN_PROVIDER = "session-token"


def register_token_route(
        path: str, *, session_token_check: Optional[Callable[[Request], bool]] = None) -> None:
    """Mark ``path`` (exact match) as token-authable. Idempotent; does NOT make the route public.

    ``session_token_check`` (core app setup only): the route also accepts the deployment session
    token, verified by that callable (``web_server._has_valid_session_token``, the check
    ``_require_token`` uses). Such a route keeps every other credential it had: a request without
    a valid session token passes through to the cookie / native gates unchanged."""
    with _lock:
        if session_token_check is None:
            _token_routes.add(path)
        else:
            _session_token_routes[path] = session_token_check


def is_token_route(path: str) -> bool:
    """True if ``path`` was registered as token-authable (exact match)."""
    with _lock:
        return path in _token_routes or path in _session_token_routes


def is_session_token_route(path: str) -> bool:
    """True if ``path`` accepts the deployment session token on a gated dashboard."""
    with _lock:
        return path in _session_token_routes


def clear_token_routes() -> None:
    """Test-only: drop all plugin-registered (bearer) token routes. Core session-token routes,
    registered once at app setup, stay."""
    with _lock:
        _token_routes.clear()


def _session_token_principal(request: Request) -> Optional[TokenPrincipal]:
    with _lock:
        check = _session_token_routes.get(request.url.path)
    if check is None:
        return None
    # Only the gated dashboard refuses a header-only call; the loopback gate already accepts
    # the session token on these routes (and stamps its SSH bootstrap session), so leave it be.
    app = getattr(request, "app", None)
    if not getattr(getattr(app, "state", None), "auth_required", False):
        return None
    try:
        ok = bool(check(request))
    except Exception:  # health: allow BLE001 -- auth seam fails closed; traceback logged, never the token
        _log.warning("dashboard-auth: session-token check raised", exc_info=True)
        return None
    return TokenPrincipal(principal=SESSION_TOKEN_PROVIDER, provider=SESSION_TOKEN_PROVIDER) if ok else None


def authenticate_token(request: Request) -> tuple[Optional[TokenPrincipal], Optional[str]]:
    """Try every token provider against the request's bearer token. Returns ``(principal, None)``
    on success; ``(None, None)`` for no token or no recogniser (401); ``(None, name)`` when no
    provider accepted it AND at least one was unreachable (caller surfaces 503). Never raises.
    On a session-token route the deployment session token is accepted first
    (see :func:`register_token_route`)."""
    if (principal := _session_token_principal(request)) is not None:
        return principal, None
    token = extract_bearer_token(request)
    if not token:
        return None, None
    unreachable: Optional[str] = None
    for provider in list_token_providers():
        try:
            principal = provider.verify_token(token=token)
        except ProviderError as e:
            _log.warning("dashboard-auth: token provider %r unreachable during verify: %s",
                         provider.name, e)
            if unreachable is None:
                unreachable = provider.name
            continue
        except Exception as e:
            _log.warning("dashboard-auth: token provider %r raised during verify: %s",
                         provider.name, e)
            continue
        if principal is not None:
            return principal, None
    return None, unreachable


async def token_auth_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """Pass-through for unregistered paths; for a token route, valid token -> attach principal +
    flag, unreachable -> 503, else 401."""
    path = request.url.path
    if not is_token_route(path):
        return await call_next(request)
    principal, unreachable = authenticate_token(request)
    if principal is not None:
        request.state.token_principal = principal
        request.state.token_authenticated = True
        return await call_next(request)
    with _lock:
        bearer_route = path in _token_routes
    if not bearer_route:
        # Session-token route without a valid session token: the cookie / native gates decide,
        # exactly as before the route was registered.
        return await call_next(request)
    if unreachable:
        audit_log(
            AuditEvent.TOKEN_AUTH_FAILURE, provider=unreachable, reason="provider_unreachable",
            path=path, ip=_client_ip(request))
        return unreachable_response(unreachable)

    audit_log(
        AuditEvent.TOKEN_AUTH_FAILURE, reason="no_provider_recognises_token", path=path,
        ip=_client_ip(request))
    return JSONResponse({"error": "unauthenticated", "detail": "Unauthorized"}, status_code=401)
