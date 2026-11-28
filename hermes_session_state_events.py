"""SessionState v2 event sources (design item 3). [daveey]

Every source becomes ``store.emit_session``/``store.emit_card``; the store recomputes each affected lineage in full.

* :func:`emit_session` -- in-process facade (never raises) called by ``hermes_state_outcome.refresh_outcome``, which
  every turn start/end, subagent reap, notice and mute already funnels through.
* :class:`BoardWatcher` -- every committed ``task_events`` row of every local board and every Board replica
  (``kanban/sync-shadow/*.db``), whichever process wrote it: a retained read-only ``PRAGMA data_version`` observer
  per file, then a per-file event-id cursor. Replica rows also refresh the per-session remote-card listing (cards on
  other gateways linked to this host's chats), so a newly synced link reaches its chat without a prior index row.
* :class:`StateWatcher` -- state.db commits by other processes: new transcript rows (turns, subagent children,
  whose parent is emitted too) and stored-outcome changes.
* :class:`ProcessWatcher` -- the process registry's content-free lifecycle wake; a per-chat digest of its live and
  finished jobs decides which chats changed.
* :class:`UserStepsWatcher` -- user-steps ``actions.sqlite3`` request create/resolve, per-session digest.

Watchers only poll on the store's worker thread (``store.add_tick``); a poll is a few ``data_version`` pragmas.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

MAX_EVENTS_PER_POLL = 5000


def emit_session(session_id: Optional[str], trigger: str = "manual") -> None:
    """Tell the running store (if any) that ``session_id`` changed. Never raises."""
    try:
        from hermes_session_state_store import current

        store = current()
        if store is not None and session_id:
            store.emit_session(str(session_id), trigger)
    except Exception:  # health: allow BLE001 -- shadow-mode hook must never break its caller
        logger.debug("session state emit failed", exc_info=True)


def _fingerprint(path: Path) -> Any:
    from tui_gateway.kanban_replica_observer import fingerprint

    stamp = fingerprint(path)
    return None if stamp is None else (stamp.path, stamp.ident, stamp.generation, stamp.data_version)


def _open_ro(path: Path) -> sqlite3.Connection:
    from hermes_cli.kanban_generation import open_ro

    return open_ro(path, timeout=0.5)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()


class _Watched:
    """Change-gated poll of a set of SQLite files: ``changed(path)`` runs only when its data_version moved."""

    def __init__(self) -> None:
        self._stamps: dict[str, Any] = {}

    def _moved(self, path: Path) -> bool:
        stamp = _fingerprint(path)
        key = str(path)
        if stamp is not None and self._stamps.get(key) == stamp:
            return False
        self._stamps[key] = stamp
        return True


class BoardWatcher(_Watched):
    """See the module doc. ``boards()`` -> ``{slug: path}``, ``replicas()`` -> ``[path]``."""

    def __init__(self, store: Any, *, boards: Optional[Callable[[], dict[str, Path]]] = None,
                 replicas: Optional[Callable[[], list[Path]]] = None,
                 remote_cards: Optional[Callable[[], list[dict]]] = None) -> None:
        super().__init__()
        from hermes_session_state_collect import board_paths

        self.store = store
        self._boards = boards or board_paths
        self._replicas = replicas or _replica_paths
        self._remote_cards = remote_cards or _remote_cards
        self._cursors: dict[str, int] = {}
        self._remote: Optional[dict[str, str]] = None

    def __call__(self) -> None:
        for slug, path in sorted(self._boards().items()):
            if self._moved(path):
                self._drain(path, lambda tid, conn, slug=slug: self._local(slug, tid, conn))
        replica_moved = False
        for path in self._replicas():
            if self._moved(path):
                replica_moved = True
                self._drain(path, lambda tid, conn, slug=path.stem: self.store.emit_card(
                    slug, tid, (), key=None, trigger="replica_sync", by_id=True))
        if replica_moved or self._remote is None:
            self._remote_listing()

    def _local(self, slug: str, task_id: str, conn: sqlite3.Connection) -> None:
        from hermes_cli.kanban_outcome_refresh import linked_chats

        try:
            chats = linked_chats(conn, task_id)
        except sqlite3.Error:
            chats = []
        self.store.emit_card(slug, task_id, chats)

    def _drain(self, path: Path, emit: Callable[[str, sqlite3.Connection], None]) -> None:
        key = str(path)
        try:
            with closing(_open_ro(path)) as conn:
                if key not in self._cursors:  # first sight: start at the head; the first recompute reads it all
                    row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()
                    self._cursors[key] = int(row[0])
                    return
                rows = conn.execute("SELECT id, task_id FROM task_events WHERE id > ? ORDER BY id LIMIT ?",
                                    (self._cursors[key], MAX_EVENTS_PER_POLL)).fetchall()
                if rows:
                    self._cursors[key] = int(rows[-1][0])
                    if len(rows) == MAX_EVENTS_PER_POLL:
                        self._stamps.pop(key, None)  # more to read: poll this file again next tick
                for tid in dict.fromkeys(str(r[1]) for r in rows):
                    emit(tid, conn)
        except sqlite3.Error:
            self._stamps.pop(key, None)
            logger.debug("session state: board %s unreadable", path, exc_info=True)

    def _remote_listing(self) -> None:
        try:
            cards = self._remote_cards()
        except Exception:  # health: allow BLE001 -- an unreadable listing retries on the next replica change
            logger.debug("session state: remote card listing unreadable", exc_info=True)
            return
        by_session: dict[str, list] = {}
        for card in cards:
            sid = card.get("session") or card.get("session_id")
            if sid:
                by_session.setdefault(str(sid), []).append(
                    [card.get("key"), card.get("status"), card.get("stale"), card.get("synced_at"),
                     card.get("block_kind"), card.get("updated_at")])
        digests = {sid: _digest(sorted(rows, key=str)) for sid, rows in by_session.items()}
        previous, self._remote = self._remote, digests
        if previous is None:
            return
        for sid in sorted(set(previous) | set(digests)):
            if previous.get(sid) != digests.get(sid):
                self.store.emit_session(sid, "replica_sync")


def _replica_paths() -> list[Path]:
    from hermes_cli.kanban_board_replica_refs import _replica_paths as paths

    return paths()


def _remote_cards() -> list[dict]:
    from hermes_cli.kanban_session_activity import LISTED_STATUSES, _remote_cards as remote

    return remote(None, LISTED_STATUSES, True)


class StateWatcher(_Watched):
    """See the module doc. ``path`` is the store's own state.db."""

    def __init__(self, store: Any, path: Path) -> None:
        super().__init__()
        self.store, self.path = store, Path(path)
        self._message = None  # last seen messages.id
        self._outcome_at = None  # last seen sessions.outcome_at

    def __call__(self) -> None:
        if not self._moved(self.path):
            return
        try:
            with closing(_open_ro(self.path)) as conn:
                head = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
                outcome = conn.execute("SELECT COALESCE(MAX(outcome_at), 0) FROM sessions").fetchone()[0]
                if self._message is None:
                    self._message, self._outcome_at = int(head), float(outcome or 0)
                    return
                touched = conn.execute(
                    "SELECT DISTINCT m.session_id, s.parent_session_id FROM messages m "
                    "LEFT JOIN sessions s ON s.id = m.session_id WHERE m.id > ? AND m.id <= ?",
                    (self._message, int(head))).fetchall()
                touched += conn.execute("SELECT id, parent_session_id FROM sessions WHERE outcome_at > ?",
                                        (self._outcome_at,)).fetchall()
                self._message, self._outcome_at = int(head), max(self._outcome_at, float(outcome or 0))
        except sqlite3.Error:
            self._stamps.pop(str(self.path), None)
            logger.debug("session state: state.db unreadable", exc_info=True)
            return
        for sid, parent in touched:
            self.store.emit_session(sid, "turn")
            if parent:
                self.store.emit_session(parent, "subagent")


class ProcessWatcher:
    """See the module doc. Subscribes to ``registry``'s lifecycle wake; snapshots on the worker thread."""

    def __init__(self, store: Any, registry: Any = None) -> None:
        from tools.process_registry_events import subscribe

        if registry is None:
            from tools.process_registry import process_registry as registry
        self.store, self.registry = store, registry
        self._dirty = True
        self._digests: Optional[dict[str, str]] = None
        self.close = subscribe(registry, self._wake)

    def _wake(self) -> None:
        self._dirty = True

    def _snapshot(self) -> dict[str, str]:
        rows: dict[str, list] = {}
        with self.registry._lock:
            sessions = [*self.registry._running.values(), *self.registry._finished.values()]
        for s in sessions:
            owner = getattr(s, "parent_session_id", "") or ""
            if owner:
                rows.setdefault(owner, []).append([s.id, bool(s.exited), s.exit_code])
        return {sid: _digest(sorted(items, key=str)) for sid, items in rows.items()}

    def __call__(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        current = self._snapshot()
        previous, self._digests = self._digests, current
        if previous is None:
            return
        for sid in sorted(set(previous) | set(current)):
            if previous.get(sid) != current.get(sid):
                self.store.emit_session(sid, "process")


class UserStepsWatcher(_Watched):
    """See the module doc. ``path`` is the profile's ``user-steps/actions.sqlite3``."""

    def __init__(self, store: Any, path: Path) -> None:
        super().__init__()
        self.store, self.path = store, Path(path)
        self._digests: Optional[dict[str, str]] = None

    def __call__(self) -> None:
        if not self.path.is_file():
            # Absent means known-empty: when the store first appears, every request in it is new and is emitted
            # (a None baseline would swallow the first requests until an unrelated event or reconciliation).
            self._digests = {}
            return
        if not self._moved(self.path):
            return
        try:
            with closing(_open_ro(self.path)) as conn:
                rows = conn.execute("SELECT session, namespace, id, revision, state, resolution FROM requests"
                                    ).fetchall()
        except sqlite3.Error:
            self._stamps.pop(str(self.path), None)
            logger.debug("session state: user-steps store unreadable", exc_info=True)
            return
        grouped: dict[str, list] = {}
        for session, *rest in rows:
            if session:
                grouped.setdefault(str(session), []).append([str(v) for v in rest])
        current = {sid: _digest(sorted(items)) for sid, items in grouped.items()}
        previous, self._digests = self._digests, current
        if previous is None:
            return
        for sid in sorted(set(previous) | set(current)):
            if previous.get(sid) != current.get(sid):
                self.store.emit_session(sid, "user_steps")


def install_watchers(store: Any, *, state_path: Path, user_steps_path: Optional[Path],
                     registry: Any = None) -> list[Any]:
    watchers: list[Any] = [BoardWatcher(store), StateWatcher(store, state_path)]
    try:
        watchers.append(ProcessWatcher(store, registry))
    except ImportError:
        logger.debug("session state: process registry unavailable", exc_info=True)
    if user_steps_path is not None:
        watchers.append(UserStepsWatcher(store, user_steps_path))
    for watcher in watchers:
        store.add_tick(watcher)
    return watchers
