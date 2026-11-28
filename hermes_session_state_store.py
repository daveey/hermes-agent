"""SessionStateStore: the gateway's one writer of SessionState v2 (design items 2-5). [daveey]

Shadow mode (P1): runs beside the v1 producers; nothing renders from it. Per lineage it keeps one persisted row in
state.db (``session_state``), recomputed in full by the pluggable pure reducer whenever an event names that lineage:

* **index**: ``session_state_cards`` maps every card key the lineage's object shows (linked cards and their
  dependency closure, local and replica) to the lineage, and ``session_state_alias`` maps every session id of the
  lineage to it, so an event about a card or any session id resolves to the objects it changes without a scan.
* **rev**: one store-wide monotonic counter (``session_state_meta.rev``), persisted, bumped only when an object's
  digest (canonical JSON without ``rev``/``computed_at``) changes; equal recomputes never churn it.
* **timer**: each object's ``next_change_at`` is the only timer: when it falls due the lineage is recomputed.
* **reconciliation**: every :data:`RECONCILE_S` every persisted lineage is recomputed; a digest change there was a
  missed event, logged and counted (``metrics()['reconcile_diffs']``).
* **divergence**: each recompute compares the v2 header with the stored v1 ``outcome_json.status`` and appends a
  bounded JSONL record on disagreement (``logs/session-state-divergence.jsonl``).

Event sources call :meth:`emit_session`/:meth:`emit_card` (via ``hermes_session_state_events``); one worker thread
coalesces them. Readers (:meth:`get`, :meth:`changes_since`) only read persisted rows: they never collect.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

RECONCILE_S = 300.0
DIVERGENCE_MAX_BYTES = 2 * 1024 * 1024
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS session_state (
        lineage TEXT PRIMARY KEY, tip TEXT NOT NULL, rev INTEGER NOT NULL, digest TEXT NOT NULL,
        body TEXT NOT NULL, computed_at REAL NOT NULL, next_change_at REAL)""",
    "CREATE INDEX IF NOT EXISTS idx_session_state_rev ON session_state(rev)",
    "CREATE TABLE IF NOT EXISTS session_state_alias (session_id TEXT PRIMARY KEY, lineage TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS session_state_cards (
        card_key TEXT NOT NULL, lineage TEXT NOT NULL, PRIMARY KEY (card_key, lineage))""",
    "CREATE INDEX IF NOT EXISTS idx_session_state_cards_lineage ON session_state_cards(lineage)",
    """CREATE TABLE IF NOT EXISTS session_state_resolutions (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, lineage TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
        body TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_session_state_resolutions_lineage ON session_state_resolutions(lineage)",
    "CREATE TABLE IF NOT EXISTS session_state_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)
_DIVERGENT = {  # v1 status kind -> v2 header statuses that agree with it
    "running": {"running"}, "waiting": {"waiting"}, "decision": {"needs_you", "waiting"},
    "done": {"done"}, "muted": {"muted"}, "unresolved": {"stuck", "attention", "unknown", "idle", "needs_you"},
}


def digest(obj: dict[str, Any]) -> str:
    """SHA-256 of the canonical object without its store-owned fields."""
    from hermes_session_state_schema import STORE_OWNED

    semantic = {k: v for k, v in obj.items() if k not in STORE_OWNED}
    return hashlib.sha256(json.dumps(semantic, sort_keys=True, separators=(",", ":"), default=str)
                          .encode("utf-8")).hexdigest()


class SessionStateStore:
    """See the module doc. ``db`` is a ``SessionDB``; ``collect(db, tip, trigger=)`` returns reducer inputs."""

    def __init__(self, db: Any, *, collect: Optional[Callable[..., dict]] = None,
                 reduce: Optional[Callable[..., Any]] = None, clock: Callable[[], float] = time.time,
                 divergence_path: Optional[Path] = None, reconcile_s: float = RECONCILE_S) -> None:
        from hermes_session_state_collect import collect as default_collect
        from hermes_session_state_reducer import reduce as default_reduce

        self.db = db
        self._collect = collect or default_collect
        self._reduce = reduce or default_reduce
        self._clock = clock
        self._divergence_path = divergence_path
        self._reconcile_s = reconcile_s
        self._compute_lock = threading.RLock()  # one recompute (collect -> reduce -> persist -> push) at a time
        self._cv = threading.Condition()
        self._pending: dict[str, str] = {}  # session id -> trigger
        self._timers: dict[str, float] = {}  # lineage -> next_change_at
        self._subscribers: list[Callable[[dict], None]] = []
        self._ticks: list[Callable[[], None]] = []  # watcher polls run on the worker thread
        self._tick_s = 0.1
        self._thread: Optional[threading.Thread] = None
        self._stopping = False
        self._next_reconcile = self._clock() + reconcile_s
        self._metrics = {"recomputes": 0, "published": 0, "unchanged": 0, "errors": 0, "reconcile_runs": 0,
                         "reconcile_diffs": 0, "divergences": 0, "timer_fires": 0}
        self._ensure()
        self._load_timers()

    # -- schema / persistence -------------------------------------------------------------------------------
    def _ensure(self) -> None:
        def write(conn):
            for statement in _SCHEMA:
                conn.execute(statement)
        self.db._execute_write(write)

    def _load_timers(self) -> None:
        with self.db._read_ctx() as conn:
            rows = conn.execute("SELECT lineage, next_change_at FROM session_state "
                                "WHERE next_change_at IS NOT NULL").fetchall()
        self._timers = {str(r[0]): float(r[1]) for r in rows}

    def current_rev(self) -> int:
        with self.db._read_ctx() as conn:
            row = conn.execute("SELECT value FROM session_state_meta WHERE key = 'rev'").fetchone()
        return int(row[0]) if row else 0

    def lineage_for(self, session_id: str) -> Optional[str]:
        with self.db._read_ctx() as conn:
            row = conn.execute("SELECT lineage FROM session_state_alias WHERE session_id = ?",
                               (session_id,)).fetchone()
        return str(row[0]) if row else None

    def lineages_for_card(self, card_key: str, *, task_id: Optional[str] = None) -> list[str]:
        """Lineages whose object shows ``card_key``; with ``task_id`` also any key ending in ``/<task_id>``
        (a replica row names the card by id; its owner-qualified key is the replica's)."""
        suffix = f"/{task_id}" if task_id else "\x00"
        with self.db._read_ctx() as conn:
            return [str(r[0]) for r in conn.execute(
                "SELECT DISTINCT lineage FROM session_state_cards WHERE card_key = ? "
                "OR substr(card_key, -length(?)) = ? ORDER BY lineage", (card_key, suffix, suffix))]

    def get(self, session_id: str) -> Optional[dict[str, Any]]:
        """The persisted object of the lineage ``session_id`` belongs to (any id of it), or None. No compute."""
        with self.db._read_ctx() as conn:
            row = conn.execute(
                "SELECT s.body FROM session_state s WHERE s.lineage = COALESCE("
                "(SELECT lineage FROM session_state_alias WHERE session_id = ?), ?)",
                (session_id, session_id)).fetchone()
        return json.loads(row[0]) if row else None

    def changes_since(self, rev: int, lineages: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
        """Every persisted object whose ``rev`` is above ``rev`` (optionally only these lineages), oldest first."""
        sql = "SELECT body FROM session_state WHERE rev > ?"
        args: list[Any] = [int(rev)]
        if lineages is not None:
            sql += " AND lineage IN (SELECT value FROM json_each(?))"
            args.append(json.dumps(sorted(set(lineages))))
        with self.db._read_ctx() as conn:
            return [json.loads(r[0]) for r in conn.execute(sql + " ORDER BY rev", args)]

    def resolutions(self, lineage: str) -> list[dict[str, Any]]:
        with self.db._read_ctx() as conn:
            return [json.loads(r[0]) for r in conn.execute(
                "SELECT body FROM session_state_resolutions WHERE lineage = ? ORDER BY seq", (lineage,))]

    def add_resolution(self, lineage: str, resolution: dict[str, Any]) -> bool:
        """Record one request's resolution (idempotent per ``request_id``); False when it was already recorded."""
        def write(conn):
            cur = conn.execute("INSERT OR IGNORE INTO session_state_resolutions (lineage, request_id, body) "
                               "VALUES (?, ?, ?)", (lineage, resolution["request_id"],
                                                    json.dumps(resolution, sort_keys=True)))
            return cur.rowcount > 0
        return bool(self.db._execute_write(write))

    # -- recompute ------------------------------------------------------------------------------------------
    def recompute(self, session_id: str, *, trigger: str = "manual") -> Optional[dict[str, Any]]:
        """Full recompute of the lineage ``session_id`` belongs to; returns the object now persisted (pushed to
        subscribers when its digest changed). None when the session does not exist."""
        with self._compute_lock:
            self._metrics["recomputes"] += 1
            tip = self._tip(session_id)
            if tip is None:
                return None
            now = self._clock()
            inputs = self._collect(self.db, tip, trigger=trigger)
            lineage = str(inputs["lineage"])
            state = self._reduce(inputs, now=now, resolutions=self.resolutions(lineage))
            obj = state.to_dict() if hasattr(state, "to_dict") else dict(state)
            obj["computed_at"] = now
            published, obj = self._persist(lineage, tip, obj, inputs)
            if published:
                self._metrics["published"] += 1
                self._push(obj)
            else:
                self._metrics["unchanged"] += 1
            self._set_timer(lineage, obj.get("next_change_at"))
            self._diverge(obj, inputs.get("stored_status"))
            return obj

    def _tip(self, session_id: str) -> Optional[str]:
        lineage = self.lineage_for(session_id) or session_id
        tip = self.db.get_compression_tip(lineage) or lineage
        if self.db._read_one("SELECT 1 FROM sessions WHERE id = ?", (tip,)) is None:
            return None
        return tip

    def _persist(self, lineage: str, tip: str, obj: dict, inputs: dict) -> tuple[bool, dict]:
        new_digest = digest(obj)
        ids = sorted({lineage, tip, *(str(i) for i in (inputs.get("parts") or {}).get("ids") or ())})
        cards = sorted(obj.get("cards") or {})

        def unchanged(conn) -> Optional[dict]:
            """The persisted object when nothing (object, card index, aliases) would change, else None."""
            row = conn.execute("SELECT digest, body FROM session_state WHERE lineage = ?", (lineage,)).fetchone()
            if row is None or row[0] != new_digest:
                return None
            have_cards = [str(r[0]) for r in conn.execute(
                "SELECT card_key FROM session_state_cards WHERE lineage = ? ORDER BY card_key", (lineage,))]
            have_ids = {str(r[0]): str(r[1]) for r in conn.execute(
                "SELECT session_id, lineage FROM session_state_alias WHERE session_id IN "
                "(SELECT value FROM json_each(?))", (json.dumps(ids),))}
            if have_cards != cards or have_ids != {sid: lineage for sid in ids}:
                return None
            return json.loads(row[1])

        # Digest no-churn means no write at all: a no-op recompute must not touch state.db (WAL churn on every
        # event, and a live writer under backup/recovery source-unchanged checks).
        with self.db._read_ctx() as conn:
            kept = unchanged(conn)
        if kept is not None:
            return False, kept

        def write(conn):
            row = conn.execute("SELECT digest, body FROM session_state WHERE lineage = ?", (lineage,)).fetchone()
            conn.execute("DELETE FROM session_state_cards WHERE lineage = ?", (lineage,))
            conn.executemany("INSERT OR IGNORE INTO session_state_cards (card_key, lineage) VALUES (?, ?)",
                             [(key, lineage) for key in cards])
            conn.executemany("INSERT OR REPLACE INTO session_state_alias (session_id, lineage) VALUES (?, ?)",
                             [(sid, lineage) for sid in ids])
            if row is not None and row[0] == new_digest:
                kept = json.loads(row[1])
                return False, kept
            meta = conn.execute("SELECT value FROM session_state_meta WHERE key = 'rev'").fetchone()
            rev = (int(meta[0]) if meta else 0) + 1
            conn.execute("INSERT OR REPLACE INTO session_state_meta (key, value) VALUES ('rev', ?)", (str(rev),))
            out = {**obj, "rev": rev}
            conn.execute("INSERT OR REPLACE INTO session_state (lineage, tip, rev, digest, body, computed_at, "
                         "next_change_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (lineage, tip, rev, new_digest, json.dumps(out, sort_keys=True), obj["computed_at"],
                          obj.get("next_change_at")))
            return True, out
        return self.db._execute_write(write)

    def _set_timer(self, lineage: str, due: Any) -> None:
        with self._cv:
            if isinstance(due, (int, float)) and not isinstance(due, bool):
                self._timers[lineage] = float(due)
            else:
                self._timers.pop(lineage, None)
            self._cv.notify_all()

    def _diverge(self, obj: dict, stored: Optional[dict]) -> None:
        kind = stored.get("kind") if isinstance(stored, dict) else None
        status = (obj.get("header") or {}).get("status")
        if kind is None or status in _DIVERGENT.get(kind, {status}):
            return
        self._metrics["divergences"] += 1
        record = {"at": obj.get("computed_at"), "lineage": obj["identity"]["lineage"], "rev": obj.get("rev"),
                  "v1": {"kind": kind, "reason": stored.get("reason")}, "v2": status}
        logger.info("session state divergence: %s", record)
        path = self._divergence_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > DIVERGENCE_MAX_BYTES:
                path.replace(path.with_suffix(".1.jsonl"))
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError:
            logger.debug("session state divergence log unwritable", exc_info=True)

    # -- subscribers ----------------------------------------------------------------------------------------
    def subscribe(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        with self._cv:
            self._subscribers.append(fn)

        def cancel() -> None:
            with self._cv:
                if fn in self._subscribers:
                    self._subscribers.remove(fn)
        return cancel

    def _push(self, obj: dict) -> None:
        with self._cv:
            subscribers = list(self._subscribers)
        for fn in subscribers:
            try:
                fn(obj)
            except Exception:  # health: allow BLE001 -- one bad subscriber never stops publication
                logger.debug("session state subscriber failed", exc_info=True)

    # -- events / worker ------------------------------------------------------------------------------------
    def emit_session(self, session_id: str, trigger: str = "manual") -> None:
        if not session_id:
            return
        with self._cv:
            self._pending.setdefault(str(session_id), trigger)
            self._cv.notify_all()

    def emit_card(self, board: str, task_id: str, sessions: Iterable[str] = (), *, key: Optional[str] = None,
                  trigger: str = "card_change", by_id: bool = False) -> None:
        """A card changed: recompute every lineage whose object shows it (index) and every given linked chat."""
        for lineage in self.lineages_for_card(key or f"{board}/{task_id}", task_id=task_id if by_id else None):
            self.emit_session(lineage, trigger)
        for sid in sessions:
            self.emit_session(sid, trigger)

    def add_tick(self, fn: Callable[[], None]) -> None:
        self._ticks.append(fn)

    def metrics(self) -> dict[str, int]:
        return dict(self._metrics)

    def start(self, *, tick_s: float = 0.1) -> None:
        self._tick_s = tick_s
        if self._thread is None:
            from agent.memory_provider import spawn_context_thread

            self._thread = spawn_context_thread(self._run, name="session-state", daemon=True, passive=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._cv:
            self._stopping = True
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def _due(self, now: float) -> list[str]:
        with self._cv:
            due = [lineage for lineage, at in self._timers.items() if at <= now]
            for lineage in due:
                self._timers.pop(lineage, None)
        return due

    def run_once(self) -> int:
        """One worker pass: watcher ticks, due timers, the reconciliation sweep when due, then every pending
        recompute. Returns how many lineages it recomputed (tests drive this directly)."""
        for tick in list(self._ticks):
            try:
                tick()
            except Exception:  # health: allow BLE001 -- a broken watcher is logged, the others keep running
                logger.warning("session state watcher failed", exc_info=True)
        now = self._clock()
        for lineage in self._due(now):
            self._metrics["timer_fires"] += 1
            self.emit_session(lineage, "timer")
        if now >= self._next_reconcile:
            self._next_reconcile = now + self._reconcile_s
            self.reconcile()
        with self._cv:
            pending, self._pending = self._pending, {}
        done = 0
        for sid, trigger in pending.items():
            try:
                if self.recompute(sid, trigger=trigger) is not None:
                    done += 1
            except Exception:  # health: allow BLE001 -- one failed lineage is retried by the next event/sweep
                self._metrics["errors"] += 1
                logger.warning("session state recompute failed for %s", sid, exc_info=True)
        return done

    def reconcile(self) -> int:
        """Recompute every persisted lineage; a digest change here is a missed event (logged, counted)."""
        self._metrics["reconcile_runs"] += 1
        with self.db._read_ctx() as conn:
            rows = [(str(r[0]), str(r[1])) for r in conn.execute("SELECT lineage, digest FROM session_state")]
        diffs = 0
        for lineage, before in rows:
            try:
                obj = self.recompute(lineage, trigger="reconcile")
            except Exception:  # health: allow BLE001 -- the sweep continues past one failing lineage
                self._metrics["errors"] += 1
                logger.warning("session state reconcile failed for %s", lineage, exc_info=True)
                continue
            if obj is not None and digest(obj) != before:
                diffs += 1
                logger.info("session state reconcile diff: lineage=%s rev=%s", lineage, obj.get("rev"))
        self._metrics["reconcile_diffs"] += diffs
        return diffs

    def _wait_s(self) -> float:
        now = self._clock()
        deadline = min([self._next_reconcile, *self._timers.values()]) if self._timers else self._next_reconcile
        return max(0.0, min(self._tick_s, deadline - now))

    def _run(self) -> None:
        while True:
            with self._cv:
                if self._stopping:
                    return
                if not self._pending:
                    self._cv.wait(self._wait_s())
                if self._stopping:
                    return
            self.run_once()


_store: list[Optional[SessionStateStore]] = [None]


def current() -> Optional[SessionStateStore]:
    """The process's running store (None outside a serving gateway / when disabled)."""
    return _store[0]


def install(store: Optional[SessionStateStore]) -> Optional[SessionStateStore]:
    previous, _store[0] = _store[0], store
    return previous
