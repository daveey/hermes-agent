"""Carry a courier origin intent through the Board so a restored owner can rebuild it. [daveey]

``origin_creation_intents`` (``kanban_origin_creation``) is machine-local and does not sync, and its
``binding_json.binding_id`` is a random secret, so nothing on the Board could rebuild it. The hermes-hub collector
publishes a card's ``origin_bindings`` only from that table, and an origin gateway's ledger
(``kanban_origin_ledger._advanced_observation``) advances an obligation only from a newer owner record that still
carries the exact marker. An owner reprovisioned from the Board therefore republished every courier card with
``origin_bindings: []``, and the origin chat's obligation stayed at its last observation forever (fail-closed: never
falsely released, but never completed either); a receipt still in flight stayed ``origin_publication_pending``
(t_01a1cf7ee5a6103e).

Two hooks:

- :func:`record` runs in the writer's transaction right after the intent row is inserted and appends one task event
  (``origin_intent_recorded``) holding the whole row. Task events sync, so the Board keeps a copy.
- :func:`restore_from_event` runs when the sync apply inserts such an event. For a card this file got back from the
  Board (``kanban_restored_cards``, written by ``kanban_restore_chat_subs.note_restored_task`` in a primary live
  board file) whose binding names THIS gateway as owner and this card, it re-inserts the exact row
  (``INSERT OR IGNORE``: an existing row is never changed). Foreign copies, replicas, unmarked cards and
  malformed payloads are skipped.

Retention: local ``gc_events`` keeps this kind (like ``decomposed``). The Board service drops events older than its
30-day window, so a card that was open longer than that before it was lost still cannot be rebuilt: it fails closed
exactly as before.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Mapping

EVENT_KIND = "origin_intent_recorded"
_FIELDS = ("request_id", "intent_json", "binding_json", "child_request_id")
INTENTS_DDL = """CREATE TABLE IF NOT EXISTS origin_creation_intents (
            request_id TEXT PRIMARY KEY NOT NULL,
            intent_json TEXT NOT NULL,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            binding_json TEXT NOT NULL,
            child_request_id TEXT NOT NULL UNIQUE)"""


def ensure_task_index(conn: sqlite3.Connection) -> None:
    """Index owner-marker equality reads without creating the lazy intent table.

    Keep binding_json out of the index: payloads can be large. Equal task IDs
    retain rowid order, as in the original table scan; no read semantics change.
    """
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='origin_creation_intents'").fetchone():
        conn.execute('CREATE INDEX IF NOT EXISTS idx_origin_intents_task ON origin_creation_intents(task_id)')


def record(conn: sqlite3.Connection, task_id: str, request_id: str, intent_json: str, binding_json: str,
           child_request_id: str) -> None:
    """Append the synced copy of a just-inserted ``origin_creation_intents`` row (caller's transaction).

    A plain row, not ``append_event``: it records an identity, not a lifecycle step, so it must not stamp recovery
    history or snapshot wake evidence (an adoption leaves the owner card's state untouched)."""
    payload = {"v": 1, "request_id": request_id, "intent_json": intent_json, "binding_json": binding_json,
               "child_request_id": child_request_id}
    conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, NULL, ?, ?, ?)",
                 (task_id, EVENT_KIND, json.dumps(payload, ensure_ascii=False), int(time.time())))


def _row(payload: Any, task_id: str, own: str) -> tuple | None:
    """The exact intent row an event payload carries for ``task_id`` owned by ``own``, or None."""
    from hermes_cli.kanban_origin_ledger import validate_binding
    try:
        data = json.loads(payload) if isinstance(payload, str) else payload
        if not isinstance(data, dict) or data.get("v") != 1:
            return None
        values = [data.get(f) for f in _FIELDS]
        if not all(isinstance(v, str) and v for v in values):
            return None
        binding = json.loads(data["binding_json"])
        validate_binding({**binding, "publication_version": 1})
        json.loads(data["intent_json"])
    except (TypeError, ValueError):
        return None
    owner = binding["owner"]
    if owner["gateway"] != own or owner["task_id"] != task_id:
        return None  # another gateway's card (its owner rebuilds it), or a payload naming another card
    request_id, intent_json, binding_json, child_request_id = values
    return request_id, intent_json, task_id, binding_json, child_request_id


def restore_from_event(conn: sqlite3.Connection, cols: Mapping[str, Any], own: str) -> bool:
    """Rebuild the intent row a just-inserted ``origin_intent_recorded`` event of a restored card carries."""
    if cols.get("kind") != EVENT_KIND or not own:
        return False
    task_id = cols.get("task_id")
    if not task_id or not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_restored_cards'").fetchone():
        return False
    if conn.execute("SELECT 1 FROM kanban_restored_cards WHERE task_id = ?", (task_id,)).fetchone() is None:
        return False  # only a card this primary live file got back from the Board
    row = _row(cols.get("payload"), task_id, own)
    if row is None:
        return False
    conn.execute(INTENTS_DDL)
    ensure_task_index(conn)
    return conn.execute("INSERT OR IGNORE INTO origin_creation_intents VALUES (?,?,?,?,?)", row).rowcount == 1
