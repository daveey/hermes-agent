"""Single-consumer envelope segments; hashes attest bytes, not coverage."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from hermes_cli.kanban_fairness_schema import SEGMENT_BYTES, VERSION, BadRecord, decode_record

RECEIPTS, MANIFEST = "receipts.json", "manifest.json"
MAX_CAPTURE_BYTES = 512 * 1024 * 1024


def encoded(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def write(path: Path, data: bytes) -> None:
    from hermes_cli.kanban_fairness_budget import write as bounded_write

    bounded_write(path, data)


MAX_METADATA_BYTES = 8 * 1024 * 1024


def read_bounded(path, limit):
    """Bound the allocation even if the file grows after stat."""
    with path.open("rb") as stream:
        if os.fstat(stream.fileno()).st_size > limit:
            raise BadRecord("file exceeds storage bound")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise BadRecord("file exceeds storage bound")
    return data


def dedupe_gaps(gaps):
    return list({encoded(g): g for g in gaps}.values())


def checked_gaps(gaps):
    if not isinstance(gaps, list) or any(
            not isinstance(g, dict) or not isinstance(g.get("code"), str) or not g["code"]
            or ("detail" in g and not isinstance(g["detail"], str)) for g in gaps):
        raise BadRecord("invalid coverage gaps")
    return dedupe_gaps(gaps)


def validate_state(state, *, check_totals=True):
    required = {"version", "capture_id", "board_uid", "segments", "sources", "gaps", "bytes"}
    if not isinstance(state, dict) or not required <= state.keys():
        raise BadRecord("invalid capture metadata")
    if type(state["version"]) is not int or state["version"] != VERSION:
        raise BadRecord("invalid capture version")
    for key in ("capture_id", "board_uid"):
        if state[key] is not None and (not isinstance(state[key], str) or not state[key]):
            raise BadRecord("invalid capture identity")
    if (state["capture_id"] is None) != (state["board_uid"] is None):
        raise BadRecord("partial capture identity")
    if type(state["bytes"]) is not int or not 0 <= state["bytes"] <= MAX_CAPTURE_BYTES:
        raise BadRecord("invalid capture byte count")
    if not isinstance(state["segments"], list):
        raise BadRecord("invalid segment list")
    total = 0
    for item in state["segments"]:
        if (not isinstance(item, dict) or set(item) != {"name", "bytes", "records", "sha256"}
                or not isinstance(item["name"], str)
                or not re.fullmatch(r"segment-\d{6}\.jsonl", item["name"])
                or type(item["bytes"]) is not int or not 0 < item["bytes"] <= SEGMENT_BYTES
                or type(item["records"]) is not int or not 0 < item["records"] <= item["bytes"]
                or not isinstance(item["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
            raise BadRecord("invalid segment receipt")
        total += item["bytes"]
    cuts = source_map(state["sources"])
    if total > MAX_CAPTURE_BYTES or (check_totals and total != state["bytes"]) or bool(cuts) != bool(total):
        raise BadRecord("inconsistent capture totals or empty source cuts")
    if total and state["capture_id"] is None:
        raise BadRecord("missing capture identity")
    checked_gaps(state["gaps"])
    if "journal_seq" in state and (type(state["journal_seq"]) is not int or state["journal_seq"] < 0):
        raise BadRecord("invalid journal cursor")
    if "journal_seq" in state:
        for name in ("source_gateway", "database_uid"):
            if not isinstance(state.get(name), str) or not state[name]:
                raise BadRecord("invalid database export identity")
        db = state.get("db")
        if (not isinstance(db, dict) or set(db) != {"path", "dev", "ino"}
                or not isinstance(db["path"], str) or not db["path"]
                or any(type(db[k]) is not int or db[k] < 0 for k in ("dev", "ino"))):
            raise BadRecord("invalid database file identity")
        if type(state.get("armed_ms")) is not int or state["armed_ms"] < 0:
            raise BadRecord("invalid database export opening cut")
    return state


def receipts(out: Path) -> dict:
    path = out / RECEIPTS
    if path.exists():
        try:
            return validate_state(json.loads(read_bounded(path, MAX_METADATA_BYTES).decode("utf-8-sig")))
        except (ValueError, TypeError, RecursionError) as exc:
            raise BadRecord("invalid receipts") from exc
    return {"version": VERSION, "capture_id": None, "board_uid": None, "segments": [],
            "sources": [], "gaps": [], "bytes": 0}


def source_key(record):
    return record["source_gateway"], record["source_epoch"]


def source_map(sources):
    result = {}
    if not isinstance(sources, list):
        raise BadRecord("source cuts must be a list")
    for item in sources:
        if not isinstance(item, dict) or set(item) != {"source_gateway", "source_epoch", "last_seq"}:
            raise BadRecord("invalid source closing cut")
        key = source_key(item)
        if any(not isinstance(x, str) or not x for x in key) or key in result:
            raise BadRecord("invalid or duplicate source identity")
        seq = item["last_seq"]
        if type(seq) is not int or seq <= 0:
            raise BadRecord("invalid source closing sequence")
        result[key] = seq
    return result


def source_rows(cuts):
    return [{"source_gateway": g, "source_epoch": e, "last_seq": n} for (g, e), n in sorted(cuts.items())]


def append_records(out: Path, records, *, max_bytes: int = MAX_CAPTURE_BYTES, _metadata=None) -> dict:
    """Append a caller-sized batch; rejection is loss. One consumer owns the directory.

    Segment and capture byte ceilings do not bound transient batch memory. Runtime callers must
    bound their own queue and batch sizes; this writer materializes encoded lines and segments.
    """
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_CAPTURE_BYTES:
        raise ValueError("max_bytes outside capture bound")
    if (out / MANIFEST).exists():
        raise ValueError("capture is already sealed")
    state = receipts(out)
    cuts = source_map(state["sources"])
    if _metadata is not None:
        if not isinstance(_metadata, dict) or set(_metadata) != {"journal_seq"}:
            raise BadRecord("internal metadata may only advance journal_seq")
        cursor = _metadata["journal_seq"]
        if type(cursor) is not int or cursor < state.get("journal_seq", 0):
            raise BadRecord("invalid journal cursor advance")
    for index, item in enumerate(state["segments"], 1):
        if item["name"] != f"segment-{index:06d}.jsonl":
            raise BadRecord("invalid segment order")
    batches, lines, size, total = [], [], 0, state["bytes"]
    for raw in records:
        rec = decode_record(raw)
        if state["capture_id"] is None:
            state.update(capture_id=rec["capture_id"], board_uid=rec["board_uid"])
        if (rec["capture_id"], rec["board_uid"]) != (state["capture_id"], state["board_uid"]):
            raise BadRecord("capture/board identity changed")
        key = source_key(rec)
        if rec["seq"] != cuts.get(key, 0) + 1:
            raise BadRecord("sequence must start at 1 and increase without gaps within source epoch")
        cuts[key] = rec["seq"]
        data = encoded(rec) + b"\n"
        if len(data) > SEGMENT_BYTES or total + len(data) > max_bytes:
            raise ValueError("capture storage or record bound exceeded")
        if lines and size + len(data) > SEGMENT_BYTES:
            batches.append(lines)
            lines, size = [], 0
        lines.append(data)
        size += len(data)
        total += len(data)
    if lines:
        batches.append(lines)
    names = [f"segment-{len(state['segments']) + i + 1:06d}.jsonl" for i in range(len(batches))]
    if any((out / name).exists() for name in names):
        raise ValueError("unacknowledged segment exists; capture incomplete")
    out.mkdir(parents=True, exist_ok=True)
    data_batches = []
    for name, batch in zip(names, batches):
        data = b"".join(batch)
        data_batches.append(data)
        state["segments"].append({"name": name, "records": len(batch), "bytes": len(data),
                                  "sha256": hashlib.sha256(data).hexdigest()})
    state.update(sources=source_rows(cuts), bytes=total)
    if _metadata is not None:
        state.update(_metadata)
    state["gaps"] = checked_gaps(state["gaps"])
    validate_state(state)
    receipt_data = encoded(state)
    if len(receipt_data) > MAX_METADATA_BYTES:
        raise BadRecord("receipt metadata bound exceeded")
    for name, data in zip(names, data_batches):
        write(out / name, data)
    write(out / RECEIPTS, receipt_data)
    return state


def final_seal(manifest):
    """Bind ordered segments AND explicit epoch closing cuts."""
    return hashlib.sha256(encoded({k: v for k, v in manifest.items() if k != "final_seal"})).hexdigest()


def seal_records(out: Path, *, expected_sources: list[dict], gaps=None) -> dict:
    state = receipts(out)
    expected = source_map(expected_sources)
    if not expected or expected != source_map(state["sources"]):
        raise BadRecord("source closing cuts do not cover exactly the recorded epochs")
    if (out / MANIFEST).exists():
        raise ValueError("capture already sealed")
    manifest = {**state, "sealed": True, "gaps": checked_gaps(state["gaps"] + checked_gaps([] if gaps is None else gaps))}
    manifest["final_seal"] = final_seal(manifest)
    write(out / MANIFEST, encoded(manifest))
    return manifest


def _read_segment(out, seg, gaps):
    name = seg.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"segment-\d{6}\.jsonl", name):
        gaps.append({"code": "bad_segment_name"})
        return []
    try:
        data = read_bounded(out / name, SEGMENT_BYTES)
        if (len(data) > SEGMENT_BYTES or len(data) != seg.get("bytes")
                or hashlib.sha256(data).hexdigest() != seg.get("sha256")):
            raise ValueError("segment size/hash mismatch")
        lines = data.decode("utf-8").splitlines()
        if not data.endswith(b"\n") or len(lines) != seg.get("records"):
            raise ValueError("segment count/tail mismatch")
        return lines
    except (OSError, ValueError):
        gaps.append({"code": "segment_mismatch", "detail": name})
        return []


def load_records(out: Path) -> dict:
    gaps = []
    try:
        path = out / MANIFEST
        manifest = validate_state(json.loads(read_bounded(path, MAX_METADATA_BYTES).decode("utf-8-sig")), check_totals=False) if path.exists() else receipts(out)
    except (OSError, ValueError, TypeError, RecursionError):
        return {"manifest": {}, "records": [], "gaps": [{"code": "bad_manifest"}]}
    if manifest.get("sealed") is not True or not manifest.get("final_seal"):
        gaps.append({"code": "unsealed"})
    else:
        try:
            if manifest["final_seal"] != final_seal(manifest):
                gaps.append({"code": "seal_mismatch"})
        except (ValueError, TypeError, RecursionError):
            return {"manifest": {}, "records": [], "gaps": [{"code": "bad_manifest"}]}
    gaps.extend(manifest.get("gaps") or [])
    if sum(s["bytes"] for s in manifest["segments"]) != manifest["bytes"]:
        gaps.append({"code": "capture_bytes"})
    records, cuts, names = [], {}, []
    for index, seg in enumerate(manifest.get("segments") or [], 1):
        names.append(seg.get("name"))
        if seg.get("name") != f"segment-{index:06d}.jsonl":
            gaps.append({"code": "segment_order"})
        for line in _read_segment(out, seg, gaps):
            try:
                rec = decode_record(json.loads(line))
            except (ValueError, TypeError, RecursionError):
                gaps.append({"code": "bad_record"})
                continue
            if (rec["capture_id"], rec["board_uid"]) != (manifest.get("capture_id"), manifest.get("board_uid")):
                gaps.append({"code": "identity_mismatch"})
            key = source_key(rec)
            if rec["seq"] != cuts.get(key, 0) + 1:
                gaps.append({"code": "sequence_gap"})
            cuts[key] = rec["seq"]
            if rec["clock_error_ns"] is None or rec["mono_ns"] is None:
                gaps.append({"code": "clock_unknown"})
            payload = rec["payload"]
            if rec["kind"] == "loss" or payload.get("unknown_fields") or payload.get("redacted"):
                gaps.append({"code": "record_coverage_unknown"})
            records.append(rec)
    if {p.name for p in out.glob("segment-*.jsonl")} != set(names):
        gaps.append({"code": "unlisted_segment"})
    try:
        if cuts != source_map(manifest.get("sources", [])):
            gaps.append({"code": "source_tail"})
    except (ValueError, TypeError, KeyError):
        gaps.append({"code": "bad_source_cuts"})
    if not cuts:
        gaps.append({"code": "source_tail"})
    return {"manifest": manifest, "records": records, "gaps": dedupe_gaps(gaps)}
