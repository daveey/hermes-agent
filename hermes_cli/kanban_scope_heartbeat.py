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


def published_before_silence(last_beat: Any, now: float, published_at: Any) -> bool:
    """A record its owner published at or before the owner's LAST clean read of that scope, for an owner that has
    been silent since (``last_beat`` is past its window). [daveey]

    The owner read the whole scope cleanly after publishing the record and left it as it is; nothing newer exists,
    because a silent owner publishes nothing. So the record is the owner's standing answer, not a guess about a
    partitioned owner's present. A future-dated beat, a record with no publication time and a record published after
    the last beat prove nothing. Hub chat 20261110_185031_4f5f4b: workers 2, 5 and 7 were shut down on 2026-11-16 and
    the 2,376 cards they had finished (every one published before their last beat) read "done, release pending" in
    their origin chats from then on."""
    return (type(last_beat) in (int, float) and type(published_at) in (int, float)
            and 0 < published_at <= last_beat <= now)


class ScopeVerdicts(frozenset):
    """The scopes ``fresh`` at one instant (the set itself) plus, for every other scope with a heartbeat, that last
    beat (``quiet``: ``frozenset((scope, beat))``). Equal and hashed on both, so a projection keyed on it is rebuilt
    when a scope goes quiet or a quiet owner beats again; a silent owner's beat never moves, so its key is stable."""
    __slots__ = ("_beats", "quiet")

    def __new__(cls, live=(), quiet=()):
        self = super().__new__(cls, live)
        self.quiet = frozenset(quiet)
        self._beats = dict(self.quiet)
        return self

    def __eq__(self, other):
        return (frozenset.__eq__(self, other) is True
                and self.quiet == (other.quiet if isinstance(other, ScopeVerdicts) else frozenset()))

    def __ne__(self, other):
        return not self == other

    def __hash__(self):
        return hash((frozenset.__hash__(self), self.quiet))

    def last_beat(self, scope) -> float | None:
        return self._beats.get(scope)


def attests(verdicts, scope, published_at: Any) -> bool:
    """Whether ``scope``'s owner stands behind a record published at ``published_at``: its heartbeat is fresh, or it
    has been silent since a beat at or after that publication (``published_before_silence``). ``verdicts`` is a
    ``ScopeVerdicts`` (which holds no future-dated beat); a plain set of fresh scopes knows no quiet beat."""
    if scope in verdicts:
        return True
    last = verdicts.last_beat(scope) if isinstance(verdicts, ScopeVerdicts) else None
    return published_before_silence(last, last, published_at)


def known_gateways(info_rows) -> frozenset:
    """Every gateway the replica holds a ``gateway/info`` row for, readable or not. An offline owner keeps its row
    (its heartbeat just goes stale); a machine removed from the Board loses it."""
    return frozenset(str(gw) for gw, _raw in info_rows)


def fresh_scopes(heartbeats: dict[tuple[str, str, str], tuple[float, float]], now: float) -> ScopeVerdicts:
    """The ``scope_heartbeats`` keys ``fresh`` at ``now``, and the last beat of the others (``ScopeVerdicts``): all a
    replica projection learns from the heartbeats."""
    live = [key for key, (beat, window) in heartbeats.items() if fresh(beat, now, window)]
    return ScopeVerdicts(live, ((key, beat) for key, (beat, _window) in heartbeats.items()
                                if key not in set(live) and type(beat) in (int, float) and beat <= now))
