"""Attention-count caches for ``kanban_session_activity``: per origin scope, with shared done-check verdicts. [daveey]

One process annotates several profiles in one request (``/api/profiles/projects/tree`` walks every profile). A
single-slot cache was evicted by every other scope, so its 30 s TTL never hit: each tree poll re-ran every done check
for every profile (48 board reads, 2-2.5 s on the hub) and evicted the default scope, whose next
``/api/sessions/<id>`` re-ran them too.

A count past its TTL is still served while the boards it read are unchanged (same content ``key``) and it is younger
than ``max_age_s``: the done checks read only the card and the boards, so with no board write their verdicts cannot
have moved. ``max_age_s`` bounds what the key cannot see (a card leaving the ``ATTENTION_WINDOW_S`` window). On
David-MBP every expiry re-ran every check (~125 ms of CPU each 30 s) while no local board changed for minutes.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

_log = logging.getLogger("hermes_cli.kanban_session_activity")


def run_checks(checks: list[Callable[[dict[str, Any]], bool]], card: dict[str, Any]) -> bool:
    """True when any check flags ``card``; a check that raises counts as False."""
    for check in checks:
        try:
            if check(card):
                return True
        except Exception:  # health: allow BLE001 -- a registered plugin check may raise anything; it counts as False
            _log.debug("kanban done check failed on %s", card.get("id"), exc_info=True)
    return False


class AttentionCache:
    """``counts``: ``{scope: (expires_mono, counts, key, read_mono)}``, bounded at ``max_scopes``. ``verdicts``:
    ``{(boards, board_path, id, completed_at, title, summary): (expires_mono, flagged)}``. A check reads only the
    card and the boards, so another profile on the same kanban root gets the same verdict within the TTL."""

    def __init__(self, ttl_s: float, max_scopes: int = 64, max_age_s: float | None = None) -> None:
        self.ttl_s = ttl_s
        self.max_age_s = ttl_s if max_age_s is None else max(ttl_s, max_age_s)
        self.max_scopes = max_scopes
        self.counts: dict[tuple[str, ...], tuple[float, dict[str, int], Any, float]] = {}
        self.verdicts: dict[tuple, tuple[float, bool]] = {}
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self.counts.clear()
            self.verdicts.clear()

    def get(self, scope: tuple[str, ...], mono: float, key: Any = None) -> dict[str, int] | None:
        """The cached counts within the TTL; past it, only while ``key`` (not None) equals the key they were read
        under and they are younger than ``max_age_s``."""
        with self._lock:
            hit = self.counts.get(scope)
        if hit is None:
            return None
        if mono < hit[0]:
            return hit[1]
        if key is not None and key == hit[2] and mono < hit[3] + self.max_age_s:
            return hit[1]
        return None

    def put(self, scope: tuple[str, ...], mono: float, counts: dict[str, int], key: Any = None) -> None:
        with self._lock:
            self.counts[scope] = (mono + self.ttl_s, counts, key, mono)
            for k in [k for k, v in self.counts.items() if v[3] + self.max_age_s <= mono]:
                self.counts.pop(k, None)
            for k in [k for k, v in self.verdicts.items() if v[0] <= mono]:
                self.verdicts.pop(k, None)
            while len(self.counts) > self.max_scopes:
                self.counts.pop(next(iter(self.counts)))

    def verdict(self, key: tuple, mono: float, compute: Callable[[], bool]) -> bool:
        with self._lock:
            memo = self.verdicts.get(key)
        if memo is not None and mono < memo[0]:
            return memo[1]
        flagged = compute()
        with self._lock:
            self.verdicts[key] = (mono + self.ttl_s, flagged)
        return flagged
