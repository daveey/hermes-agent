"""Passive ASGI HTTP lifecycle and body-byte counters for stall attribution.

Counts HTTP requests at the outer transport wrapper without opening a
loop_accounting CPU scope, so existing per-route ``accounted`` passes stay
the outermost CPU names. Keys are a fixed allowlist of literal templates
(inspected Desktop poll / session / plugin paths); unmatched, auth
early-exit, 404 and redirect stay under ``other``. Global ``totals`` book
every request at entry; the per-route row is booked once, at
``http.response.start`` (2xx on a matched template, else ``other``) or at a
terminal before start (``other``). Cumulative counts are never subtracted.
WebSocket and lifespan pass through.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Any, Optional

MAX_ROUTE_KEYS = 64
OTHER = "other"
UNIT = "ASGI body bytes"

# Inspected from hermes_cli/web_routers and always_on_rollover.REPLAY_PATHS.
# Placeholders are explicit one-segment ``{name}`` only; never raw ids.
ROUTE_ALLOWLIST: tuple[tuple[str, str], ...] = (
    ("GET", "/api/health"),
    ("GET", "/api/health/idle"),
    ("GET", "/api/status"),
    ("GET", "/api/sessions"),
    ("GET", "/api/sessions/search"),
    ("GET", "/api/sessions/stats"),
    ("GET", "/api/sessions/empty/count"),
    ("GET", "/api/sessions/{session_id}"),
    ("GET", "/api/sessions/{session_id}/messages"),
    ("GET", "/api/sessions/{session_id}/timeline"),
    ("GET", "/api/sessions/{session_id}/messages/around"),
    ("GET", "/api/sessions/{session_id}/mutation-snapshot"),
    ("GET", "/api/sessions/{session_id}/latest-descendant"),
    ("GET", "/api/sessions/{session_id}/outcome"),
    ("GET", "/api/sessions/{session_id}/export"),
    ("GET", "/api/session-kanban-tasks"),
    ("GET", "/api/profiles"),
    ("GET", "/api/profiles/sessions"),
    ("GET", "/api/profiles/sessions/stamps"),
    ("GET", "/api/profiles/projects/tree"),
    ("GET", "/api/cron/jobs"),
    ("GET", "/api/cron/jobs/{job_id}"),
    ("GET", "/api/cron/jobs/{job_id}/runs"),
    ("GET", "/api/ui-preferences"),
    ("GET", "/api/model/info"),
    ("GET", "/api/model/options"),
    ("GET", "/api/auth/me"),
    ("GET", "/api/auth/providers"),
    ("GET", "/api/plugins/kanban/board"),
    ("GET", "/api/plugins/kanban/boards"),
    ("GET", "/api/plugins/kanban/session-labels"),
    ("GET", "/api/plugins/user-steps/session-cards"),
    ("GET", "/api/plugins/user-steps/requests"),
    ("GET", "/api/plugins/hub-sync/status"),
    ("GET", "/api/plugins/runners/v1/fleet"),
    ("GET", "/api/plugins/throughput/v1/series"),
    ("GET", "/api/plugins/hermes-monitor/workers"),
    ("GET", "/api/plugins/inbox/items"),
    ("GET", "/api/plugins/core-update/status"),
    ("GET", "/api/plugins/account-usage/last"),
    ("GET", "/api/plugins/account-usage/selection"),
    ("POST", "/api/sessions/import"),
    ("POST", "/api/sessions/prune"),
    ("POST", "/api/sessions/bulk-delete"),
    ("POST", "/api/chat/file-upload"),
    ("POST", "/api/cron/jobs"),
    ("POST", "/api/health/retirement"),
    ("PATCH", "/api/sessions/{session_id}"),
    ("PATCH", "/api/ui-preferences"),
    ("DELETE", "/api/sessions/{session_id}"),
)

_FIELDS = (
    "requests_started",
    "response_finished",
    "failed",
    "cancelled",
    "inflight",
    "request_body_bytes",
    "response_body_bytes",
)


def _zero() -> dict[str, int]:
    return {name: 0 for name in _FIELDS}


def _parts(path: str) -> tuple[str, ...]:
    return tuple(path.split("/")[1:])


def _compile(
    allowlist: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    compiled = []
    for method, template in allowlist[:MAX_ROUTE_KEYS]:
        compiled.append((method, template, _parts(template)))
    return tuple(compiled)


_COMPILED = _compile(ROUTE_ALLOWLIST)

_lock = threading.Lock()
_routes: dict[str, dict[str, int]] = {OTHER: _zero()}
_totals: dict[str, int] = _zero()
_unclassified = 0


def match_route(method: str, path: str) -> Optional[str]:
    """Allowlisted ``METHOD /template`` or None. Never returns a raw path."""
    # ``path`` is an ASGI ``scope["path"]`` (no query). One trailing slash is
    # tolerated; empty interior segments (``//``) never match.
    if not path.startswith("/"):
        return None
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    segs = _parts(path)
    if any(not seg for seg in segs):
        return None
    best: Optional[str] = None
    best_score = (-1, -1)
    method = method.upper()
    for tmpl_method, template, parts in _COMPILED:
        if tmpl_method != method or len(parts) != len(segs):
            continue
        literals = 0
        ok = True
        for part, seg in zip(parts, segs):
            if part.startswith("{") and part.endswith("}"):
                continue
            if part != seg:
                ok = False
                break
            literals += 1
        if not ok:
            continue
        score = (literals, len(parts))
        if score > best_score:
            best_score = score
            best = f"{tmpl_method} {template}"
    return best


def _named_count() -> int:
    return sum(1 for key in _routes if key != OTHER)


def _row(key: str) -> dict[str, int]:
    if key not in _routes:
        if key != OTHER and _named_count() >= MAX_ROUTE_KEYS:
            key = OTHER
        if key not in _routes:
            _routes[key] = _zero()
    return _routes[key]


class _State:
    __slots__ = ("classified", "disconnect", "key", "matched", "outcome", "req_bytes")

    def __init__(self, matched: Optional[str]) -> None:
        self.matched = matched
        self.key = OTHER
        self.classified = False
        self.outcome: Optional[str] = None
        self.req_bytes = 0
        self.disconnect = False


def _start(state: _State) -> None:
    global _unclassified
    with _lock:
        _totals["requests_started"] += 1
        _totals["inflight"] += 1
        _unclassified += 1


def _classify_locked(state: _State, key: str) -> None:
    """Book the route row once. Caller holds ``_lock``. Never subtracts."""
    global _unclassified
    if state.classified:
        return
    if key != OTHER and key not in _routes and _named_count() >= MAX_ROUTE_KEYS:
        key = OTHER
    state.classified = True
    state.key = key
    row = _row(key)
    row["requests_started"] += 1
    row["inflight"] += 1
    row["request_body_bytes"] += state.req_bytes
    _unclassified -= 1


def _classify(state: _State, key: str) -> None:
    with _lock:
        _classify_locked(state, key)


def _finish(state: _State, outcome: str) -> None:
    if state.outcome is not None:
        return
    state.outcome = outcome
    with _lock:
        _classify_locked(state, OTHER)  # terminal before response.start
        row = _row(state.key)
        row["inflight"] -= 1
        row[outcome] += 1
        _totals["inflight"] -= 1
        _totals[outcome] += 1


def wrap_http_body_accounting(app):
    """ASGI wrapper: HTTP lifecycle/body bytes only; WS/lifespan unchanged."""

    async def wrapped(scope, receive, send):
        if scope.get("type") != "http":
            await app(scope, receive, send)
            return
        method = scope.get("method") or "GET"
        path = scope.get("path") or ""
        state = _State(match_route(method, path))
        _start(state)

        async def receive_wrap():
            message = await receive()
            typ = message.get("type")
            if typ == "http.request":
                n = len(message.get("body") or b"")
                if n:
                    with _lock:
                        _totals["request_body_bytes"] += n
                        state.req_bytes += n  # buffered until classified
                        if state.classified:
                            _row(state.key)["request_body_bytes"] += n
            elif typ == "http.disconnect":
                state.disconnect = True
            return message

        async def send_wrap(message):
            typ = message.get("type")
            if typ == "http.response.start":
                await send(message)
                if state.outcome is None and not state.classified:
                    status = int(message.get("status") or 0)
                    ok = state.matched is not None and 200 <= status < 300
                    _classify(state, state.matched if ok else OTHER)
                return
            if typ == "http.response.body":
                body = message.get("body") or b""
                more = bool(message.get("more_body", False))
                await send(message)
                if body:
                    with _lock:
                        _classify_locked(state, OTHER)  # body without start
                        _row(state.key)["response_body_bytes"] += len(body)
                        _totals["response_body_bytes"] += len(body)
                if not more:
                    _finish(state, "response_finished")
                return
            await send(message)

        try:
            await app(scope, receive_wrap, send_wrap)
        except asyncio.CancelledError:
            _finish(state, "cancelled")
            raise
        except Exception:
            _finish(state, "failed")
            raise
        else:
            if state.outcome is None:
                _finish(state, "cancelled" if state.disconnect else "failed")

    return wrapped


def _clear_locked() -> None:
    global _unclassified
    _routes.clear()
    _routes[OTHER] = _zero()
    _totals.clear()
    _totals.update(_zero())
    _unclassified = 0


def snapshot() -> dict[str, Any]:
    """Immutable copy. ``unit`` is ``ASGI body bytes``; keys never raw paths.

    ``totals`` counts every request at entry (including unclassified);
    ``routes`` only hold classified requests. Invariant:
    ``totals.inflight == unclassified_inflight + sum(routes.inflight)``.
    """
    with _lock:
        routes = {key: dict(row) for key, row in _routes.items()}
        totals = dict(_totals)
        unclassified = _unclassified
        mono = time.monotonic_ns()
        wall = time.time_ns()
    if OTHER not in routes:
        routes[OTHER] = _zero()
    return {
        "unit": UNIT,
        "routes": routes,
        "totals": totals,
        "unclassified_inflight": unclassified,
        "monotonic_ns": mono,
        "time_ns": wall,
    }


def reset() -> None:
    """Clear counters (tests). Does not replace the lock."""
    with _lock:
        _clear_locked()


def _after_fork_child() -> None:
    global _lock
    _lock = threading.Lock()
    _clear_locked()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_child)
