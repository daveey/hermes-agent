"""Single-flight + short-TTL cache for ``GET /api/sessions`` (hermes-hub, card t_01a19f5af20a19f6). [daveey]

Desktop reconnects fan out ~15 identical list calls within 100 ms. Each one ran the recent-order ranking
query (~0.1-0.3 s on a 7 GB state.db) plus the activity annotations in its own threadpool slot, so the
burst queued behind itself: 2-7 s per call, 131 requests parked in ``run_in_threadpool``.

Identical calls now share one computation:

* **single-flight** -- a call whose key (params + state.db path) matches one already running, against the
  same data stamp, waits for that result instead of running the SQL again;
* **short TTL** -- a finished result is reused for ``TTL_S`` while the data stamp is unchanged.

The data stamp is SQLite's own WAL change counter: the ``iChange``/``mxFrame``/salt header at the start of
``<db>-shm``, which every write transaction bumps (both header copies must agree, or the read was torn and
nothing is cached), plus ``stat`` of the db and WAL files. So any write to state.db -- a new message, a
rename, an archive -- misses the cache at once. Annotations read from other stores (kanban.db activity, live turns, cron) can be as old as the
start of the computation they came from: up to ``TTL_S`` for a cache hit, plus that computation's run time
for a caller that joined it in flight. No readable stamp (no WAL, unreadable shm) means no
caching and no coalescing: every call computes, as before.
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Hashable, Optional

TTL_S = 2.0
MAX_ENTRIES = 64
# A follower never waits longer than this on a leader; then it computes itself.
FOLLOW_TIMEOUT_S = 30.0
_SHM_HEADER = 48  # one WalIndexHdr copy; the second copy follows at offset 48


def data_stamp(db_path: os.PathLike | str) -> Optional[tuple]:
    """Fingerprint that changes with every committed write to *db_path* (WAL mode), else ``None``."""
    db = Path(db_path)
    try:
        with open(f"{db}-shm", "rb") as f:
            head = f.read(2 * _SHM_HEADER)
        st_db = os.stat(db)
        st_wal = os.stat(f"{db}-wal")
    except OSError:
        return None
    if len(head) < 2 * _SHM_HEADER or head[:_SHM_HEADER] != head[_SHM_HEADER:]:
        return None  # torn read: a writer was mid-update (it writes copy 2, then copy 1)
    return (head[:_SHM_HEADER], st_db.st_mtime_ns, st_db.st_size, st_wal.st_mtime_ns, st_wal.st_size)


class _Flight:
    __slots__ = ("done", "error", "followers", "value")

    def __init__(self) -> None:
        self.followers = 0
        self.done = threading.Event()
        self.value: Any = None
        self.error: Optional[BaseException] = None


class SingleFlightCache:
    """``get(key, stamp, compute)``: one ``compute()`` per (key, stamp) at a time, reused for ``ttl_s``."""

    def __init__(self, *, ttl_s: float = TTL_S, max_entries: int = MAX_ENTRIES,
                 clock: Callable[[], float] = time.monotonic, copy: Callable[[Any], Any] = lambda v: v) -> None:
        self.ttl_s, self.max_entries, self.clock, self.copy = ttl_s, max_entries, clock, copy
        self._lock = threading.Lock()
        self._done: OrderedDict[Hashable, tuple] = OrderedDict()  # key -> (stamp, t, value)
        self._flights: dict = {}  # (key, stamp) -> _Flight
        self.computes = 0  # for tests and diagnostics

    def _count(self) -> None:
        with self._lock:
            self.computes += 1

    def get(self, key: Hashable, stamp: Optional[tuple], compute: Callable[[], Any]) -> Any:
        if stamp is None or self.ttl_s <= 0:
            self._count()
            return compute()
        fkey = (key, stamp)
        with self._lock:
            hit = self._done.get(key)
            if hit is not None and hit[0] == stamp and self.clock() - hit[1] < self.ttl_s:
                return self.copy(hit[2])
            flight = self._flights.get(fkey)
            leader = flight is None
            if leader:
                flight = self._flights[fkey] = _Flight()
            else:
                flight.followers += 1
        if not leader:
            if flight.done.wait(FOLLOW_TIMEOUT_S):
                if flight.error is not None:
                    raise flight.error
                return self.copy(flight.value)
            self._count()  # the leader hung past FOLLOW_TIMEOUT_S: answer on our own
            return compute()
        t0 = self.clock()
        self._count()
        try:
            flight.value = compute()
        except BaseException as exc:
            flight.error = exc
            raise
        finally:
            with self._lock:
                self._flights.pop(fkey, None)
                if flight.error is None:
                    # Expired entries go now, not only when the size cap evicts them.
                    for old in [k for k, (_s, t, _v) in self._done.items() if self.clock() - t >= self.ttl_s]:
                        del self._done[old]
                    self._done[key] = (stamp, t0, flight.value)
                    self._done.move_to_end(key)
                    while len(self._done) > self.max_entries:
                        self._done.popitem(last=False)
            flight.done.set()
        return self.copy(flight.value)

    def peek(self, key: Hashable, stamp: Optional[tuple]) -> tuple[bool, Any]:
        """Return a fresh completed result without joining a running computation."""
        if stamp is None or self.ttl_s <= 0:
            return False, None
        with self._lock:
            hit = self._done.get(key)
            if hit is not None and hit[0] == stamp and self.clock() - hit[1] < self.ttl_s:
                return True, self.copy(hit[2])
        return False, None

    def clear(self) -> None:
        with self._lock:
            self._done.clear()


def copy_session_list(value: Any) -> Any:
    """Per-caller copy of a ``/api/sessions`` payload: own top-level dict, list and row dicts, so one
    response's row edits can never leak into another's."""
    if not isinstance(value, dict):
        return value
    out = dict(value)
    rows = out.get("sessions")
    if isinstance(rows, list):
        out["sessions"] = [dict(r) if isinstance(r, dict) else r for r in rows]
    return out


SESSION_LIST_CACHE = SingleFlightCache(copy=copy_session_list)
