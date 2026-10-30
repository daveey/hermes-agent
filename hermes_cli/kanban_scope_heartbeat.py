"""How long an owner collector's per-scope heartbeat (``task_scopes[].observed_at``) stays fresh. [daveey]

Every reader of that heartbeat (remote-terminal release in ``kanban_session_activity``, owner liveness in
``kanban_origin_ledger``, wait probes in ``kanban_wait_replica``) asks this module, so the freshness window
and the owner's publishing cadence can no longer drift apart as two unrelated constants.

The owner's hermes-hub collector publishes ``scope_heartbeat_period_s`` in its ``gateway/info`` record: the
longest gap between successive clean reads of a scope over its recent cycles. A loaded hub (load 15-30 on 16
cores) stretched that gap to ~440 s while the window was a fixed 450 s, so terminal remote cards flapped
"never released" for part of every cycle. The window is now ``SCOPE_HEARTBEAT_PERIOD_FACTOR`` times the
published period, never below ``SCOPE_HEARTBEAT_FLOOR_S`` (an owner that publishes no period, i.e. an older
client) and never above ``SCOPE_HEARTBEAT_MAX_S``, so an offline owner's last word still expires.
"""
from __future__ import annotations

import json
import math
from typing import Any

SCOPE_HEARTBEAT_FLOOR_S = 450.0
SCOPE_HEARTBEAT_MAX_S = 1800.0
SCOPE_HEARTBEAT_PERIOD_FACTOR = 2.5


def published_period(info: Any) -> float | None:
    """The owner's published heartbeat period in seconds, or None when absent or malformed."""
    period = info.get('scope_heartbeat_period_s') if isinstance(info, dict) else None
    if isinstance(period, bool) or not isinstance(period, (int, float)):
        return None
    if not math.isfinite(period) or period <= 0:
        return None
    return float(period)


def window_for_period(period: float | None) -> float:
    """Freshness window for a collector that refreshes each scope at most every ``period`` seconds."""
    if period is None:
        return SCOPE_HEARTBEAT_FLOOR_S
    return min(SCOPE_HEARTBEAT_MAX_S, max(SCOPE_HEARTBEAT_FLOOR_S, SCOPE_HEARTBEAT_PERIOD_FACTOR * period))


def scope_window(info: Any) -> float:
    """Freshness window for heartbeats in one owner's ``gateway/info`` record."""
    return window_for_period(published_period(info))


def fresh(observed_at: Any, now: float, window: float) -> bool:
    """A heartbeat observed within ``window`` seconds before ``now`` (a future-dated one proves nothing)."""
    return type(observed_at) in (int, float) and 0 <= now - observed_at <= window


def scope_heartbeats(info_rows: list[tuple[str, str]]) -> dict[tuple[str, str, str], tuple[float, float]]:
    """``(gateway, profile, board) -> (observed_at, window)`` for every scope its owner collector read cleanly;
    ``window`` is that owner's freshness window (``scope_window``).

    Ambiguous scopes (listed twice) and scopes not in state ``ok`` are absent: they prove nothing."""
    seen: dict[tuple[str, str, str], list] = {}
    windows: dict[tuple[str, str, str], float] = {}
    for gw, raw in info_rows:
        try:
            info = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(info, dict) or info.get('gateway') != gw or not isinstance(info.get('task_scopes'), list):
            continue
        for scope in info['task_scopes']:
            if isinstance(scope, dict) and isinstance(scope.get('profile'), str) and isinstance(scope.get('board'), str):
                key = (str(gw), scope['profile'], scope['board'])
                seen.setdefault(key, []).append(scope)
                windows[key] = scope_window(info)
    return {key: (scopes[0]['observed_at'], windows[key]) for key, scopes in seen.items()
            if len(scopes) == 1 and scopes[0].get('state') == 'ok'
            and type(scopes[0].get('observed_at')) in (int, float)}
