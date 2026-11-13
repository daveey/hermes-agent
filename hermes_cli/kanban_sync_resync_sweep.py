"""Rebuild a primary board's LIVE file from a snapshot after a 410, then sweep this gateway's rows it lacks.

A snapshot carries only live rows and :func:`kanban_sync_client.resync` only upserts, so a row of this gateway's
cards that the Board deleted below its retention floor would stay in the live file forever. The replica is rebuilt
into a fresh file (``_pulled_replica`` unlinks it); the live file cannot be: it holds unpushed local cards (outbox),
local tombstones and picked-up copies of other gateways' cards. So the sweep deletes only a row that is

- in this gateway's partition: a card ``created_on`` this gateway (or unset, a local card the Board never
  stamped here), a link whose child is such a card, a child row of such a card;
- absent from a snapshot table read to its last page;
- not held locally: no outbox op for the row (pending or refused) or, for a child row, for its card, and no local
  tombstone;
- not written after the rebuild began: its creation time, in whole seconds, is before ``since`` (a row of that
  same second is spared for one more rebuild rather than guessed older); ``task_links`` has no creation time, so a
  link is swept only if it was in the file when the rebuild began.

The deletes go through the feed apply (the same handlers and tombstones as a feed delete, observers fired), children
before cards, and each candidate is re-checked inside that apply transaction.

The round's ``board_sync_lock`` does not stop every push: ``reconcile`` (``hermes kanban sync reconcile``,
``kanban_db_recover``) pushes without it. Its push can ack an op after that table's snapshot pages were read: the
row is then missing from the snapshot although the Board holds it, and its op is gone before the sweep. So
:func:`resync_live` reads every outbox op (any state) before the rebuild starts, and the sweep also spares those
rows and every row of the cards they name. A link written after that read and acked by such a push before the
sweep has neither an op nor a creation time, so the links present at that read bound the ``task_links`` sweep.
[daveey] t_01a1d5f9de4020fa t_01a1d62f0a4187d8 t_01a1d6660108bedb
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from hermes_cli import kanban_db_sync as sync

_log = logging.getLogger(__name__)

_OWN = "(t.created_on IS NULL OR t.created_on = :gw)"
_CARD_FREE = "NOT EXISTS (SELECT 1 FROM kanban_sync_outbox o WHERE o.table_name = 'tasks' AND o.row_uid = t.id)"
# Child rows of this gateway's cards: (uid expression, creation-time column or None).
_CHILDREN: dict[str, tuple[str, Optional[str]]] = {
    "task_links": ("x.parent_id || '>' || x.child_id", None),
    "task_runs": ("x.uid", "started_at"),
    "task_comments": ("x.uid", "created_at"),
    "task_events": ("x.uid", "created_at"),
    "task_attachments": ("x.uid", "created_at"),
    "task_chat_links": ("x.uid", "created_at"),
}


def _row_free(table: str, uid: str) -> str:
    return (f"NOT EXISTS (SELECT 1 FROM kanban_sync_outbox o WHERE o.table_name = '{table}' AND o.row_uid = {uid}) "
            f"AND NOT EXISTS (SELECT 1 FROM kanban_sync_tombstones d WHERE d.table_name = '{table}' "
            f"AND d.row_uid = {uid})")


def _candidates_sql(table: str) -> str:
    """``(uid, card id)`` of the rows of ``table`` the sweep may delete (before the snapshot comparison); ``:uid``
    NULL selects all."""
    if table == "tasks":
        return (f"SELECT t.id, t.id FROM tasks t WHERE {_OWN} AND COALESCE(t.created_at, 0) < :since "
                f"AND {_row_free('tasks', 't.id')} AND (:uid IS NULL OR t.id = :uid)")
    uid, born = _CHILDREN[table]
    task = "x.child_id" if table == "task_links" else "x.task_id"
    where = [_OWN, _CARD_FREE, _row_free(table, uid), f"(:uid IS NULL OR {uid} = :uid)"]
    if born:
        where.append(f"COALESCE(x.{born}, 0) < :since")
    if table == "task_events":  # the Board drops older events without a delete: their absence is no deletion
        where.append("COALESCE(x.created_at, 0) >= :window")
    if table != "task_links":
        where.append("x.uid IS NOT NULL")
    return f"SELECT {uid}, t.id FROM {table} x JOIN tasks t ON t.id = {task} WHERE " + " AND ".join(where)


def _params(gateway: str, since: int, uid: Optional[str] = None) -> dict[str, Any]:
    from hermes_cli.kanban_sync_client import EVENT_IMPORT_WINDOW_S
    return {"gw": gateway, "since": since, "uid": uid, "window": int(time.time()) - EVENT_IMPORT_WINDOW_S}


@dataclass(frozen=True)
class Held:
    """Outbox ops present when the rebuild began: their rows, and the cards those rows belong to; and the
    ``task_links`` uids then in the file (``None``: not recorded, so no bound)."""

    rows: frozenset[tuple[str, str]] = frozenset()
    cards: frozenset[str] = frozenset()
    links: Optional[frozenset[str]] = None

    def spares(self, table: str, uid: str, card: str) -> bool:
        if table == "task_links" and self.links is not None and uid not in self.links:
            return True  # no creation time: a link that was not in the file when the rebuild began may be new
        return (table, uid) in self.rows or (table != "tasks" and card in self.cards)


def held_at_start(conn: sqlite3.Connection) -> Held:
    """Every outbox op (any state) and the card each one names, and every ``task_links`` uid, read before the
    snapshot: a push outside the round's lock (``reconcile``) may ack an op while the snapshot is read, after its
    row's table was already paged. The links are read first: a link written between the two reads is then spared
    as new rather than bounded in without its op."""
    links = frozenset(str(uid) for (uid,) in conn.execute(f"SELECT {_CHILDREN['task_links'][0]} FROM task_links x"))
    rows: set[tuple[str, str]] = set()
    cards: set[str] = set()
    for table, uid in conn.execute("SELECT table_name, row_uid FROM kanban_sync_outbox"):
        table, uid = str(table), str(uid)
        rows.add((table, uid))
        if table == "tasks":
            cards.add(uid)
        elif table == "task_links":
            cards.add(uid.partition(">")[2])
        elif table in _CHILDREN:
            found = conn.execute(f"SELECT task_id FROM {table} WHERE uid = ?", (uid,)).fetchone()
            if found and found[0]:
                cards.add(str(found[0]))
    return Held(frozenset(rows), frozenset(cards), links)


def sweep_absent(conn: sqlite3.Connection, seen: Mapping[str, set[str]], board: str, gateway: str, *,
                 since: int, observers: bool = True, held: Held = Held()) -> dict[str, int]:
    """Delete this gateway's rows that the snapshot did not return (``seen``: uids per table read to its last page;
    a table not in it is not swept), sparing what ``held`` names. Returns the count deleted per table."""
    from hermes_cli.kanban_sync_client import IMPORT_ORDER
    deletes: list[sync.RemoteChange] = []
    for table in reversed(IMPORT_ORDER):  # children before cards: a card's delete cascades its own rows
        if table not in seen:
            continue
        rows = conn.execute(_candidates_sql(table), _params(gateway, since)).fetchall()
        uids = sorted(str(uid) for uid, card in rows
                      if str(uid) not in seen[table] and not held.spares(table, str(uid), str(card)))
        deletes += [sync.RemoteChange(seq=0, table=table, uid=uid, op="delete", fields={}, version=0, hlc="",
                                      origin_gateway="", client_op_id=None)
                    for uid in uids]
    if not deletes:
        return {}

    swept: dict[str, int] = {}

    def still_sweepable(c: sqlite3.Connection, ch: sync.RemoteChange) -> bool:  # inside the apply transaction
        row = c.execute(_candidates_sql(ch.table), _params(gateway, since, ch.uid)).fetchone()
        if row is None or held.spares(ch.table, ch.uid, str(row[1])):
            return False
        swept[ch.table] = swept.get(ch.table, 0) + 1
        return True

    cursor = int((sync.sync_state(conn) or {}).get("cursor") or 0)
    sync.apply_remote_changes(conn, deletes, new_cursor=cursor, board=board, observers=observers,
                              keep=still_sweepable)
    return swept


class _SeenHub:
    """Hub proxy that records the uids each snapshot table returned, and which tables were read to the end."""

    def __init__(self, hub: Any) -> None:
        self._hub = hub
        self.seen: dict[str, set[str]] = {}
        self.complete: set[str] = set()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._hub, name)

    def snapshot_page(self, board: str, table: str, after: str = "") -> dict:
        page = self._hub.snapshot_page(board, table, after)
        self.seen.setdefault(table, set()).update(str(r["uid"]) for r in page.get("rows", []))
        if not page.get("more"):
            self.complete.add(table)
        return page


def resync_live(conn: sqlite3.Connection, hub: Any, board: str, gateway: str) -> dict[str, int]:
    """``resync`` a primary board's live file (``partition=gateway``), then :func:`sweep_absent`."""
    from hermes_cli.kanban_sync_client import resync
    _log.warning("kanban sync: %s live cursor below the retention floor; rebuilding from a snapshot", board)
    since = int(time.time())
    held = held_at_start(conn)
    seeing = _SeenHub(hub)
    resync(conn, seeing, board, observers=True, partition=gateway)
    swept = sweep_absent(conn, {t: seeing.seen.get(t, set()) for t in seeing.complete}, board, gateway, since=since,
                         held=held)
    if swept:
        _log.warning("kanban sync: %s live rebuild removed rows the Board deleted: %s", board, swept)
    return swept
