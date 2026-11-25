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

import contextlib
import logging
import math
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
            # The caller intentionally keeps the historical False-on-error
            # result, but a native holder needs a retry even without a new write.
            from hermes_cli import kanban_annotation_host as host
            host.note_deadline(host.time.time() + host.HOLDER_INTERVAL_S)
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
        self.windows: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self.counts.clear()
            self.verdicts.clear()
            self.windows.clear()

    def get(self, scope: tuple[str, ...], mono: float, key: Any = None,
            *, wall: float | None = None) -> dict[str, int] | None:
        """The cached counts within the TTL; past it, only while ``key`` (not None) equals the key they were read
        under and they are younger than ``max_age_s``."""
        with self._lock:
            hit = self.counts.get(scope)
            window = self.windows.get(scope)
        if wall is not None and window is not None and wall >= window:
            return None
        if hit is None:
            return None
        if mono < hit[0]:
            return hit[1]
        if key is not None and key == hit[2] and mono < hit[3] + self.max_age_s:
            return hit[1]
        return None

    def deadline(self, scope: tuple[str, ...]) -> float | None:
        """The wall-time window boundary, including on a cached-count hit."""
        with self._lock:
            return self.windows.get(scope)

    def put(self, scope: tuple[str, ...], mono: float, counts: dict[str, int], key: Any = None,
            *, window: float | None = None) -> None:
        with self._lock:
            self.counts[scope] = (mono + self.ttl_s, counts, key, mono)
            self.windows.pop(scope, None)
            if window is not None:
                self.windows[scope] = window
            for k in [k for k, v in self.counts.items() if v[3] + self.max_age_s <= mono]:
                self.counts.pop(k, None)
            for k in [k for k, v in self.verdicts.items() if v[0] <= mono]:
                self.verdicts.pop(k, None)
            while len(self.counts) > self.max_scopes:
                self.counts.pop(next(iter(self.counts)))
            for k in set(self.windows) - self.counts.keys():
                self.windows.pop(k, None)

    def verdict(self, key: tuple, mono: float, compute: Callable[[], bool], *, force: bool = False) -> bool:
        with self._lock:
            memo = self.verdicts.get(key)
        if not force and memo is not None and mono < memo[0]:
            return memo[1]
        flagged = compute()
        with self._lock:
            self.verdicts[key] = (mono + self.ttl_s, flagged)
        return flagged


def register_done_check(name: str, fn: Callable[[dict[str, Any]], bool], *,
                        watch_paths=None, next_deadline=None, inputs=None) -> Callable[[], None]:
    """Register ``fn(card) -> bool`` (True = this completed card needs attention). ``card`` has ``id``, ``title``,
    ``summary`` (latest run summary, else ``result``), ``completed_at``, ``board`` and ``board_path``. Re-registering a
    name replaces it; the returned callable unregisters. Optional event callbacks
    have the same no-DB-read contract as ``register_waiting_source``; undeclared
    checks retain bounded fallback rather than silently missing external inputs.
    A database path is event coverage, not a complete semantic declaration: the holder rereads undeclared
    database-dependent checks, since plugin code may select arbitrary events, comments or tables. File-only
    legacy declarations retain literal change signatures.

    ``inputs()`` optionally declares the complete portable semantic key (see ``kanban_annotation_done_inputs``); it
    is bound to this ``fn`` and, when absent or failing, callers recompute fresh."""
    from hermes_cli import kanban_annotation_done_inputs as done_inputs
    from hermes_cli import kanban_session_activity as activity
    activity._host_copy.declare_events(name, fn, watch_paths, next_deadline, done_check=True)
    done_inputs.declaration(name, fn, inputs)
    activity._done_checks[name] = fn
    activity._attention.clear()
    activity._host_copy.wake_holder()

    def _dispose() -> None:
        if activity._done_checks.get(name) is fn:
            activity._done_checks.pop(name, None)
            done_inputs.dispose(name, fn)
            activity._host_copy.declare_events(name, None, None, None, done_check=True)
            activity._attention.clear()
            activity._host_copy.wake_holder()

    return _dispose


def _check_passes(checks) -> contextlib.ExitStack:
    """One read pass for every check that offers one: a check function may carry ``attention_pass``, a context
    manager factory inside which its calls share their reads (kanban-honest-done: one connection per database
    instead of several per card). Entered around all the cards of one count and closed with it, so nothing is held
    between counts. A pass that cannot be entered is skipped: the check then reads on its own, as before."""
    stack = contextlib.ExitStack()
    for check in checks:
        factory = getattr(check, "attention_pass", None)
        if callable(factory):
            try:
                stack.enter_context(factory())
            except Exception:  # health: allow BLE001 -- a plugin's pass is an optimisation, never a requirement
                _log.debug("kanban done check pass unavailable", exc_info=True)
    return stack


def _close_passes(passes: contextlib.ExitStack) -> None:
    """Close every entered pass. A pass whose exit raises is logged like one that cannot be entered: the checks
    have already answered, and their verdicts must not be lost to a plugin's failing close."""
    try:
        passes.close()
    except Exception:  # health: allow BLE001 -- a plugin's pass is an optimisation, never a requirement
        _log.debug("kanban done check pass close failed", exc_info=True)


def attention_task_counts(now: float | None = None) -> dict[str, int]:
    """``{session_id: completed cards a registered check flags}`` over the last ``activity.ATTENTION_WINDOW_S``. Cached;
    a check that raises counts as False."""
    from hermes_cli import kanban_session_activity as activity
    if not activity._done_checks:
        return {}
    scope = activity._origin_scope()
    mono = activity.time.monotonic()
    wall = activity.time.time() if now is None else now
    forced = activity._revalidate.forced(scope, 'attention')
    hit = None if forced else activity._attention.get(scope, mono, wall=wall)
    if hit is not None:
        activity._host_copy.note_deadline(activity._attention.deadline(scope))
        return hit
    inputs = activity._revalidate.attention_key()
    hit = None if forced else activity._attention.get(scope, mono, inputs, wall=wall)
    if hit is not None:
        activity._host_copy.note_deadline(activity._attention.deadline(scope))
        return hit
    since = wall - activity.ATTENTION_WINDOW_S
    checks = list(activity._done_checks.values())
    paths = list(activity._board_db_paths())
    boards = tuple(str(p) for p in paths)
    pairs: set[tuple[str, str]] = set()
    windows: list[float] = []
    passes = _check_passes(checks)
    try:
        _flag_recent_done(activity, paths, since, checks, boards, scope, mono, forced, pairs, windows)
    finally:
        _close_passes(passes)
    counts: dict[str, int] = {}
    for sid, _tid in pairs:
        counts[sid] = counts.get(sid, 0) + 1
    window = min(windows, default=None)
    activity._attention.put(scope, mono, counts, inputs, window=window)
    activity._host_copy.note_deadline(window)
    return counts


def _flag_recent_done(activity, paths, since, checks, boards, scope, mono, forced, pairs, windows) -> None:
    """Add the flagged ``(session, card)`` of every board's recent done cards to ``pairs`` and each card's window
    boundary to ``windows``."""
    for path in paths:
        try:
            recent = activity._recent_done.memoized(path, since, activity._read_recent_done, activity._revalidate.board_memo_key, scope)  # [daveey]
        except activity.sqlite3.Error as exc:
            activity._log.debug("kanban attention read failed for %s: %s", path, exc)
            # A swallowed read failure must own a retry: native notifications
            # need not arrive again when a transient SQLite lock clears.
            activity._host_copy.note_deadline(activity.time.time() + activity._host_copy.HOLDER_INTERVAL_S)
            continue
        for sid, card in recent:
            completed = card.get('completed_at')
            if isinstance(completed, (int, float)) and math.isfinite(completed):
                # Inclusive cutoff: advance both operands so subtraction at the
                # emitted wake cannot round back onto the completion timestamp.
                windows.append(math.nextafter(math.nextafter(completed, math.inf) + activity.ATTENTION_WINDOW_S,
                                              math.inf))
            key = (boards, str(path), str(card["id"]), card.get("completed_at"), card.get("title"), card.get("summary"))
            if activity._attention.verdict(key, mono, lambda card=card: activity._run_done_checks(checks, card), force=forced):
                pairs.add((sid, key[2]))
