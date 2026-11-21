"""Durable Phase A capture envelopes and fail-closed database export. [daveey]

Runtime consumers use append_records/seal_records in an exclusively owned directory. DB export preserves
its transactionally monotonic journal_seq separately from each exporter process epoch's envelope seq.
New journals persist local observation UIDs and optionally writer-side clock samples. Legacy
rows stay unknown: export never substitutes drain-time clocks or a remote lifecycle event UID.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Optional

from hermes_cli import kanban_fairness_journal as journal
from hermes_cli import kanban_fairness_segments as segments
from hermes_cli.kanban_fairness_schema import MAX_PAYLOAD_BYTES, VERSION, decode_owner, make_record
from hermes_cli.kanban_fairness_segments import append_records, seal_records

__all__ = ["append_records", "drain", "load", "seal_records", "write_manifest"]

RECEIPTS, MANIFEST, OWNERS = segments.RECEIPTS, segments.MANIFEST, "owner_authority.jsonl"
MAX_OWNER_BYTES = 64 * 1024 * 1024
_PROCESS_EPOCH = (os.getpid(), str(uuid.uuid4()))


def _epoch():
    global _PROCESS_EPOCH
    if _PROCESS_EPOCH[0] != os.getpid():
        _PROCESS_EPOCH = (os.getpid(), str(uuid.uuid4()))
    return _PROCESS_EPOCH[1]


def _write(path, data):
    segments.write(path, data)


def _receipts(out):
    return segments.receipts(out)


def _gap(receipts: dict, code: str, detail: str) -> None:
    if not any(g["code"] == code for g in receipts["gaps"]):
        receipts["gaps"].append({"code": code, "detail": detail})


def _bind(conn, receipts, state):
    from hermes_cli.kanban_db_sync import sync_state

    identity = sync_state(conn) or {}
    database_uid = "evidence-local:" + str(uuid.uuid4())
    receipts.update(capture_id=state["capture_id"], db=state["db"], armed_ms=state["armed_ms"],
                    database_uid=database_uid, journal_seq=0,
                    source_gateway=identity.get("gateway_id") or database_uid,
                    board_uid=identity.get("board_id") or database_uid)
    if not identity.get("gateway_id") or not identity.get("board_id"):
        _gap(receipts, "source_identity_unknown", "evidence-local database identity is not cross-host authority")


def drain(conn: sqlite3.Connection, out: Path) -> dict:
    """Export journal batches outside scheduler locks. Unknown provenance stays an explicit gap."""
    out.mkdir(parents=True, exist_ok=True)
    receipts = _receipts(out)
    state = journal.status(conn)
    if state is None:
        if receipts["capture_id"] is not None:
            _gap(receipts, "journal_missing", "database no longer carries the armed journal")
            _write(out / RECEIPTS, segments.encoded(receipts))
        return receipts
    from hermes_cli.kanban_fairness_budget import initialize

    initialize(out)
    if receipts["capture_id"] is None:
        _bind(conn, receipts, state)
        _write(out / RECEIPTS, segments.encoded(receipts))
    elif (receipts["capture_id"], receipts.get("armed_ms"), receipts.get("db")) != (
            state["capture_id"], state["armed_ms"], state["db"]):
        _gap(receipts, "journal_replaced", "directory belongs to another database journal")
        _write(out / RECEIPTS, segments.encoded(receipts))
        return receipts
    if (out / MANIFEST).exists():
        return receipts
    if state.get("trigger_integrity") is not True:
        previous_gaps = len(receipts["gaps"])
        if state.get("trigger_integrity") is False:
            _gap(receipts, "trigger_loss", "capture triggers are missing or changed")
        else:
            _gap(receipts, "trigger_integrity_unknown", "journal does not attest capture trigger integrity")
        # Preserve newly observed gaps even when no new rows arrive.
        if len(receipts["gaps"]) != previous_gaps:
            _write(out / RECEIPTS, segments.encoded(receipts))
    while True:
        rows = list(journal.rows(conn, receipts["journal_seq"], limit=128))
        if not rows:
            return receipts
        cuts = segments.source_map(receipts["sources"])
        epoch = _epoch()
        seq = cuts.get((receipts["source_gateway"], epoch), 0)
        records = []
        try:
            provenance = _provenance(conn, rows[0][0], rows[-1][0])
            for db_seq, kind, wall_ms, task_id, op, fields in rows:
                seq += 1
                source_uid, clock = provenance.get(db_seq, (None, {}))
                record_kind = "opening" if kind == "opening" else "row_change"
                payload = {"task_id": task_id, "op": op, "fields": json.loads(fields),
                           "journal_seq": db_seq, "journal_kind": kind,
                           "database_uid": receipts["database_uid"], "source_uid": source_uid}
                if kind == "creation":
                    record_kind, payload = "creation", json.loads(fields)
                if len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > MAX_PAYLOAD_BYTES:
                    record_kind = "loss"
                    # These are exporter-epoch sequence bounds, not durable journal sequence IDs.
                    payload = {"reason": "oversize", "count": 1, "first_seq": seq, "last_seq": seq,
                               "start_wall_ns": wall_ms * 1_000_000, "end_wall_ns": wall_ms * 1_000_000}
                records.append(make_record(
                    capture_id=state["capture_id"], source_gateway=receipts["source_gateway"],
                    board_uid=receipts["board_uid"], source_epoch=epoch, seq=seq,
                    kind=record_kind, wall_ns=clock.get("wall_ns", wall_ms * 1_000_000), payload=payload,
                    mono_ns=clock.get("mono_ns"), clock_error_ns=clock.get("clock_error_ns")))
            receipts = append_records(out, records, _metadata={"journal_seq": rows[-1][0]})
        except (ValueError, OSError, TypeError, RecursionError) as exc:
            _gap(receipts, "export_failed", type(exc).__name__)
            _write(out / RECEIPTS, segments.encoded(receipts))
            return receipts



def _provenance(conn, first, last):
    columns = {r[1] for r in conn.execute(f"PRAGMA table_info({journal.JOURNAL})")}
    if not {"source_uid", "clock_json"} <= columns:
        return {}
    result = {}
    for seq, uid, raw in conn.execute(
            f"SELECT seq, source_uid, clock_json FROM {journal.JOURNAL} WHERE seq BETWEEN ? AND ?", (first, last)):
        clock = json.loads(raw) if raw is not None else {}
        if (not isinstance(clock, dict) or (raw is not None and (
                set(clock) != {"wall_ns", "mono_ns", "clock_error_ns"}
                or any(type(v) is not int or v < 0 for v in clock.values())))):
            raise ValueError("invalid journal clock sample")
        result[seq] = (uid, clock)
    return result


def write_manifest(conn: sqlite3.Connection, out: Path, *, board: str, assumptions: dict,
                   policy: dict, roster: Optional[list] = None) -> dict:
    if (out / MANIFEST).exists():
        raise ValueError("capture already sealed")
    state = journal.seal(conn)
    receipts = drain(conn, out)
    manifest = {
        **receipts, "version": VERSION, "capture_id": state["capture_id"], "board": board, "db": state["db"],
        "window": {"armed_ms": state["armed_ms"], "sealed_ms": state["sealed_ms"]},
        "journal": {k: state[k] for k in ("stored", "bytes", "dropped", "first_drop_ms", "last_drop_ms",
                                         "max_records", "max_bytes", "last_seq")},
        "assumptions": assumptions, "policy": policy, "roster": roster or [], "sealed": True,
    }
    # Empty captures cannot attest an epoch's final drain, and over-limit journal payload is a gap.
    if not receipts["sources"]:
        _gap(manifest, "source_tail", "no exporter epoch was captured")
    actual_bytes = conn.execute(f"SELECT COALESCE(SUM(length(CAST(fields AS BLOB))), 0) FROM {journal.JOURNAL}").fetchone()[0]
    if actual_bytes > state["max_bytes"] or state["stored"] > state["max_records"]:
        _gap(manifest, "journal_bound", "stored journal exceeded its declared bound")
    manifest["final_seal"] = segments.final_seal(manifest)
    _write(out / MANIFEST, segments.encoded(manifest))
    return manifest


def load(out: Path) -> dict:
    from hermes_cli.kanban_fairness_root import collect, files

    try:
        paths, _ = files(out)
    except (OSError, ValueError):
        return {"manifest": {}, "records": [], "owners": None, "gaps": [{"code": "capture_bound"}]}
    capture = collect(out, _load_one(out), _load_one)
    if any(path.name.endswith(".tmp") for path in paths):
        capture["gaps"].append({"code": "capture_orphan"})
    return capture


def _load_one(out: Path) -> dict:
    from hermes_cli.kanban_fairness_tail import validate_tail

    capture = segments.load_records(out)
    manifest, records, gaps = capture["manifest"], capture["records"], capture["gaps"]
    gaps.extend(validate_tail(out, manifest))
    expected = 1
    for record in records:
        if record["kind"] == "creation" and "journal" in manifest:
            # Exactly one exported record per journal row; creation's frozen payload has no cut.
            # Adjacent lifecycle cuts and the sealed journal count fence this position.
            expected += 1
            continue
        if record["kind"] not in ("opening", "row_change"):
            continue
        payload = record["payload"]
        if payload["journal_seq"] != expected:
            gaps.append({"code": "journal_sequence_gap"})
        expected = payload["journal_seq"] + 1
        if payload["source_uid"] is None:
            gaps.append({"code": "source_uid_unknown"})
    counters = manifest.get("journal")
    if "journal" in manifest and not _valid_counters(counters):
        gaps.append({"code": "bad_journal_counters"})
    elif counters is not None:
        if counters["dropped"]:
            gaps.append({"code": "journal_dropped", "detail": "journal bound dropped changes"})
        if counters["stored"] != len(records) or counters["last_seq"] != expected - 1:
            gaps.append({"code": "record_count"})
    capture["owners"] = _owners(out, gaps)
    capture["gaps"] = segments.dedupe_gaps(gaps)
    return capture


def _valid_counters(counters):
    required = {"stored", "bytes", "dropped", "first_drop_ms", "last_drop_ms", "max_records", "max_bytes", "last_seq"}
    if not isinstance(counters, dict) or set(counters) != required:
        return False
    for name, value in counters.items():
        if value is None and name in ("first_drop_ms", "last_drop_ms"):
            continue
        if type(value) is not int or value < 0:
            return False
    return (counters["stored"] <= counters["max_records"] and counters["bytes"] <= counters["max_bytes"]
            and counters["max_records"] > 0 and counters["max_bytes"] > 0)


def _owners(out: Path, gaps: list[dict]) -> Optional[list[dict]]:
    path = out / OWNERS
    if not path.exists():
        return None
    owners = []
    try:
        # A JSONL journal is not metadata. Keep all coverage/interval lines, not a tail
        # which can silently discard the beginning of the capture's owner history.
        data = segments.read_bounded(path, MAX_OWNER_BYTES)
        lines = data.decode("utf-8-sig").splitlines()
    except (OSError, ValueError):
        gaps.append({"code": "bad_owner_record", "detail": "owner file unreadable or over bound"})
        return owners
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            owners.append(decode_owner(json.loads(line)))
        except (ValueError, TypeError, RecursionError) as exc:
            gaps.append({"code": "bad_owner_record", "detail": f"{OWNERS}:{number}: {exc}"})
    return owners
