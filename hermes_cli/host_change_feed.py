"""One host change-feed thread per process that wakes subscribed loops (design t_01a1d563324d7e8e §6, card P). [daveey]

The hub gateway runs about a dozen background loops (sync, notifier, unloaded, remote index, links refresh,
change_watcher, session notifications, ...). Each wakes on its own timer, opens or probes its own inputs, and
then decides whether anything moved. This module gives them one shared probe instead: a single daemon thread
reads the host's change counters once per :data:`INTERVAL_S` and wakes only the loops subscribed to a counter
that moved.

The counters (topics):

* ``board:<path>``: each local board's ``kanban_activity_revision`` content key
  (``kanban_activity_revision.content_key``; bookkeeping-only writes such as claims, heartbeats and sync stamps
  do not move it).
* ``replica``: the hub replica's applied feed position, ``meta.cursor`` of hub.db. The hub client stamps it
  with the ``seq`` of the last change it applied, so it moves on every replica row written or deleted, and on
  nothing else (hub.db's ``data_version`` also moves on its own bookkeeping).
* ``state``: state.db ``PRAGMA data_version`` from a retained read-only observer
  (``tui_gateway.kanban_replica_observer``).

A loop subscribes once and replaces its sleep with :meth:`Subscription.wait` (threads) or
:meth:`Subscription.wait_async` (the gateway's asyncio loops), passing **its own next due time** as the
timeout. ``wait`` returns the topics that moved (non-empty) or an empty set when the due time passed with no
change, so each loop keeps every time-driven wakeup it acts on (renewals, retries, claim expiry, GC) and no
recovery cadence is lost.

**Fail-open.** A counter that cannot be read is *unknown*, and an unknown key never equals any other key, so
its subscribers are woken on every probe while it stays unreadable and do their own full pass, as they do
today. The first probe only sets the baseline: a loop runs its first pass on its own.

Every probe is one ``change_feed.probe`` pass in loop accounting (P1): a skip when nothing moved, a change
otherwise, with the number of moved topics as rows.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

INTERVAL_S = 1.0  # one probe of every counter per second
_SLICE_S = 1.0  # waits re-check their stop condition at least this often

BOARD = "board:"
REPLICA = "replica"
STATE = "state"


class _Unknown:
    """An unreadable counter: never equal to anything, itself included, so it always reads as moved."""

    __slots__ = ()

    def __eq__(self, other: object) -> bool:
        return False

    def __ne__(self, other: object) -> bool:
        return True

    __hash__ = object.__hash__

    def __repr__(self) -> str:
        return "<unknown>"


UNKNOWN = _Unknown()


def _matches(patterns: tuple[str, ...], topic: str) -> bool:
    return any(topic == p or (p.endswith("*") and topic.startswith(p[:-1])) for p in patterns)


class Subscription:
    """One loop's interest in a set of topics (exact names, or a prefix ending in ``*`` such as ``board:*``)."""

    def __init__(self, feed: HostChangeFeed, name: str, topics: Iterable[str]) -> None:
        self.feed = feed
        self.name = name
        self.topics = tuple(topics)
        self._cond = threading.Condition()
        self._pending: set[str] = set()
        self._async: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []
        self.closed = False

    def wants(self, topic: str) -> bool:
        return _matches(self.topics, topic)

    def _notify(self, topics: Iterable[str]) -> None:
        mine = {t for t in topics if self.wants(t)}
        if not mine:
            return
        with self._cond:
            self._pending |= mine
            self._cond.notify_all()
            waiters = list(self._async)
        for loop, event in waiters:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:  # that event loop is closed: its waiter is gone
                pass

    def _take(self) -> frozenset[str]:
        out = frozenset(self._pending)
        self._pending.clear()
        return out

    def pending(self) -> frozenset[str]:
        """The topics that moved since the last wait, without blocking (and consuming them)."""
        with self._cond:
            return self._take()

    def wait(self, timeout: Optional[float], *, stop: Optional[threading.Event] = None) -> frozenset[str]:
        """Block until a subscribed topic moves, ``timeout`` seconds pass (the caller's next due time), ``stop``
        is set or the subscription is closed. Returns the moved topics, or an empty set when woken by anything
        else. Changes that arrived while the loop was busy are returned at once."""
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._cond:
            while not self._pending and not self.closed and not (stop is not None and stop.is_set()):
                left = _SLICE_S if deadline is None else min(_SLICE_S, deadline - time.monotonic())
                if left <= 0:
                    break
                self._cond.wait(left)
            return self._take()

    async def wait_async(self, timeout: Optional[float], *,
                         running: Callable[[], bool] = lambda: True) -> frozenset[str]:
        """:meth:`wait` for an asyncio loop: never blocks the event loop; returns early once ``running()`` is
        false (checked at least every second, like the gateway's ``_sleep_between_ticks``)."""
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        deadline = None if timeout is None else loop.time() + max(0.0, timeout)
        with self._cond:
            if self._pending:
                return self._take()
            self._async.append((loop, event))
        try:
            while not self.closed and running():
                left = _SLICE_S if deadline is None else min(_SLICE_S, deadline - loop.time())
                if left <= 0:
                    break
                try:
                    await asyncio.wait_for(event.wait(), left)
                except TimeoutError:
                    continue
                with self._cond:
                    if self._pending:
                        break
                event.clear()
        finally:
            with self._cond:
                self._async = [w for w in self._async if w[1] is not event]
        with self._cond:
            return self._take()

    def close(self) -> None:
        self.feed.unsubscribe(self)
        with self._cond:
            self.closed = True
            self._cond.notify_all()
            waiters = list(self._async)
        for loop, event in waiters:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass


Probe = Callable[[], Mapping[str, object]]


class HostChangeFeed:
    """The process's change feed: ``probe()`` maps every topic to its current key; topics whose key moved since
    the last probe wake their subscribers. Started on the first :meth:`subscribe` (``autostart``)."""

    def __init__(self, probe: Optional[Probe] = None, *, interval: float = INTERVAL_S, autostart: bool = True,
                 name: str = "host-change-feed") -> None:
        self._probe = probe or default_probe
        self.interval = interval
        self.autostart = autostart
        self.name = name
        self._lock = threading.Lock()
        self._keys: Optional[dict[str, object]] = None  # None until the baseline probe
        self._subs: list[Subscription] = []
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.probes = 0
        self.wakes = 0

    def subscribe(self, name: str, topics: Iterable[str]) -> Subscription:
        sub = Subscription(self, name, topics)
        with self._lock:
            self._subs.append(sub)
        if self.autostart:
            self.start()
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            self._subs = [s for s in self._subs if s is not sub]

    def keys(self) -> dict[str, object]:
        with self._lock:
            return dict(self._keys or {})

    def poll_once(self) -> frozenset[str]:
        """One probe: read every counter, wake the subscribers of the topics that moved, return those topics.
        The first call only records the baseline. A probe that raises makes every known topic unknown."""
        current: dict[str, object]
        try:
            current = dict(self._probe())
        except Exception:  # health: allow BLE001 -- fail-open: an unreadable probe wakes every subscriber
            logger.debug("host change feed probe failed", exc_info=True)
            with self._lock:
                current = {t: UNKNOWN for t in (self._keys or {})}
        with self._lock:
            self.probes += 1
            previous, self._keys = self._keys, current
            if previous is None:
                return frozenset()
            moved = frozenset(t for t in current.keys() | previous.keys()
                              if t not in current or t not in previous or current[t] != previous[t])
            subs = list(self._subs)
        if moved:
            for sub in subs:
                if any(sub.wants(t) for t in moved):
                    self.wakes += 1
                    sub._notify(moved)
        return moved

    def _run(self) -> None:
        from hermes_cli import loop_accounting

        while not self._stop.is_set():
            try:
                with loop_accounting.pass_("change_feed.probe") as acct:
                    moved = self.poll_once()
                    if moved:
                        acct.change()
                        acct.rows(len(moved))
                    else:
                        acct.skip()
            except Exception:  # health: allow BLE001 -- the feed thread must outlive any one probe
                logger.debug("host change feed pass failed", exc_info=True)
            if self._stop.wait(self.interval):
                break

    def start(self) -> bool:
        """Start the probe thread once (idempotent). True when it is running."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop.clear()
            from agent.memory_provider import spawn_context_thread

            self._thread = spawn_context_thread(self._run, name=self.name)
            self._thread.start()
            return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            with sub._cond:
                sub._cond.notify_all()


# ---- default counters ---------------------------------------------------------------------------------------

_kept_lock = threading.Lock()
_kept: dict[str, tuple[sqlite3.Connection, tuple]] = {}  # path -> (read-only autocommit connection, (dev, ino))


def _kept_ro(path: Path) -> sqlite3.Connection:
    """A retained read-only autocommit connection to ``path``, reopened when the file is replaced."""
    real = os.path.realpath(path)
    st = os.stat(real)
    ident = (st.st_dev, st.st_ino)
    held = _kept.get(real)
    if held is not None and held[1] == ident:
        return held[0]
    if held is not None:
        _close_kept(real)
    conn = sqlite3.connect(f"file:{quote(real)}?mode=ro", uri=True, timeout=0.0, check_same_thread=False,
                           isolation_level=None)
    try:
        conn.execute("PRAGMA query_only = ON")
    except BaseException:
        conn.close()
        raise
    _kept[real] = (conn, ident)
    return conn


def _close_kept(real: str) -> None:
    held = _kept.pop(real, None)
    if held is not None:
        try:
            held[0].close()
        except sqlite3.Error:
            pass


def replica_key(hub: Path) -> object:
    """``(dev, ino, cursor)`` of hub.db (None when there is no replica), UNKNOWN when unreadable."""
    if not hub.is_file():
        return None
    with _kept_lock:
        try:
            conn = _kept_ro(hub)
            row = conn.execute("SELECT v FROM meta WHERE k = 'cursor'").fetchone()
            ident = _kept[os.path.realpath(hub)][1]
        except (OSError, sqlite3.Error):
            _close_kept(os.path.realpath(hub))
            return UNKNOWN
    return (*ident, None if row is None else str(row[0]))


def state_key(path: Path) -> object:
    """state.db's retained-observer ``(ident, generation, data_version)``; None when absent, UNKNOWN when unreadable."""
    if not path.is_file():
        return None
    from tui_gateway.kanban_replica_observer import fingerprint

    stamp = fingerprint(path)
    if stamp is None:
        return UNKNOWN
    return (stamp.ident, stamp.generation, stamp.data_version)


def board_keys() -> dict[str, object]:
    from hermes_cli import kanban_session_activity as ksa

    out: dict[str, object] = {}
    for path in ksa._board_db_paths():
        try:
            out[BOARD + str(path)] = ksa._board_content_key(path)
        except Exception:  # health: allow BLE001 -- fail-open: an unreadable board wakes its subscribers
            out[BOARD + str(path)] = UNKNOWN
    return out


def default_probe() -> dict[str, object]:
    """Every counter of this host: each local board's content key, hub.db's cursor, state.db's data_version."""
    from hermes_state import _default_db_path
    from tui_gateway.kanban_remote_notices import hub_db_path

    keys = board_keys()
    keys[REPLICA] = replica_key(hub_db_path())
    keys[STATE] = state_key(Path(_default_db_path()))
    return keys


_feed: Optional[HostChangeFeed] = None
_feed_lock = threading.Lock()


def feed() -> HostChangeFeed:
    """This process's feed over :func:`default_probe`. Under pytest it never starts its thread (tests drive
    :meth:`HostChangeFeed.poll_once`), so a feed left by one test cannot wake another test's loops."""
    global _feed
    with _feed_lock:
        if _feed is None:
            _feed = HostChangeFeed(autostart=not os.environ.get("PYTEST_CURRENT_TEST"))
        return _feed


def reset() -> None:
    """Stop the process feed and close the kept connections (tests, a backend forgetting its state)."""
    global _feed
    with _feed_lock:
        old, _feed = _feed, None
    if old is not None:
        old.stop()
    with _kept_lock:
        for real in list(_kept):
            _close_kept(real)
