"""Keyed remote notice discovery: read only the replica rows a fact wake named. [daveey]

``kanban_remote_notices.collect`` normally builds (or looks up) the whole remote index: every task row of the hub
replica, routed to chats, plus ``observe`` over every terminal state. On a keyed change-feed wake that read is
prohibited; the wake names the ``(owner gateway, key)`` replica rows that moved, and this module answers the same
``(me, {sid: [card, ...]}, observed)`` triple for those rows only:

* rows come from short-lived read-only connections in ONE read snapshot, ``gw = ? AND key IN (...)`` batches of at
  most :data:`KEY_BATCH` keys; the woken keys are coalesced first (each key read once however often it woke);
* routing is the canonical projection's (``remote_index_projection.snapshot``): another gateway's row, a notifiable
  or withdrawn task, its ``_linked_chats``. Unqualified legacy keys wait for reconciliation: checking their
  supersession with a suffix query would scan unrelated rows on every wake;
* first observations follow ``kanban_remote_observations._persist`` for a source whose baseline exists (insert-only
  rows, shared across owners of one key, legacy keys and unknown owner times never seeded). Only a source whose
  baseline is ALREADY recorded is written, checked inside the ledger write transaction: a keyed read must never
  establish the baseline, or every state it did not read would get a fresh local window on the next full read.
  Without a baseline (or before the replica is caught up) nothing is written and the answer is ``complete=False``:
  owner-fresh cards are still claimed (as ``collect`` does on an unready replica), the rest wait for the full read.

Nothing here claims or delivers; ``collect`` runs its own claim loop over the cards (durable claim/ACK unchanged).
``WakeBatch(..., keys=None)`` is reserved for the process reconciler: it enumerates
canonical identities in the same read snapshot, then uses the same bounded IN
reads and records the complete baseline once for all loaded chats in a profile.
"""
from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from typing import Iterable

KEY_BATCH = 500


def coalesce(keys: Iterable) -> list[tuple[str, str]]:
    """``[(owner, key)]`` once each, sorted. Accepts ``(owner, key)`` pairs or ``"<owner>/<key>"`` refs."""
    out = set()
    for item in keys:
        if isinstance(item, str):
            owner, _, key = item.partition("/")
        else:
            owner, key = item
        if owner and key:
            out.add((str(owner), str(key)))
    return sorted(out)


def read_rows(path: Path, keys: list[tuple[str, str]] | None) -> tuple[str, bool, list[tuple[str, str, dict]]]:
    """``(me, ready, [(owner, key, task)])`` for the woken rows routed to some chat or notifiable, one snapshot."""
    from tui_gateway.kanban_remote_notices import _ro

    with contextlib.closing(_ro(path)) as conn:
        conn.execute("BEGIN")
        try:
            meta = dict(conn.execute("SELECT k, v FROM meta WHERE k IN ('gateway', 'cursor', 'head')"))
            me = str(meta.get("gateway") or "")
            ready = False
            with contextlib.suppress(TypeError, ValueError):
                ready = bool(me) and int(str(meta.get("cursor"))) >= int(str(meta.get("head")))
            if not me:
                return "", False, []
            reconcile = keys is None
            if reconcile:
                from tui_gateway.kanban_remote_notices import _canonical
                keys = _canonical(conn.execute(
                    "SELECT gw, key FROM replica WHERE coll = 'tasks' AND gw != ?", (me,)).fetchall())
            by_owner: dict[str, list[str]] = {}
            for owner, key in keys:
                if owner != me:
                    by_owner.setdefault(owner, []).append(key)
            rows = []
            for owner, owned in by_owner.items():
                for at in range(0, len(owned), KEY_BATCH):
                    batch = owned[at:at + KEY_BATCH]
                    marks = ",".join("?" * len(batch))
                    rows += conn.execute(f"SELECT gw, key, data FROM replica WHERE coll = 'tasks' AND gw = ? "
                                         f"AND key IN ({marks})", (owner, *batch)).fetchall()
            rows.sort(key=lambda row: (str(row[0]), str(row[1])))
            out = []
            for owner, key, data in rows:
                if str(key).count('/') < 2 and not reconcile:
                    # Alias supersession requires a scope scan. Leave legacy
                    # identities to the shared reconciliation pass instead.
                    ready = False
                    continue
                try:
                    task = json.loads(data)
                except (TypeError, ValueError):
                    continue
                if isinstance(task, dict):
                    out.append((str(owner), str(key), task))
            return me, ready, out
        finally:
            conn.rollback()


def route(rows: list[tuple[str, str, dict]], me: str, profile: str, sids: tuple) -> dict[str, list]:
    """``{sid: [card, ...]}`` of ``rows`` for this chat, by the canonical projection's rule."""
    from tui_gateway.kanban_remote_notices import _linked_chats, _notifiable, _withdrawal

    by_sid: dict[str, list] = {sid: [] for sid in sids}
    for owner, key, task in rows:
        if not task.get("id") or not (_notifiable(task) or _withdrawal(task) is not None):
            continue
        for chat_profile, sid in _linked_chats(task, me):
            if chat_profile == profile and sid in by_sid:
                by_sid[sid].append({"gw": owner, "key": key, "task": task})
    return by_sid


def observe_keyed(replica: Path, ledger, now: float, rows: list[tuple[str, str, dict]]):
    """``Observed`` for ``rows``' notifiable states, or None when the source has no baseline (nothing written)."""
    from tui_gateway.kanban_remote_notice_state import state as notice_state
    from tui_gateway.kanban_remote_notices import _fresh_at, _ledger, _notifiable, _withdrawal, ledger_path
    from tui_gateway.kanban_remote_observations import Observed, _fresh_map

    cards = [(owner, key, task) for owner, key, task in rows if _notifiable(task) or _withdrawal(task) is not None]
    source = str(Path(replica).resolve())
    ledger = Path(ledger) if ledger is not None else ledger_path()
    states = [(owner, key, notice_state(task), f"{owner}/{key}", _fresh_at(task)) for owner, key, task in cards]
    with contextlib.closing(_ledger(ledger)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            has_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='observation_sources'"
                                     ).fetchone() is not None
            if (not has_table or conn.execute("SELECT 1 FROM observation_sources WHERE source=?",
                                              (source,)).fetchone() is None
                    or conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' "
                                    "AND name='observations_card_state'").fetchone() is None):
                conn.rollback()
                return None
            if not cards:
                conn.rollback()
                return Observed()
            conn.execute("CREATE TABLE IF NOT EXISTS observations (source TEXT, ref TEXT, state TEXT, "
                         "first_seen REAL, PRIMARY KEY(source, ref, state))")
            seen_map = {}
            for owner, key, state, ref, at in states:
                if (ref, state) in seen_map:
                    continue
                # A recorded baseline: never the initial snapshot (``_persist`` with ``initial`` False).
                seen = now if at is not None and key.count("/") >= 2 else None
                found = conn.execute("SELECT first_seen, ref FROM observations WHERE source=? AND state=? "
                                     "AND substr(ref, instr(ref, '/') + 1)=?", (source, state, key)).fetchall()
                if found:
                    seen = None if any(row[0] is None for row in found) else min(row[0] for row in found)
                if not any(row[1] == ref for row in found):
                    conn.execute("INSERT INTO observations VALUES (?,?,?,?)", (source, ref, state, seen))
                seen_map[(ref, state)] = seen
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return _fresh_map(states, seen_map)


class WakeBatch:
    """One owner's immutable discovery result reused by every chat in a wake batch.

    Construct outside per-chat executors. The owner pins replica, delivery ledger,
    profile and capture time; consumers cannot reuse it under another scope.
    A new batch (including reconciliation) must create fresh evidence.
    """
    def __init__(self, path, ledger, now, profile, keys):
        from tui_gateway.kanban_remote_notices import ledger_path
        self.path = Path(path).resolve()
        self.ledger = Path(ledger if ledger is not None else ledger_path()).resolve()
        self.profile, self.now = profile, now
        self.expires = time.monotonic() + 30
        self.reconcile = keys is None
        self.me, ready, rows = read_rows(self.path, None if self.reconcile else coalesce(keys))
        if ready and self.reconcile:
            from tui_gateway.kanban_remote_observations import _persist
            from tui_gateway.kanban_remote_notices import _notifiable, _withdrawal
            cards = [(owner, key, task) for owner, key, task in rows
                     if _notifiable(task) or _withdrawal(task) is not None]
            observed = _persist(str(self.path), cards, self.ledger, now)
        else:
            observed = observe_keyed(self.path, self.ledger, now, rows) if ready else None
        self.rows = json.dumps(rows)
        self.observed = observed

    def discover(self, path, ledger, now, profile, sids):
        from tui_gateway.kanban_remote_notices import ledger_path
        from tui_gateway.kanban_remote_observations import Observed
        target = Path(ledger if ledger is not None else ledger_path()).resolve()
        if time.monotonic() >= self.expires:
            raise ValueError('wake batch expired')
        if (Path(path).resolve() != self.path or target != self.ledger
                or profile != self.profile or now != self.now):
            raise ValueError('wake batch scope or capture time mismatch')
        return (self.me, route(json.loads(self.rows), self.me, profile, sids),
                self.observed if self.observed is not None else Observed(), self.observed is not None)


def discover(path: Path, ledger, now: float, profile: str, sids: tuple, keys: Iterable | WakeBatch
             ) -> tuple[str, dict[str, list], dict, bool]:
    """``(me, {sid: [card, ...]}, observed, complete)`` for the woken ``keys`` only.

    ``complete`` is False when first observations could not be recorded (replica not caught up, or no baseline
    yet): the caller must keep a retry obligation so a full read settles what this one could not."""
    from tui_gateway.kanban_remote_observations import Observed

    if isinstance(keys, WakeBatch):
        return keys.discover(path, ledger, now, profile, sids)
    me, ready, rows = read_rows(path, coalesce(keys))
    if not me:
        return "", {}, Observed(), False
    by_sid = route(rows, me, profile, sids)
    observed = observe_keyed(path, ledger, now, rows) if ready else None
    if observed is None:
        return me, by_sid, Observed(), False
    return me, by_sid, observed, True
