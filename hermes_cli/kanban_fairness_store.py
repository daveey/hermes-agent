"""Durable files of one queue-fairness capture: hashed JSONL segments and a sealed manifest. [daveey]

``drain`` copies journal rows the directory does not hold yet into ``segment-<n>.jsonl`` (written whole,
fsynced, renamed) and records each segment's hash in ``receipts.json``. ``write_manifest`` closes the capture
with the journal's own counters. ``load`` reads a directory back for the reader and reports, rather than
repairs, anything that makes the capture incomplete: no seal, a changed or missing segment, a hole in the
sequence, dropped changes, or a journal that moved to another database file (a replica rebuild).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Optional

from hermes_cli import kanban_fairness_journal as journal
from hermes_cli.kanban_fairness_schema import SEGMENT_BYTES, VERSION, BadRecord, decode_owner, decode_record

RECEIPTS, MANIFEST, OWNERS = "receipts.json", "manifest.json", "owner_authority.jsonl"


def _write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def _receipts(out: Path) -> dict:
    path = out / RECEIPTS
    if not path.exists():
        return {"version": VERSION, "capture_id": None, "db": None, "armed_ms": None, "segments": [], "gaps": []}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def drain(conn: sqlite3.Connection, out: Path) -> dict:
    """Append the journal's new rows to ``out``; returns the receipts (with any gap this call found)."""
    out.mkdir(parents=True, exist_ok=True)
    receipts = _receipts(out)
    state = journal.status(conn)
    if state is None:
        if receipts["capture_id"] is not None:
            _gap(receipts, "journal_missing", "the database no longer carries the armed journal")
            _write(out / RECEIPTS, json.dumps(receipts, indent=1).encode())
        return receipts
    if receipts["capture_id"] is None:
        receipts.update(capture_id=state["capture_id"], db=state["db"], armed_ms=state["armed_ms"])
    elif (receipts["capture_id"], receipts["armed_ms"], receipts["db"]) != (
            state["capture_id"], state["armed_ms"], state["db"]):
        _gap(receipts, "journal_replaced", "the journal on this database is not the one this directory drains")
        _write(out / RECEIPTS, json.dumps(receipts, indent=1).encode())
        return receipts
    last = receipts["segments"][-1]["last_seq"] if receipts["segments"] else 0
    while True:
        lines, size, first = [], 0, None
        for seq, kind, wall_ms, task_id, op, fields in journal.rows(conn, last):
            line = json.dumps({"version": VERSION, "seq": seq, "kind": kind, "wall_ms": wall_ms,
                               "task_id": task_id, "op": op, "fields": json.loads(fields)},
                              separators=(",", ":"), sort_keys=True)
            lines.append(line)
            size += len(line) + 1
            first = seq if first is None else first
            last = seq
            if size >= SEGMENT_BYTES:
                break
        if not lines:
            break
        data = ("\n".join(lines) + "\n").encode()
        name = f"segment-{len(receipts['segments']) + 1:06d}.jsonl"
        _write(out / name, data)
        receipts["segments"].append({"name": name, "first_seq": first, "last_seq": last, "records": len(lines),
                                     "sha256": hashlib.sha256(data).hexdigest()})
        _write(out / RECEIPTS, json.dumps(receipts, indent=1).encode())
    return receipts


def _gap(receipts: dict, code: str, detail: str) -> None:
    if not any(g["code"] == code for g in receipts["gaps"]):
        receipts["gaps"].append({"code": code, "detail": detail})


def write_manifest(conn: sqlite3.Connection, out: Path, *, board: str, assumptions: dict,
                   policy: dict, roster: Optional[list] = None) -> dict:
    """Seal the journal, drain its tail and write ``manifest.json``. An unsealed directory is incomplete."""
    state = journal.seal(conn)
    receipts = drain(conn, out)
    manifest = {
        "version": VERSION, "capture_id": state["capture_id"], "board": board, "db": state["db"],
        "window": {"armed_ms": state["armed_ms"], "sealed_ms": state["sealed_ms"]},
        "journal": {k: state[k] for k in ("stored", "bytes", "dropped", "first_drop_ms", "last_drop_ms",
                                         "max_records", "max_bytes", "last_seq")},
        "segments": receipts["segments"], "gaps": receipts["gaps"], "assumptions": assumptions,
        "policy": policy, "roster": roster or [], "sealed": True,
    }
    _write(out / MANIFEST, json.dumps(manifest, indent=1, sort_keys=True).encode())
    return manifest


def load(out: Path) -> dict:
    """``{"manifest", "records", "owners", "gaps"}`` for the reader. ``gaps`` is every reason the capture cannot
    certify a pass; ``records`` holds what could be decoded, in sequence order."""
    gaps: list[dict] = []
    path = out / MANIFEST
    if not path.exists():
        receipts = _receipts(out)
        gaps.append({"code": "unsealed", "detail": "no manifest.json: the capture was never sealed"})
        manifest = {"version": VERSION, "capture_id": receipts["capture_id"], "window": {
            "armed_ms": receipts["armed_ms"], "sealed_ms": None}, "journal": None,
            "segments": receipts["segments"], "gaps": receipts["gaps"], "assumptions": {}, "policy": {},
            "sealed": False}
    else:
        manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    gaps.extend(manifest.get("gaps") or [])
    records, expected = [], 1
    for seg in manifest.get("segments") or []:
        file = out / seg["name"]
        data = file.read_bytes() if file.exists() else None
        if data is None or hashlib.sha256(data).hexdigest() != seg["sha256"]:
            gaps.append({"code": "segment_mismatch", "detail": f"{seg['name']} is missing or changed"})
            continue
        for number, line in enumerate(data.decode().splitlines(), 1):
            try:
                record = decode_record(json.loads(line))
            except (ValueError, BadRecord) as exc:
                gaps.append({"code": "bad_record", "detail": f"{seg['name']}:{number}: {exc}"})
                continue
            if record["seq"] != expected:
                gaps.append({"code": "sequence_gap", "detail": f"expected seq {expected}, read {record['seq']}"})
            expected = record["seq"] + 1
            records.append(record)
    counters = manifest.get("journal")
    if counters:
        if counters["dropped"]:
            gaps.append({"code": "journal_dropped", "detail": f"{counters['dropped']} change(s) past the "
                         f"journal bound, first at {counters['first_drop_ms']} ms"})
        if counters["stored"] != len(records) or counters["last_seq"] != expected - 1:
            gaps.append({"code": "record_count", "detail": f"journal stored {counters['stored']} (last seq "
                         f"{counters['last_seq']}), directory holds {len(records)}"})
    return {"manifest": manifest, "records": records, "owners": _owners(out, gaps), "gaps": gaps}


def _owners(out: Path, gaps: list[dict]) -> Optional[list[dict]]:
    path = out / OWNERS
    if not path.exists():
        return None
    owners = []
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            owners.append(decode_owner(json.loads(line)))
        except (ValueError, BadRecord) as exc:
            gaps.append({"code": "bad_owner_record", "detail": f"{OWNERS}:{number}: {exc}"})
    return owners
