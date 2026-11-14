"""Read-only, scoped snapshot of the durable origin ledger (annotation dependency).

The caller holds a read transaction on a ledger connection; this helper only
SELECTs (no schema creation, no ``project``/``refresh_observations`` writes, no
caches or file readers). Raw ledger columns are preserved exactly: the token
covers every relevant obligation column (including released rows) and relevant
receipts, so any ledger change that can alter a projection changes the token.
Malformed rows fail closed with ``ValueError``. Parsed mappings are immutable
selector data, NOT arguments to the legacy projection helpers (which require
concrete dict/list types). Use json.loads(binding_json/receipt_json) at any such
boundary; a shallow dict() does not thaw nested values.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from types import MappingProxyType

from hermes_cli import kanban_origin_ledger as _ledger


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


@dataclass(frozen=True)
class LedgerSnapshot:
    gateway: str
    profile: str
    obligations: tuple   # ordered by binding_id; read-only mappings with raw columns + parsed binding
    receipts: tuple      # ordered by request_id; raw columns + parsed receipt
    owner_keys: tuple    # sorted ((owner gateway, task_record_key), ...)
    own_boards: tuple    # sorted ((profile, board), ...) where owner scope == origin scope
    token: str


def _rows(conn: sqlite3.Connection, sql: str) -> list:
    # An existing but incomplete ledger is unkeyable, not an empty authority.
    # A missing FILE is handled by the outer reader, before opening read-only.
    return conn.execute(sql).fetchall()


def _canon(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def snapshot(conn: sqlite3.Connection, gateway: str, profile: str) -> LedgerSnapshot:
    if not isinstance(gateway, str) or not gateway or not isinstance(profile, str) or not profile:
        raise ValueError("origin_scope_invalid")
    if not conn.in_transaction:
        raise ValueError("origin_ledger_snapshot_required")
    obligations, hashes = [], set()
    for key, col_hash, raw, resolved, task_raw, version, released in sorted(_rows(
            conn, "SELECT binding_id,binding_hash,binding_json,resolved_session,task_json,"
                  "observation_version,released FROM origin_obligations"), key=lambda r: str(r[0])):
        try:
            binding = json.loads(raw)
        except (TypeError, ValueError):
            raise ValueError("origin_binding_invalid") from None
        _ledger.validate_binding(binding)
        origin = binding["origin"]
        if (origin["gateway"], origin["profile"]) != (gateway, profile):
            continue
        digest = _ledger.binding_hash(binding)
        if key != _ledger._binding_key(binding) or col_hash != digest:
            raise ValueError("origin_obligation_inconsistent")
        hashes.add(digest)
        obligations.append({"binding_id": key, "binding_hash": col_hash, "binding_json": raw,
                            "resolved_session": resolved, "task_json": task_raw,
                            "observation_version": version, "released": released,
                            "binding": binding})
    receipts = []
    for request_id, col_hash, raw in sorted(_rows(
            conn, "SELECT request_id,binding_hash,receipt_json FROM origin_receipts"), key=lambda r: str(r[0])):
        try:
            ack = json.loads(raw)
            linked = ack["binding_hash"]
            if not isinstance(ack, dict) or not isinstance(linked, str):
                raise ValueError
        except (TypeError, ValueError, KeyError):
            raise ValueError("origin_receipt_invalid") from None
        if linked in hashes:  # parsed linkage, as project() does; the SQL column is preserved raw only
            receipts.append({"request_id": request_id, "binding_hash": col_hash,
                             "receipt_json": raw, "receipt": ack})
    owner_keys = sorted({(o["binding"]["owner"]["gateway"], o["binding"]["task_record_key"]) for o in obligations})
    own_boards = sorted({(o["binding"]["owner"]["profile"], o["binding"]["owner"]["board"]) for o in obligations
                         if (o["binding"]["owner"]["gateway"], o["binding"]["owner"]["profile"]) == (gateway, profile)})
    plain = lambda rows: [{k: v for k, v in r.items() if k not in ("binding", "receipt")} for r in rows]
    token = hashlib.sha256(_canon({"scope": [gateway, profile], "obligations": plain(obligations),
                                   "receipts": plain(receipts)}).encode()).hexdigest()
    return LedgerSnapshot(gateway, profile,
                          tuple(MappingProxyType({k: _freeze(v) for k, v in o.items()}) for o in obligations),
                          tuple(MappingProxyType({k: _freeze(v) for k, v in r.items()}) for r in receipts),
                          tuple(owner_keys), tuple(own_boards), token)


def _task(raw):
    task = json.loads(raw)
    if not isinstance(task, dict):
        raise ValueError("origin_task_invalid")
    # JSON accepts NaN/Infinity by default; no reusable equality for these.
    json.dumps(task, allow_nan=False)
    return task


def current_matches(ledger: LedgerSnapshot, rows, cache: dict | None = None) -> tuple:
    """Raw equality part of project()'s current verdict, NOT owner freshness.

    Normalized task tokens cannot replace this: a liveness-only publication can
    make a ledger observation no longer current until reconciliation advances it.
    The outer union must separately include heartbeat/readability and clock gates.
    No SQLite writes, row normalization, or authority decisions occur here.

    ``cache`` (caller-owned, replaced in place): per binding, the verdict for the exact immutable replica row
    object and ledger ``(task_json, observation_version)`` it was computed from, so an unchanged pair is not
    decoded again on the next generation.
    """
    from hermes_cli.kanban_replica_rows import text

    result, fresh = [], {}
    for obligation in ledger.obligations:
        binding = obligation["binding"]
        key = (binding["owner"]["gateway"], binding["task_record_key"])
        row = rows.by_key.get(key)
        raw, version = obligation["task_json"], obligation["observation_version"]
        hit = cache.get(obligation["binding_id"]) if cache is not None else None
        if hit is not None and hit[0] is row and hit[1] == raw and hit[2] == version:
            equal = hit[3]
        else:
            task = _task(raw)
            equal = bool(row is not None and row[4] == version and _task(text(row[2])) == task)
        fresh[obligation["binding_id"]] = (row, raw, version, equal)
        result.append((obligation["binding_id"], equal))
    if cache is not None:
        cache.clear()
        cache.update(fresh)
    return tuple(result)
