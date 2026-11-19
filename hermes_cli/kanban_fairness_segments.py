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
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def receipts(out: Path) -> dict:
    path = out / RECEIPTS
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8-sig"))
    return {"version": VERSION, "capture_id": None, "board_uid": None, "segments": [],
            "sources": [], "gaps": [], "bytes": 0}


def source_key(record):
    return record["source_gateway"], record["source_epoch"]


def source_map(sources):
    result = {}
    for item in sources:
        if set(item) != {"source_gateway", "source_epoch", "last_seq"}:
            raise BadRecord("invalid source closing cut")
        key = source_key(item)
        if any(not isinstance(x, str) or not x for x in key) or key in result:
            raise BadRecord("invalid or duplicate source identity")
        seq = item["last_seq"]
        if type(seq) is not int or seq < 0:
            raise BadRecord("invalid source closing sequence")
        result[key] = seq
    return result


def source_rows(cuts):
    return [{"source_gateway": g, "source_epoch": e, "last_seq": n} for (g, e), n in sorted(cuts.items())]


def append_records(out: Path, records, *, max_bytes: int = MAX_CAPTURE_BYTES) -> dict:
    """Append one bounded batch. Caller counts rejection as loss; one consumer owns the directory."""
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_CAPTURE_BYTES:
        raise ValueError("max_bytes outside capture bound")
    if (out / MANIFEST).exists():
        raise ValueError("capture is already sealed")
    state = receipts(out)
    cuts = source_map(state["sources"])
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
    for name, batch in zip(names, batches):
        data = b"".join(batch)
        write(out / name, data)
        state["segments"].append({"name": name, "records": len(batch), "bytes": len(data),
                                  "sha256": hashlib.sha256(data).hexdigest()})
    state.update(sources=source_rows(cuts), bytes=total)
    write(out / RECEIPTS, encoded(state))
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
    manifest = {**state, "sealed": True, "gaps": state["gaps"] + (gaps or [])}
    manifest["final_seal"] = final_seal(manifest)
    write(out / MANIFEST, encoded(manifest))
    return manifest


def _read_segment(out, seg, gaps):
    name = seg.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"segment-\d{6}\.jsonl", name):
        gaps.append({"code": "bad_segment_name"})
        return []
    try:
        data = (out / name).read_bytes()
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
        manifest = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else receipts(out)
    except (OSError, ValueError, TypeError):
        return {"manifest": {}, "records": [], "gaps": [{"code": "bad_manifest"}]}
    if manifest.get("sealed") is not True or not manifest.get("final_seal"):
        gaps.append({"code": "unsealed"})
    elif manifest["final_seal"] != final_seal(manifest):
        gaps.append({"code": "seal_mismatch"})
    gaps.extend(manifest.get("gaps") or [])
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
            if rec["clock_error_ns"] is None:
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
    return {"manifest": manifest, "records": records, "gaps": gaps}
