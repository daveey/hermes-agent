"""Process-safe provider allocation and session lineage (no credentials or prompts)."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import closing
from pathlib import Path

logger = logging.getLogger(__name__)

# SQLite's busy handler is the only retry: it re-attempts a lock until this deadline, before any
# allocation work or ``prepare`` runs. A whole-transaction retry after ``prepare`` would repeat its
# credential-pool side effects (account selection/refresh), so none is attempted.
BUSY_TIMEOUT_SECONDS = 30.0
# Converting a rollback-journal file to WAL needs a moment with no reader. A long reader must not
# stall allocation for the full busy deadline here; the next connection simply tries again.
WAL_CONVERT_TIMEOUT_SECONDS = 1.0
# Allocation holds the writer lock across ``prepare`` (credential/client readiness). Log when that
# window is long enough to queue other first allocations behind it.
SLOW_WRITE_WARNING_SECONDS = 5.0


class RoutingState:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self):
        """Writer connection in WAL mode, so readers never block a commit (nor a commit, readers).

        In the default rollback journal a COMMIT needs every reader gone: one long read transaction
        (a maintenance scan) made each allocator's COMMIT time out, and the PENDING lock it left
        while waiting refused every new reader, schema check and peek host-wide.
        """
        db = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
        try:
            if str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
                db.execute(f"PRAGMA busy_timeout={int(WAL_CONVERT_TIMEOUT_SECONDS * 1000)}")
                try:
                    db.execute("PRAGMA journal_mode=WAL")
                except sqlite3.OperationalError as exc:
                    if exc.sqlite_errorcode & 0xFF not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                        raise
                    # A reader holds the rollback-journal file: stay in it for this connection.
                finally:
                    db.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_SECONDS * 1000)}")
        except BaseException:
            db.close()
            raise
        return db

    @staticmethod
    def _ensure_schema(db):
        """Autocommit, before the allocation transaction: tables outlive a rolled-back allocation, so a
        later peek or ``link_continuation`` reads an empty table rather than a missing one. On an
        existing schema each statement only reads it (no write lock in WAL)."""
        db.execute("CREATE TABLE IF NOT EXISTS aliases (session TEXT PRIMARY KEY, root TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS routes (root TEXT, class TEXT, metadata TEXT NOT NULL, "
                   "PRIMARY KEY(root, class))")
        db.execute("CREATE TABLE IF NOT EXISTS counters (pool TEXT PRIMARY KEY, position INTEGER NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS switch_fences (root TEXT, class TEXT, intent TEXT NOT NULL, "
                   "PRIMARY KEY(root, class))")

    def allocate(self, session_id, parent_session_id, level, candidates, version, excluded, prepare=None,
                 exact_pin=False, replace: bool | str = False, pin_source=None, walled_from=None):
        """Allocate and persist atomically; aliases share one failover decision.

        ``replace`` is ``True`` (unconditional, an in-session switch) or a committed switch-intent fence
        token: that intent replaces this lineage's class allocation at most once, and only while it is
        newer than the intent the allocation last honoured (``model_routing_switch_intent.supersedes``).
        The fence is recorded in this transaction, so overlapping builds, a crash retry before the intent
        is consumed and a late build of a superseded intent re-read the honoured allocation instead of
        choosing again; a failed ``prepare`` rolls it back.

        A sticky reuse that writes nothing (the per-turn/re-entry case, never any truthy ``replace``)
        is read from a snapshot and prepared outside the writer lock: no allocation, counter or route
        changes, so there is nothing to publish atomically. Every write (first allocation, alias,
        failover, pin, replacement) still runs ``prepare`` before COMMIT, so a failed credential/client
        never becomes visible.
        """
        if not replace and walled_from is None:
            previous = self._sticky(session_id, level, candidates, excluded)
            if previous is not None:
                if prepare is not None:
                    prepare(previous)
                return previous
        with closing(self._connect()) as db:
            self._ensure_schema(db)
            db.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            try:
                result = self._allocate(db, session_id, parent_session_id, level, candidates, version, excluded,
                                        exact_pin, replace, pin_source=pin_source, walled_from=walled_from)
                # Credentials/client must be usable before another process can
                # observe the replacement. Failure rolls back counters as well.
                if prepare is not None:
                    prepare(result)
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
            held = time.monotonic() - started
            if held >= SLOW_WRITE_WARNING_SECONDS:
                logger.warning("model routing allocation held the routing writer lock %.1fs (class %s)", held, level)
            return result

    def _sticky(self, session, level, candidates, excluded):
        """The existing allocation when reusing it needs no write, else None (take the write path)."""
        if not self.path.exists():
            return None
        with closing(self._connect()) as db:
            try:
                row = db.execute(
                    "SELECT r.metadata FROM aliases a JOIN routes r ON r.root=a.root "
                    "WHERE a.session=? AND r.class=?", (session, level)).fetchone()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    return None
                raise
        if row is None:
            return None
        previous = json.loads(row[0])
        if not _reusable(previous, candidates, excluded):
            return None
        return previous

    def peek(self, session, level):
        """Read the latest lineage selection without creating state or advancing it."""
        if not self.path.exists():
            return None
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True,
                                     timeout=BUSY_TIMEOUT_SECONDS)) as db:
            row = db.execute(
                "SELECT r.metadata FROM routes r JOIN aliases a ON a.root=r.root "
                "WHERE a.session=? AND r.class=?", (session, level)).fetchone()
            return json.loads(row[0]) if row else None

    def link_continuation(self, session, parent):
        """Bind a proven compression child without consuming an allocation slot.

        Call before publishing the child in SessionDB: a failed publish may leave
        an unreachable alias, but a visible child must never lack its route.
        Existing independent allocations cannot be merged retroactively.
        """
        from agent.model_routing import ModelRoutingError

        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT root FROM aliases WHERE session=?", (parent,)).fetchone()
                if row is None:
                    raise ModelRoutingError("Compression routing lineage has no allocated parent")
                existing = db.execute("SELECT root FROM aliases WHERE session=?", (session,)).fetchone()
                if existing is not None and existing[0] != row[0]:
                    raise ModelRoutingError("Compression routing lineage conflicts with an independent session")
                db.execute("INSERT OR IGNORE INTO aliases VALUES (?, ?)", (session, row[0]))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def _allocate(self, db, session, parent, level, candidates, version, excluded, exact_pin=False,
                  replace: bool | str = False, pin_source=None, walled_from=None):
        from agent.model_routing import ModelRoutingError

        root = self._root(db, session, parent)
        fence = replace if isinstance(replace, str) and replace else None
        if fence is not None:
            honoured = db.execute("SELECT intent FROM switch_fences WHERE root=? AND class=?",
                                  (root, level)).fetchone()
            from agent.model_routing_switch_intent import supersedes
            if not supersedes(fence, honoured[0] if honoured is not None else None):
                # This event (a retry, an overlapping build) or a newer one already replaced the
                # allocation: ordinary sticky lookup, so a superseded build cannot move it back.
                replace = False
        row = db.execute("SELECT metadata FROM routes WHERE root=? AND class=?", (root, level)).fetchone()
        if row and not replace:
            previous = json.loads(row[0])
            if walled_from is not None:
                # [daveey] A quota-walled router/default pin moves within its class; David's own
                # pin never does (``is_explicit_pin``).
                if is_explicit_pin(previous):
                    raise ModelRoutingError("Exact pinned route cannot fail over")
            elif _reusable(previous, candidates, excluded):
                # [daveey] An echo of this session's own selection (the gateway resolving the concrete
                # model the router picked) is not David naming it: it never upgrades the allocation to
                # an exact pin, so a quota wall can still move it within its class.
                return previous
            # A sticky class selection is a preference, not an exact-model pin.
            # Re-select only from the current class's filtered candidates; prepare
            # still runs before commit, so failed credentials cannot publish it.
            elif previous.get("exact_pin") or exact_pin:
                raise ModelRoutingError("Exact pinned route cannot fail over")
        if not candidates:
            raise ModelRoutingError(f"No eligible {level} routes under billing/capability policy")
        route = self._choose(db, level, candidates, version, kind=_allocation_kind(root))
        metadata = {"requested_class": level, "policy_version": version,
                    "selected_provider": route["provider"], "selected_model": route["model"],
                    "billing_group": route["billing"], "reason": "same-class-failover" if row else "allocation"}
        if exact_pin:
            metadata["exact_pin"] = True
        metadata["pin_source"] = "explicit" if replace or (exact_pin and pin_source == "explicit") else "default"
        if replace:
            metadata["reason"] = "explicit-switch"
        if walled_from is not None:
            metadata.pop("exact_pin", None)
            metadata.update(reason="same-class-failover", walled_from=dict(walled_from))
        db.execute("INSERT OR REPLACE INTO routes VALUES (?, ?, ?)", (root, level, json.dumps(metadata)))
        if replace and fence is not None:
            db.execute("INSERT OR REPLACE INTO switch_fences VALUES (?, ?, ?)", (root, level, fence))
        return metadata

    @staticmethod
    def _root(db, session, parent):
        row = db.execute("SELECT root FROM aliases WHERE session=?", (session,)).fetchone()
        if row:
            return row[0]
        root = session
        if parent:
            row = db.execute("SELECT root FROM aliases WHERE session=?", (parent,)).fetchone()
            root = row[0] if row else parent
            db.execute("INSERT OR IGNORE INTO aliases VALUES (?, ?)", (parent, root))
        db.execute("INSERT INTO aliases VALUES (?, ?)", (session, root))
        return root

    @staticmethod
    def _choose(db, level, candidates, version, kind="main"):
        # Provider weights, not route/account counts, determine traffic share.
        subscriptions = [r for r in candidates if r["billing"] == "subscription"]
        candidates = subscriptions or candidates
        groups = {}
        for route in candidates:
            groups.setdefault(route["provider"], []).append(route)
        cycle = [p for p in sorted(groups) for _ in range(groups[p][0]["weight"])]
        key = [version, level, [(p, groups[p][0]["weight"]) for p in sorted(groups)]]
        # Main sessions and auxiliary owners keep separate cycles. A shared counter interleaved
        # one main + one aux allocation per session, so with two providers every main session
        # landed on the same provider (and every aux call on the other). The main key is
        # unchanged so existing counters keep their position.
        pool = json.dumps(key if kind == "main" else key + [kind])
        row = db.execute("SELECT position FROM counters WHERE pool=?", (pool,)).fetchone()
        position = row[0] if row else 0
        provider = cycle[position % len(cycle)]
        db.execute("INSERT OR REPLACE INTO counters VALUES (?, ?)", (pool, position + 1))
        return sorted(groups[provider], key=lambda r: r["model"])[0]


def is_explicit_pin(metadata):
    """True for a route David chose (an honoured switch, a model he named); a router pick, the config
    default or an echo of either is ``default``. Rows written before ``pin_source`` count as explicit
    only when they record an honoured switch: their ``exact_pin`` was also set by echoes. [daveey]"""
    if not metadata:
        return False
    source = metadata.get("pin_source")
    if source in ("explicit", "default"):
        return source == "explicit"
    return metadata.get("reason") == "explicit-switch"


def _reusable(previous, candidates, excluded):
    """One sticky-reuse rule for the snapshot read and the write transaction."""
    identity = previous["selected_provider"], previous["selected_model"]
    allowed = any((r["provider"], r["model"]) == identity and
                  r["billing"] == previous["billing_group"] for r in candidates)
    return allowed and identity not in excluded


def _allocation_kind(root):
    """Auxiliary owners are allocated under ``aux:<owner>:<task>:<class>`` roots."""
    return "aux" if str(root).startswith("aux:") else "main"
