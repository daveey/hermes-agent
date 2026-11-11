"""Read the canonical completed-run receipt without re-admitting historical facts.

A newer review/blocked run is not a completion. A legacy latest completion has
unspecified artifact acceptance, even if an older run carried a receipt.
"""
from __future__ import annotations

import json
import math
import re

from hermes_completion_receipt import validate_completion_receipt

MAX_SOURCE_UID = 256
MAX_RELOCATIONS = 32
MAX_SOURCE_TEXT = 2048
MAX_SOURCE_BYTES = 16384
_SOURCE_REF = re.compile(r"@task:[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+/[^\s/@]+\Z")


def validate_receipt_source(value, receipt, *, expected_ref=None):
    """Normalize bounded task-run provenance, not its truth or current acceptance.

    Receipt must already be canonical. Outer qualification/attestation remains
    the caller's job; expected_ref binds the source to that qualified record.
    """
    required = {'kind', 'ref', 'event_id', 'run_id', 'completed_at'}
    optional = {'event_uid', 'run_uid', 'attached_from_workspace'}
    if (not isinstance(value, dict) or not required <= value.keys()
            or value.keys() - required - optional):
        raise ValueError('completion_receipt_source: missing or unknown fields')

    def text(raw, limit):
        if (not isinstance(raw, str) or not raw.strip() or len(raw) > limit
                or any(ord(c) < 32 for c in raw)):
            raise ValueError('completion_receipt_source: invalid or oversized text')
        return raw

    ref = text(value['ref'], MAX_SOURCE_TEXT)
    if (value['kind'] != 'task_run' or not _SOURCE_REF.fullmatch(ref)
            or (expected_ref is not None and ref != expected_ref)):
        raise ValueError('completion_receipt_source: wrong task source')
    out: dict = {'kind': 'task_run', 'ref': ref}
    for key in ('event_id', 'run_id'):
        if type(value[key]) is not int or not 0 < value[key] <= 2**63 - 1:
            raise ValueError('completion_receipt_source: invalid store-local ID')
        out[key] = value[key]
    for key in ('event_uid', 'run_uid'):
        if key in value:
            out[key] = None if value[key] is None else text(value[key], MAX_SOURCE_UID)
    stamp = value['completed_at']
    if (type(stamp) not in (int, float) or stamp < 0 or stamp > 1e308
            or not math.isfinite(stamp)):
        raise ValueError('completion_receipt_source: invalid completion timestamp')
    out['completed_at'] = stamp
    if 'attached_from_workspace' in value:
        relocations = value['attached_from_workspace']
        refs = {e['ref'] for item in receipt['items'] for e in item['evidence']}
        refs.update(item['superseded_by'] for item in receipt['items'] if 'superseded_by' in item)
        if (not isinstance(relocations, dict) or len(relocations) > MAX_RELOCATIONS
                or any(ref not in refs for ref in relocations)):
            raise ValueError('completion_receipt_source: invalid relocation references')
        out['attached_from_workspace'] = {
            text(ref, MAX_SOURCE_TEXT): text(dest, MAX_SOURCE_TEXT)
            for ref, dest in relocations.items()}
    if len(json.dumps(out, ensure_ascii=True)) > MAX_SOURCE_BYTES:
        raise ValueError('completion_receipt_source: provenance exceeds budget')
    return {key: out[key] for key in ('kind', 'ref', 'event_id', 'event_uid',
                                     'run_id', 'run_uid', 'completed_at',
                                     'attached_from_workspace') if key in out}


def read_completed(conn, task_id, *, gateway, board):
    """Return receipt plus source-qualified provenance, or None (legacy/invalid).

    Caller supplies the authoritative source namespace, not the observing host.
    Source event/run UIDs survive replica integer remapping; integer fallbacks
    are explicitly store-local. This is a projection, never a second writer.
    """
    if not gateway or not board:
        raise ValueError("completion_receipt: projection requires the source gateway and board")
    run_columns = {r[1] for r in conn.execute("PRAGMA table_info(task_runs)")}
    run_uid = "r.uid" if "uid" in run_columns else "NULL"
    row = conn.execute(
        f"SELECT e.*, r.metadata AS receipt_metadata, r.outcome AS receipt_outcome, "
        f"{run_uid} AS receipt_run_uid FROM task_events e "
        "LEFT JOIN task_runs r ON r.id=e.run_id AND r.task_id=e.task_id "
        "WHERE e.task_id=? AND e.kind='completed' ORDER BY e.created_at DESC,e.id DESC LIMIT 1",
        (task_id,)).fetchone()
    if row is None or row["receipt_outcome"] != "completed":
        return None
    try:
        event = json.loads((row["payload"] if "payload" in row.keys() else None) or "{}")
        if not isinstance(event, dict) or type(event.get("completion_receipt_admission")) is not int or event["completion_receipt_admission"] != 1:
            return None  # older writers may have stored arbitrary unadmitted metadata
        metadata = json.loads(row["receipt_metadata"] or "{}")
        if not isinstance(metadata, dict) or "completion_receipt" not in metadata:
            return None
        receipt = validate_completion_receipt(metadata["completion_receipt"])
    except (ValueError, TypeError):
        return None
    event_uid = row["uid"] if "uid" in row.keys() else None
    source = {
        "kind": "task_run", "ref": f"@task:{board}@{gateway}/{task_id}",
        "event_id": row["id"], "event_uid": event_uid,
        "run_id": row["run_id"], "run_uid": row["receipt_run_uid"],
        "completed_at": row["created_at"],
    }
    relocations = metadata.get("attached_from_workspace")
    if isinstance(relocations, dict):
        # Evidence keeps its observed identity. Carry the existing relocation
        # provenance alongside it, not as a new evidence observation.
        refs = {e["ref"] for item in receipt["items"] for e in item["evidence"]}
        refs.update(item["superseded_by"] for item in receipt["items"] if "superseded_by" in item)
        source["attached_from_workspace"] = {ref: dest for ref, dest in relocations.items()
                                             if ref in refs and isinstance(dest, str)}
    try:
        source = validate_receipt_source(source, receipt,
                                         expected_ref=f'@task:{board}@{gateway}/{task_id}')
    except (ValueError, TypeError):
        return None
    return {"completion_receipt": receipt, "completion_receipt_source": source}


def remote_fields(task, *, gateway, profile, board, task_id):
    """Transport admitted historical facts, never a fresh completion verdict.

    The enclosing owner observation must bind these exact bytes. Qualification
    comes from the record/binding, not a receipt's claimed ref or observer host.
    Callers retain their existing current/released/stale gates independently.
    """
    from hermes_cli.kanban_origin_ledger import _owner_evidence

    source = task.get('completion_receipt_source')
    if not isinstance(source, dict):
        return {}
    if (not profile or any(task.get(k) != v for k, v in (
            ('gateway', gateway), ('profile', profile), ('board', board), ('id', task_id)))
            or not _owner_evidence(task)):
        return {}
    try:
        receipt = validate_completion_receipt(task.get('completion_receipt'))
        source = validate_receipt_source(source, receipt,
                                         expected_ref=f'@task:{board}@{gateway}/{task_id}')
    except (ValueError, TypeError):
        return {}
    return {'completion_receipt': receipt, 'completion_receipt_source': source}


def read_inventory(conn, task_id, *, board, gateway=None):
    """Optional projection for existing inventories, including older board schemas.

    A synced task's created_on is its authority even when another worker picked
    it up. Unsynced local boards use the configured gateway name. Unknown source
    or an old schema means no receipt, never fabricated acceptance.
    """
    import sqlite3
    from hermes_cli.kanban_origin import gateway_names

    try:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return None
        source = row["created_on"] if "created_on" in row.keys() else None
        source = source or gateway or gateway_names()[0]
        if not source:
            return None
        return read_completed(conn, task_id, gateway=source, board=board)
    except sqlite3.Error:
        return None
