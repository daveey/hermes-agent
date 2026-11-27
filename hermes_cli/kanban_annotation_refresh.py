"""Bounded annotation refresh: a poll with a previous read never waits out a slow new one. [daveey]

On hermes-hub the one-row ``GET /api/sessions/<id>`` found the refresh lock free and ran the whole annotation read
(counts, remote-links projection, waiting sources) on its own worker. Inside a Desktop poll burst (15 concurrent
session-list reads) that took 5.3 s and the Desktop's 5 s request timeout fired (2026-10-31 23:33 UTC,
t_01a19451dffa47a8). ``refresh_bounded`` runs the read on a helper thread and waits a bounded time for it; a read
that finishes in time is returned (read-your-writes for the common fast read), a slower one leaves the previous read
as the answer and publishes itself for the next poll.
"""

from __future__ import annotations

from contextlib import contextmanager
import contextvars
import logging
import threading
import time
from typing import Any, Callable

_log = logging.getLogger("hermes_cli.kanban_session_activity")

COPY_FIRST_MAX_AGE_S = 30.0
_capture_role = contextvars.ContextVar('annotation_capture_role', default=False)


@contextmanager
def capture_role():
    """Scope the plugin-incomplete child's reads, including bounded helper threads."""
    token = _capture_role.set(True)
    try:
        yield
    finally:
        _capture_role.reset(token)


def in_capture_role() -> bool:
    return _capture_role.get()


# Wall time each published parts tuple was read (by identity): a served previous read keeps its own date.
_read_at: dict[tuple[str, ...], tuple[tuple, float]] = {}


def record(scope: tuple[str, ...], parts: tuple, at: float) -> None:
    _read_at[scope] = (parts, at)


class ObservedParts(tuple):
    """An immutable host read carrying its own date, independent of a concurrently publishing scope cache."""

    observed_at: float

    def __new__(cls, parts: tuple, observed_at: float):
        value = super().__new__(cls, parts)
        value.observed_at = observed_at
        return value


def read_at(parts: Any) -> float | None:
    """Wall time this process read ``parts`` (None for a loaded copy or an unknown tuple)."""
    if isinstance(parts, ObservedParts):
        return parts.observed_at
    for read, at in list(_read_at.values()):
        if read is parts:
            return at
    return None


def unavailable(parts: tuple | None, display: bool, observed_at: float | None) -> bool:
    """Match the Desktop's persisted observation horizon; expiry is unknown, never empty/done."""
    return parts is None or (display and observed_at is not None and time.time() - observed_at > 180.0)


def recent_copy(candidate: tuple | None, saved_at: Callable[[Any], float | None]) -> tuple | None:
    """``candidate`` when the capture child may serve it at once instead of starting a refresh it cannot finish.

    A publication-age shortcut, not a data-age guarantee: a restart copy carries its SAVE time (``saved_at``), a
    previous read its read time. Rows keep the copy's own date (``copy_at`` in ``annotate_kanban_activity``). Past
    ``COPY_FIRST_MAX_AGE_S``, or with an unknown or future date, the caller runs the ordinary bounded refresh, and
    the 180 s display horizon (:func:`unavailable`) still marks expired rows unknown.
    """
    if candidate is None:
        return None
    date = saved_at(candidate)
    if date is None:
        date = read_at(candidate)
    return candidate if date is not None and 0 <= time.time() - date <= COPY_FIRST_MAX_AGE_S else None


def reset() -> None:
    _read_at.clear()


def _real_thread_class() -> type:
    # Tests stub ``threading.Thread``; this read must run on a real thread whatever is installed.
    return next(c for c in type(threading.main_thread()).__mro__ if c.__name__ == "Thread")


def refresh_bounded(refresh: Callable[[], tuple], lock: Any, previous: tuple | None, wait_s: float) -> tuple | None:
    """Run ``refresh`` on a helper thread that owns the already-held ``lock`` and releases it. Return its result
    (or raise its error) when it finishes within ``wait_s``; otherwise return ``previous``."""
    done = threading.Event()
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["parts"] = refresh()
        except BaseException as exc:  # health: allow BLE001 -- handed to the waiting caller or logged below
            box["error"] = exc
        finally:
            lock.release()
            done.set()
            if "error" in box and box.get("abandoned"):
                _log.warning("kanban annotation background refresh failed; previous read kept",
                             exc_info=box["error"])

    ctx = contextvars.copy_context()
    try:
        _real_thread_class()(target=ctx.run, args=(run,), daemon=True, name="kanban-annotation-refresh").start()
    except BaseException:
        lock.release()
        raise
    if done.wait(wait_s):
        if "error" in box:
            raise box["error"]
        return box["parts"]
    box["abandoned"] = True
    if done.is_set() and "error" in box:
        raise box["error"]
    return box.get("parts", previous)
