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

    def allocate(self, session_id, parent_session_id, level, candidates, version, excluded):
        """Allocate and persist atomically; aliases share one failover decision."""
        with closing(sqlite3.connect(self.path, timeout=30, isolation_level=None)) as db:
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("CREATE TABLE IF NOT EXISTS aliases (session TEXT PRIMARY KEY, root TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS routes (root TEXT, class TEXT, metadata TEXT NOT NULL, "
                       "PRIMARY KEY(root, class))")
            db.execute("CREATE TABLE IF NOT EXISTS counters (pool TEXT PRIMARY KEY, position INTEGER NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            try:
                result = self._allocate(db, session_id, parent_session_id, level, candidates, version, excluded)
                db.execute("COMMIT")
                return result
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def _allocate(self, db, session, parent, level, candidates, version, excluded):
        from agent.model_routing import ModelRoutingError

        root = self._root(db, session, parent)
        row = db.execute("SELECT metadata FROM routes WHERE root=? AND class=?", (root, level)).fetchone()
        if row:
            previous = json.loads(row[0])
            identity = previous["selected_provider"], previous["selected_model"]
            allowed = any((r["provider"], r["model"]) == identity and
                          r["billing"] == previous["billing_group"] for r in candidates)
            if identity not in excluded:
                if not allowed:
                    raise ModelRoutingError("Sticky route is no longer eligible; explicit failure selection required")
                return previous
        if not candidates:
            raise ModelRoutingError(f"No eligible {level} routes under billing/capability policy")
        route = self._choose(db, level, candidates, version)
        metadata = {"requested_class": level, "policy_version": version,
                    "selected_provider": route["provider"], "selected_model": route["model"],
                    "billing_group": route["billing"], "reason": "same-class-failover" if row else "allocation"}
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
    def _choose(db, level, candidates, version):
        # Provider weights, not route/account counts, determine traffic share.
        subscriptions = [r for r in candidates if r["billing"] == "subscription"]
        candidates = subscriptions or candidates
        groups = {}
        for route in candidates:
            groups.setdefault(route["provider"], []).append(route)
        cycle = [p for p in sorted(groups) for _ in range(groups[p][0]["weight"])]
        pool = json.dumps([version, level, [(p, groups[p][0]["weight"]) for p in sorted(groups)]])
        row = db.execute("SELECT position FROM counters WHERE pool=?", (pool,)).fetchone()
        position = row[0] if row else 0
        provider = cycle[position % len(cycle)]
        db.execute("INSERT OR REPLACE INTO counters VALUES (?, ?)", (pool, position + 1))
        return sorted(groups[provider], key=lambda r: r["model"])[0]
