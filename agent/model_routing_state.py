"""Process-safe provider allocation and session lineage (no credentials or prompts)."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path


class RoutingState:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def allocate(self, session_id, parent_session_id, level, candidates, version, excluded, prepare=None,
                 exact_pin=False, replace=False):
        """Allocate and persist atomically; aliases share one failover decision."""
        with closing(sqlite3.connect(self.path, timeout=30, isolation_level=None)) as db:
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("CREATE TABLE IF NOT EXISTS aliases (session TEXT PRIMARY KEY, root TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS routes (root TEXT, class TEXT, metadata TEXT NOT NULL, "
                       "PRIMARY KEY(root, class))")
            db.execute("CREATE TABLE IF NOT EXISTS counters (pool TEXT PRIMARY KEY, position INTEGER NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            try:
                result = self._allocate(db, session_id, parent_session_id, level, candidates, version, excluded,
                                        exact_pin, replace)
                # Credentials/client must be usable before another process can
                # observe the replacement. Failure rolls back counters as well.
                if prepare is not None:
                    prepare(result)
                db.execute("COMMIT")
                return result
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def peek(self, session, level):
        """Read the latest lineage selection without creating state or advancing it."""
        if not self.path.exists():
            return None
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as db:
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

        with closing(sqlite3.connect(self.path, timeout=30, isolation_level=None)) as db:
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

    def _allocate(self, db, session, parent, level, candidates, version, excluded, exact_pin=False, replace=False):
        from agent.model_routing import ModelRoutingError

        root = self._root(db, session, parent)
        row = db.execute("SELECT metadata FROM routes WHERE root=? AND class=?", (root, level)).fetchone()
        if row and not replace:
            previous = json.loads(row[0])
            identity = previous["selected_provider"], previous["selected_model"]
            allowed = any((r["provider"], r["model"]) == identity and
                          r["billing"] == previous["billing_group"] for r in candidates)
            if allowed and identity not in excluded:
                if exact_pin and not previous.get("exact_pin"):
                    previous["exact_pin"] = True
                    db.execute("UPDATE routes SET metadata=? WHERE root=? AND class=?",
                               (json.dumps(previous), root, level))
                return previous
            # A sticky class selection is a preference, not an exact-model pin.
            # Re-select only from the current class's filtered candidates; prepare
            # still runs before commit, so failed credentials cannot publish it.
            if previous.get("exact_pin") or exact_pin:
                raise ModelRoutingError("Exact pinned route cannot fail over")
        if not candidates:
            raise ModelRoutingError(f"No eligible {level} routes under billing/capability policy")
        route = self._choose(db, level, candidates, version, kind=_allocation_kind(root))
        metadata = {"requested_class": level, "policy_version": version,
                    "selected_provider": route["provider"], "selected_model": route["model"],
                    "billing_group": route["billing"], "reason": "same-class-failover" if row else "allocation"}
        if exact_pin:
            metadata["exact_pin"] = True
        if replace:
            metadata["reason"] = "explicit-switch"
        db.execute("INSERT OR REPLACE INTO routes VALUES (?, ?, ?)", (root, level, json.dumps(metadata)))
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


def _allocation_kind(root):
    """Auxiliary owners are allocated under ``aux:<owner>:<task>:<class>`` roots."""
    return "aux" if str(root).startswith("aux:") else "main"
