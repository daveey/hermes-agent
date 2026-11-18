"""Explicit one-task correction of a reserved idempotency key on a card the Board never created. [daveey]

The Board fences ``pickup-stall:`` / ``worker-resources-unknown:`` keys to the pickup-stall detector. An ordinary
local card that carries such a key was refused as a whole (``pickup_stall_provenance``), every child op behind it
was refused ``not_found``, and the rejected ops stay visible forever: the conservative resolver
(``kanban_sync_rejections``) cannot retire them because the Board never held the card. New creates are now refused
(``kanban_db_creation.reject_reserved_key``); this module repairs the cards that already exist.

Contract (``hermes kanban sync reserved-key TASK_ID --new-key KEY [--apply --expect TOKEN]``)

* **Preview is the default and writes nothing.** ``--reason TEXT`` (non-blank) is required for preview and apply. It
  prints a ``token`` (a digest of the exact task, child rows, outbox rows, tombstones, replacement key, reason, board
  and a nonsecret endpoint+gateway fingerprint). ``--apply`` needs that token (compare-and-set). The hub's enrolled
  gateway must be present and equal this file's sync gateway, and the file's board id must equal the board.
* **Eligible card only**: a local, non-detector card with ``created_on`` and ``sync_version`` NULL whose key is
  reserved and whose ops on the Board were rejected; no run, claim, lease or worker pid; every outbox row of the card
  and its child rows is a ``rejected`` upsert/import for an included identity (no pending op, tombstone, release,
  delete, unaccounted rejection, outgoing link or event older than the service retention: those refuse instead of
  being dropped). The replacement key is non-empty, unreserved, unused locally and differs from the old one.
* **Fresh authenticated Board reads**: ``Hub.row_evidence`` for the card and *every* child identity must be
  ``never_existed`` at floor 0 with no row and no tombstone, and an exhaustive paged ``Hub.snapshot_page('tasks')``
  must show no live Board card with the replacement key. Moving page heads require contiguous authenticated changes
  through the final page's head; any matching key in that interval vetoes, even if later removed. Incomplete,
  malformed or failed reads refuse. This proves absence at that cut, not an atomic reservation. Reads run
  with no write transaction open; the whole interval from the first read to the in-transaction veto, writer-lock wait
  included, is bounded on the monotonic clock (``EVIDENCE_MAX_AGE_S``). The exact local inputs are re-read and
  compared inside the write transaction that applies.
* **Apply** (one transaction): inserts the receipt (reason, endpoint fingerprint) and a byte copy of every rejected op it supersedes, rewrites only
  ``tasks.idempotency_key`` (capture triggers are suppressed, so no partial trigger op is minted) and enqueues one
  ordinary full ``import`` op for the corrected card. Nothing else changes: rejected ops are not deleted, reset or
  edited, and no historical event payload is rewritten (no event is appended either).
* **Children follow the accepted parent.** ``advance`` (run by every sync round and by a repeated apply) enqueues the
  child rows as normal ``upsert`` ops only once the parent op left the outbox, the local card carries the Board's
  version and fresh evidence shows it live under the corrected key created on this gateway; it records the
  replacement op ids (state ``children_queued``). The receipt becomes ``complete`` only when every recorded child op
  left the outbox (none rejected) and the Board holds each child row live. Each step re-validates identity and
  endpoint, is idempotent, and commits with the ops it describes.
* **The old rejections retire only through the receipt.** ``kanban_sync_rejections._classify`` stays conservative; a
  ``complete`` receipt that names an op (bytes unchanged, task bound, corrected parent live on the Board) retires the
  original *task* op only if every other rejected wire field equals the Board's and the local card, with the key
  compared as the replacement key; child ops then go through the unchanged field-equality classifier. A receipt that
  names an op but cannot vouch for it leaves it unresolved (no fallback to the conservative rules).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from urllib.parse import urlsplit
from typing import Any, Optional

from hermes_cli import kanban_db_sync as sync
from hermes_cli.kanban_db_connect import write_txn
from hermes_cli.kanban_pickup_alarm_identity import DETECTOR, reserved

MAX_KEY_LEN = 200
MAX_CHILDREN = 2000
MAX_SNAPSHOT_PAGES = 500
EVIDENCE_MAX_AGE_S = 30.0  # monotonic bound from the first Board read to the transactional veto (lock wait included)
_mono = time.monotonic
_CHILD_TABLES = ("task_runs", "task_comments", "task_events", "task_attachments", "task_chat_links")
_ORDER = ("task_links", "task_runs", "task_comments", "task_events", "task_attachments", "task_chat_links")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kanban_sync_key_supersessions (
    receipt_id   TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL UNIQUE,
    board        TEXT NOT NULL,
    gateway_id   TEXT NOT NULL,
    old_key      TEXT NOT NULL,
    new_key      TEXT NOT NULL,
    plan_token   TEXT NOT NULL,
    reason       TEXT NOT NULL,
    endpoint     TEXT NOT NULL,
    parent_op_id TEXT NOT NULL,
    children     TEXT NOT NULL,
    state        TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    completed_at INTEGER
);
CREATE TABLE IF NOT EXISTS kanban_sync_key_supersession_ops (
    old_op_id  TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL,
    table_name TEXT NOT NULL,
    row_uid    TEXT NOT NULL,
    op_json    TEXT NOT NULL,
    op_sha256  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kanban_sync_key_supersession_children (
    op_id      TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL,
    table_name TEXT NOT NULL,
    row_uid    TEXT NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    for stmt in SCHEMA_SQL.split(";"):
        if stmt.strip():
            conn.execute(stmt)


def _has_tables(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN "
                        "('kanban_sync_key_supersessions','kanban_sync_key_supersession_ops',"
                        "'kanban_sync_key_supersession_children')").fetchone()[0] == 3


def _default(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"$blob": value.hex()}
    raise TypeError(type(value).__name__)


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_default)


def _sha(obj: Any) -> str:
    return hashlib.sha256(_canon(obj).encode()).hexdigest()


class _Veto(Exception):
    """A transactional revalidation failed; nothing was written."""


def _fresh(started: Any) -> bool:
    """The whole evidence interval (first Board read .. now, writer-lock wait included) is on the monotonic clock."""
    if not isinstance(started, (int, float)):
        return False
    return 0 <= _mono() - started <= EVIDENCE_MAX_AGE_S


# --- identity ---------------------------------------------------------------------------------------

def _hub_gateway(hub: Any) -> Optional[str]:
    gateway = getattr(getattr(hub, "ident", None), "gateway", None)
    return gateway if isinstance(gateway, str) and gateway.strip() else None


def _endpoint(hub: Any) -> str:
    """Nonsecret fingerprint of the enrolled Board endpoint and gateway name (never the key)."""
    ident = getattr(hub, "ident", None)
    parts = urlsplit(str(getattr(ident, "url", "") or ""))
    where = f"{parts.scheme}://{parts.hostname or ''}:{parts.port or ''}{parts.path.rstrip('/')}"
    return _sha({"endpoint": where, "gateway": _hub_gateway(hub)})


def _identity_refusal(conn: sqlite3.Connection, hub: Any, board: str) -> Optional[str]:
    """The hub's enrolled gateway, this file's sync identity and the requested board must all be one identity."""
    state = sync.sync_state(conn)
    if sync.board_mode(conn) == "off" or not state:
        return "sync_off"
    gateway = _hub_gateway(hub)
    if gateway is None or state.get("gateway_id") != gateway:
        return "foreign_gateway_identity"
    if state.get("board_id") != board:
        return "foreign_board_identity"
    return None


# --- local inputs -----------------------------------------------------------------------------------

def _row_uid(table: str, row: dict) -> str:
    return f"{row['parent_id']}>{row['child_id']}" if table == "task_links" else row["uid"]


def _snapshot(conn: sqlite3.Connection, task_id: str) -> dict:
    """Every local input the correction depends on, as plain data (compared whole, before and inside the write)."""
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    snap: dict[str, Any] = {"task": dict(task) if task else None, "children": {}, "outbox": [], "tombstones": [],
                            "outgoing_links": []}
    if task is None:
        return snap
    snap["outgoing_links"] = [dict(r) for r in conn.execute(
        "SELECT * FROM task_links WHERE parent_id=? ORDER BY child_id", (task_id,))]
    snap["children"]["task_links"] = [dict(r) for r in conn.execute(
        "SELECT * FROM task_links WHERE child_id=? ORDER BY parent_id", (task_id,))]
    for table in _CHILD_TABLES:
        snap["children"][table] = [dict(r) for r in conn.execute(f"SELECT * FROM {table} WHERE task_id=? ORDER BY id",
                                                                 (task_id,))]
    uids = {("tasks", task_id)} | {(t, _row_uid(t, r)) for t, rows in snap["children"].items() for r in rows
                                   if t == "task_links" or r.get("uid")}
    rows = {tuple(r) for r in conn.execute(
        "SELECT op_id FROM kanban_sync_outbox WHERE row_uid=? OR json_extract(fields,'$.task_id')=? "
        "OR substr(row_uid, -(length(?)+1))='>'||? OR substr(row_uid, 1, length(?)+1)=?||'>'",
        (task_id, task_id, task_id, task_id, task_id, task_id))}
    uid_list = sorted(u for _t, u in uids)
    for i in range(0, len(uid_list), 500):
        part = uid_list[i:i + 500]
        rows |= {tuple(r) for r in conn.execute(
            f"SELECT op_id FROM kanban_sync_outbox WHERE row_uid IN ({','.join('?' * len(part))})", part)}
        snap["tombstones"] += [dict(r) for r in conn.execute(
            f"SELECT * FROM kanban_sync_tombstones WHERE row_uid IN ({','.join('?' * len(part))}) "
            "ORDER BY table_name, row_uid", part)]
    for (op_id,) in sorted(rows):
        snap["outbox"].append(dict(conn.execute("SELECT * FROM kanban_sync_outbox WHERE op_id=?", (op_id,)).fetchone()))
    snap["outbox"].sort(key=lambda r: r["op_id"])
    return snap


def _identities(snap: dict) -> list[tuple[str, str]]:
    """(table, uid) of the card and every child row that travels with it (all events included: none is skipped)."""
    out = [("tasks", snap["task"]["id"])]
    for table in _ORDER:
        out += [(table, _row_uid(table, row)) for row in snap["children"].get(table, [])]
    return out


def _unsupported(snap: dict) -> Optional[str]:
    """Shapes the correction cannot carry over whole: refuse them rather than silently drop them."""
    from hermes_cli.kanban_sync_client import EVENT_IMPORT_WINDOW_S
    cutoff = int(time.time()) - EVENT_IMPORT_WINDOW_S
    for row in snap["children"].get("task_events", []):
        if (row.get("created_at") or 0) < cutoff:
            return f"unsupported_old_event: {row.get('uid')}"  # the service would not keep it: it cannot be re-sent
    if snap["outgoing_links"]:
        return f"unsupported_outgoing_link: {snap['outgoing_links'][0]['child_id']}"  # belongs to another card
    included = set(_identities(snap))
    for op in snap["outbox"]:
        if op["op"] not in ("upsert", "import"):
            return f"unsupported_outbox_op: {op['op']} {op['table_name']} {op['row_uid']}"
        if (op["table_name"], op["row_uid"]) not in included:
            return f"unaccounted_rejection: {op['table_name']} {op['row_uid']}"
    return None


def _refusal(conn: sqlite3.Connection, hub: Any, board: str, snap: dict, new_key: Any) -> Optional[str]:
    """Local eligibility; the first failing rule, as a short code."""
    task = snap["task"]
    if task is None:
        return "no_such_task"
    why = _identity_refusal(conn, hub, board)
    if why:
        return why
    if task["created_on"] is not None or task["sync_version"] is not None:
        return "not_locally_owned"  # the Board stamped or acknowledged it: another process owns the identity
    if task["created_by"] == DETECTOR:
        return "detector_card"
    old = task["idempotency_key"]
    if not reserved(old):
        return "key_not_reserved"
    if (task["status"] == "running" or any(task[c] is not None for c in (
            "claim_lock", "claim_expires", "worker_pid", "worker_started_at", "lease_holder", "lease_expires",
            "lease_version", "current_run_id"))
            or any(r["ended_at"] is None for r in snap["children"]["task_runs"])):
        return "active_run_or_lease"
    if not isinstance(new_key, str) or not new_key or new_key != new_key.strip() or len(new_key) > MAX_KEY_LEN:
        return "bad_replacement_key"
    if reserved(new_key) or new_key == old:
        return "bad_replacement_key"
    if conn.execute("SELECT 1 FROM tasks WHERE idempotency_key=? AND id!=?", (new_key, task["id"])).fetchone():
        return "replacement_key_in_use"
    if sum(len(r) for r in snap["children"].values()) > MAX_CHILDREN:
        return "too_many_children"
    if any(t != "task_links" and not r.get("uid") for t, rows in snap["children"].items() for r in rows):
        return "child_without_uid"
    if snap["tombstones"]:
        return "local_tombstone"
    if any(op["state"] != "rejected" for op in snap["outbox"]):
        return "pending_outbox"
    why = _unsupported(snap)
    if why:
        return why
    if not any(op["table_name"] == "tasks" and op["row_uid"] == task["id"]
               and json.loads(op["fields"] or "{}").get("idempotency_key") == old for op in snap["outbox"]):
        return "no_rejected_op_carrying_the_key"
    return None


def _never_existed(e: dict) -> bool:
    return (e.get("state") == "never_existed" and e.get("floor") == 0 and e.get("row") is None
            and e.get("tombstone") is None)


def _token(board: str, hub: Any, new_key: str, reason: str, snap: dict) -> str:
    return _sha({"v": 2, "board": board, "endpoint": _endpoint(hub), "new_key": new_key, "reason": reason,
                 "snapshot": snap})


def _clean_reason(reason: Any) -> Optional[str]:
    return reason.strip() if isinstance(reason, str) and reason.strip() else None


def _remote_key_refusal(hub: Any, board: str, new_key: str) -> Optional[str]:
    """Authenticated, exhaustive ``hub.snapshot_page`` scan of every live Board card for a duplicate replacement key."""
    from hermes_cli.kanban_sync_client import HubError, SyncUnavailable
    incomplete = "authoritative_snapshot_incomplete"
    after = ""
    snapshot_head = None
    last_head = -1
    for _ in range(MAX_SNAPSHOT_PAGES):
        try:
            page = hub.snapshot_page(board, "tasks", after)
        except (HubError, SyncUnavailable, OSError, AttributeError, ValueError) as exc:
            return f"authoritative_snapshot_unavailable: {exc}"
        if (not isinstance(page, dict) or not isinstance(page.get("rows"), list) or type(page.get("head")) is not int
                or not isinstance(page.get("more"), bool) or not all(isinstance(r, dict) for r in page["rows"])):
            return incomplete + ": malformed page"
        if page.get("table") != "tasks":
            return incomplete + ": table identity mismatch"
        if snapshot_head is None:
            snapshot_head = page["head"]
        if page["head"] < max(0, last_head):
            return incomplete + ": snapshot head regressed"
        last_head = page["head"]
        if any(r.get("idempotency_key") == new_key for r in page["rows"]):
            return "replacement_key_in_remote_use"
        if not page["more"]:
            if page["head"] == snapshot_head:
                return None
            from hermes_cli.kanban_sync_key_evidence import cover_changes
            return cover_changes(hub, board, new_key, snapshot_head, page["head"])
        nxt = page.get("next")
        if not isinstance(nxt, str) or nxt <= after:
            return incomplete + ": page cursor does not advance"
        after = nxt
    return incomplete + ": page cap reached"


# --- receipts ----------------------------------------------------------------------------------------

def receipt_for_task(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    if not _has_tables(conn):
        return None
    row = conn.execute("SELECT * FROM kanban_sync_key_supersessions WHERE task_id=?", (task_id,)).fetchone()
    return dict(row) if row else None


def receipt_for_op(conn: sqlite3.Connection, op_id: str) -> Optional[dict]:
    """The receipt (with this op's recorded bytes) that names ``op_id``, or None."""
    if not _has_tables(conn):
        return None
    row = conn.execute(
        "SELECT r.*, o.op_sha256, o.table_name AS op_table, o.row_uid AS op_row_uid, EXISTS(SELECT 1 FROM "
        "kanban_sync_key_supersession_ops k WHERE k.receipt_id=r.receipt_id AND k.table_name='tasks' AND "
        "json_extract(json_extract(k.op_json,'$.fields'),'$.idempotency_key')=r.old_key) AS old_key_recorded, "
        "r.gateway_id=(SELECT gateway_id FROM kanban_sync_state LIMIT 1) AS gateway_bound "
        "FROM kanban_sync_key_supersession_ops o "
        "JOIN kanban_sync_key_supersessions r ON r.receipt_id=o.receipt_id WHERE o.old_op_id=?", (op_id,)).fetchone()
    return dict(row) if row else None


def _receipt_flaw(op: dict, before: dict, evidence: dict, receipt: dict) -> Optional[str]:
    """Why a receipt that names this op cannot vouch for it (the caller then never falls back to the old rules)."""
    table, uid = op["table_name"], op["row_uid"]
    if receipt["state"] != "complete":
        return "key-supersession receipt is not complete"
    if _sha(op) != receipt["op_sha256"] or (table, uid) != (receipt["op_table"], receipt["op_row_uid"]):
        return "rejected op differs from the receipt's recorded bytes"
    if evidence.get("state") != "live" or (evidence.get("row") or {}).get("uid") != uid:
        return "Board does not hold this row live"
    task_id = receipt["task_id"]
    owner = uid.partition(">")[2] if table == "task_links" else before.get("task") if table != "tasks" else uid
    if owner != task_id:
        return "rejected op is not bound to the receipt's task"
    parent = before.get("parent") or {}
    if parent.get("idempotency_key") != receipt["new_key"] or parent.get("id") != task_id:
        return "local card does not carry the corrected key"
    if not receipt["gateway_bound"] or not receipt["old_key_recorded"]:
        return "receipt gateway or old key is not the one the Board and the rejected ops carry"
    if table != "tasks" and [table, uid] not in json.loads(receipt["children"]):
        return "row is not one of the receipt's children"
    return None


def _task_proof(op: dict, before: dict, evidence: dict, receipt: dict) -> Optional[str]:
    """The original task op retires only when the Board's card is the corrected one and every *other* wire field agrees."""
    from hermes_cli.kanban_sync_client import _wire_fields
    remote, local = evidence["row"], before.get("row") or {}
    sent = _wire_fields("tasks", json.loads(op["fields"] or "{}"))
    if sent.get("idempotency_key", receipt["old_key"]) != receipt["old_key"]:
        return "rejected op carries a key other than the receipt's old key"
    current = before.get("fields") or {}
    if (remote.get("idempotency_key") != receipt["new_key"] or current.get("idempotency_key") != receipt["new_key"]
            or local.get("idempotency_key") != receipt["new_key"]
            or remote.get("created_on") != receipt["gateway_id"]):
        return "Board card is not the corrected card created by this gateway"
    version = remote.get("version")
    if not isinstance(version, int) or version < (op.get("base_version") or 0) or local.get("sync_version") != version:
        return "local card has not settled at the Board's version"
    expected = {**sent, "idempotency_key": receipt["new_key"]}
    if not all(k in current and k in remote and current[k] == remote[k] for k in expected):
        return "Board card differs from the local card in a rejected wire field"
    return None


def _parent_flaw(receipt: dict, parent_evidence: Optional[dict]) -> Optional[str]:
    """A child op is vouched for only while the Board holds the corrected card, created by this gateway."""
    remote = (parent_evidence or {}).get("row") or {}
    if not (parent_evidence and parent_evidence.get("state") == "live" and remote.get("uid") == receipt["task_id"]
            and remote.get("idempotency_key") == receipt["new_key"]
            and remote.get("created_on") == receipt["gateway_id"]):
        return "Board does not hold the corrected card created by this gateway"
    return None


def receipt_resolution(op: dict, before: dict, evidence: dict,
                       parent_evidence: Optional[dict] = None) -> Optional[tuple[str, str]]:
    """How a *complete* receipt treats this rejected op.

    ``None``: no receipt names it (the conservative rules apply). ``("unresolved", why)``: a receipt names it but cannot
    vouch for it; the caller must not fall back. ``("conservative", "")``: a child op the receipt vouches for; the
    caller's existing field-equality classifier decides. ``("superseded", proof)``: the original task op."""
    receipt = before.get("receipt")
    if not receipt:
        return None
    flaw = _receipt_flaw(op, before, evidence, receipt)
    if flaw:
        return "unresolved", f"receipt {receipt['receipt_id']}: {flaw}"
    flaw = (_parent_flaw(receipt, parent_evidence) if op["table_name"] != "tasks"
            else _task_proof(op, before, evidence, receipt))
    if flaw:
        return "unresolved", f"receipt {receipt['receipt_id']}: {flaw}"
    if op["table_name"] != "tasks":
        return "conservative", ""
    return "superseded", (f"key-supersession receipt {receipt['receipt_id']}: the Board holds the corrected "
                          "card and every rejected wire field matches")


# --- preview / apply ----------------------------------------------------------------------------------

def _report(task_id: str, new_key: Any, board: str) -> dict:
    return {"task_id": task_id, "board": board, "new_key": new_key, "eligible": False, "action": "preview"}


def _read_board(hub: Any, board: str, ids: list, new_key: str) -> Optional[str]:
    """Fresh Board reads: never-existed evidence for every identity, then replacement-key uniqueness."""
    from hermes_cli.kanban_sync_client import HubError, SyncUnavailable
    from hermes_cli.kanban_sync_rejections import _evidence
    try:
        for table, uid in ids:
            if not _never_existed(_evidence(hub, board, table, uid)):
                return f"board_history_not_proven_never_existed: {table} {uid}"
    except (HubError, SyncUnavailable, OSError, AttributeError, ValueError) as exc:
        return f"authoritative_evidence_unavailable: {exc}"
    return _remote_key_refusal(hub, board, new_key)


def plan(conn: sqlite3.Connection, hub: Any, board: str, task_id: str, new_key: str, reason: Any = None) -> dict:
    """Read-only preview: eligibility, fresh Board evidence for the card and every child, and the CAS token."""
    report = _report(task_id, new_key, board)
    clean = _clean_reason(reason)
    if clean is None:
        report["refusal"] = "reason_required"
        return report
    report["reason"] = clean
    existing = receipt_for_task(conn, task_id)
    if existing:
        report.update(action="already_applied" if existing["new_key"] == new_key else "refused",
                      receipt=existing, token=existing["plan_token"], old_key=existing["old_key"])
        if existing["new_key"] != new_key:
            report["refusal"] = "receipt_exists_other_key"
        return report
    snap = _snapshot(conn, task_id)
    why = _refusal(conn, hub, board, snap, new_key)
    if why:
        report["refusal"] = why
        return report
    started = _mono()
    ids = _identities(snap)
    why = _read_board(hub, board, ids, new_key)
    if why:
        report["refusal"] = why
        return report
    if not _fresh(started):
        report["refusal"] = "evidence_interval_exceeded"
        return report
    if _snapshot(conn, task_id) != snap:
        report["refusal"] = "local_state_changed_during_evidence_read"
        return report
    report.update(eligible=True, old_key=snap["task"]["idempotency_key"], token=_token(board, hub, new_key, clean, snap),
                  superseded_op_ids=[o["op_id"] for o in snap["outbox"]], children=[list(i) for i in ids[1:]],
                  evidence_checked=len(ids), evidence_started_monotonic=started)
    return report


def _write_receipt(conn: sqlite3.Connection, hub: Any, board: str, snap: dict, new_key: str, reason: str,
                   token: str, ids: list) -> None:
    task_id = snap["task"]["id"]
    ensure_schema(conn)
    receipt_id, parent_op = sync.uuid7(), sync.uuid7()
    old_key = snap["task"]["idempotency_key"]
    conn.execute("INSERT INTO kanban_sync_key_supersessions (receipt_id, task_id, board, gateway_id, old_key, new_key, "
                 "plan_token, reason, endpoint, parent_op_id, children, state, created_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?, 'parent_queued', ?)",
                 (receipt_id, task_id, board, _hub_gateway(hub), old_key, new_key, token, reason, _endpoint(hub),
                  parent_op, json.dumps([list(i) for i in ids[1:]]), int(time.time())))
    for op in snap["outbox"]:
        conn.execute("INSERT INTO kanban_sync_key_supersession_ops VALUES (?,?,?,?,?,?)",
                     (op["op_id"], receipt_id, op["table_name"], op["row_uid"], _canon(op), _sha(op)))
    previous = conn.execute("SELECT applying FROM kanban_sync_state LIMIT 1").fetchone()[0]
    conn.execute("UPDATE kanban_sync_state SET applying = 1")  # no trigger op: the import op below is the write
    try:
        changed = conn.execute(
            "UPDATE tasks SET idempotency_key=? WHERE id=? AND idempotency_key=? AND created_on IS NULL "
            "AND sync_version IS NULL", (new_key, task_id, old_key)).rowcount
    finally:
        conn.execute("UPDATE kanban_sync_state SET applying = ?", (previous,))
    if changed != 1:
        raise _Veto("task_changed_before_write")
    from hermes_cli import kanban_sync_client as ksc
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    conn.execute(
        "INSERT INTO kanban_sync_outbox (op_id, table_name, row_uid, op, fields, base_version, hlc, created_at) "
        "VALUES (?, 'tasks', ?, 'import', ?, NULL, '', ?)",
        (parent_op, task_id, json.dumps(ksc._row_fields(conn, "tasks", row)), time.time_ns() // 1_000_000))


def apply(conn: sqlite3.Connection, hub: Any, board: str, task_id: str, new_key: str, expected: Optional[str],
          reason: Any = None) -> dict:
    """Apply the previewed correction when ``expected`` is the preview's token; a repeat continues the receipt."""
    report = plan(conn, hub, board, task_id, new_key, reason)
    if report["action"] == "already_applied":
        receipt = report["receipt"]
        if (expected != receipt["plan_token"] or report["reason"] != receipt["reason"]
                or _identity_wait(conn, hub, board, receipt)):
            report.update(action="refused", refusal="stale_or_missing_expected_intent", eligible=False)
            return report
        report["advance"] = advance(conn, hub, board, task_id=task_id)
        report["receipt"] = receipt_for_task(conn, task_id)
        return report
    if not report["eligible"]:
        report["action"] = "refused"
        return report
    if not expected or expected != report["token"]:
        report.update(action="refused", refusal="stale_or_missing_expected_token", eligible=False)
        return report
    snap_ids = [tuple(i) for i in [("tasks", task_id), *report["children"]]]
    clean, started = report["reason"], report["evidence_started_monotonic"]
    try:
        with write_txn(conn):
            if not _fresh(started):
                raise _Veto("evidence_interval_exceeded")
            snap = _snapshot(conn, task_id)
            if _refusal(conn, hub, board, snap, new_key) or _token(board, hub, new_key, clean, snap) != expected:
                raise _Veto("local_state_changed_before_write")
            if [tuple(i) for i in _identities(snap)] != snap_ids:
                raise _Veto("child_set_changed_before_write")
            _write_receipt(conn, hub, board, snap, new_key, clean, expected, snap_ids)
    except _Veto as exc:
        report.update(action="refused", refusal=str(exc), eligible=False)
        return report
    report.update(action="applied", receipt=receipt_for_task(conn, task_id))
    return report


# --- advance -----------------------------------------------------------------------------------------

def _identity_wait(conn: sqlite3.Connection, hub: Any, board: str, receipt: dict) -> Optional[str]:
    """Why this hub / file may not continue the receipt (identity or endpoint no longer the one it was bound to)."""
    state = sync.sync_state(conn) or {}
    if (_hub_gateway(hub) != receipt["gateway_id"] or _endpoint(hub) != receipt["endpoint"]
            or state.get("gateway_id") != receipt["gateway_id"] or state.get("board_id") != receipt["board"]
            or receipt["board"] != board):
        return "identity or endpoint differs from the receipt's"
    return None


def _parent_wait(conn: sqlite3.Connection, hub: Any, board: str, receipt: dict) -> Optional[str]:
    """Why the corrected parent is not yet proven accepted (None when it is)."""
    from hermes_cli import kanban_sync_client as ksc
    from hermes_cli.kanban_sync_rejections import _evidence
    tid = receipt["task_id"]
    parent = conn.execute("SELECT state FROM kanban_sync_outbox WHERE op_id=?", (receipt["parent_op_id"],)).fetchone()
    task = conn.execute("SELECT idempotency_key, sync_version FROM tasks WHERE id=?", (tid,)).fetchone()
    if parent is not None:
        return f"parent op {parent[0]}"
    if task is None or task["sync_version"] is None or task["idempotency_key"] != receipt["new_key"]:
        return "parent not acknowledged locally"
    try:
        e = _evidence(hub, board, "tasks", tid)
    except (ksc.HubError, ksc.SyncUnavailable, OSError, AttributeError, ValueError) as exc:
        return f"authoritative evidence unavailable: {exc}"
    remote = e.get("row") or {}
    if not (e.get("state") == "live" and remote.get("uid") == tid and remote.get("idempotency_key") == receipt["new_key"]
            and remote.get("created_on") == receipt["gateway_id"]):
        return "Board has not accepted the corrected card"
    return None


def _receipt_state(conn: sqlite3.Connection, receipt: dict) -> Optional[str]:
    row = conn.execute("SELECT state FROM kanban_sync_key_supersessions WHERE receipt_id=?",
                       (receipt["receipt_id"],)).fetchone()
    return row[0] if row else None


def _queue_children(conn: sqlite3.Connection, receipt: dict) -> Optional[int]:
    """Inside the write transaction: enqueue the entire manifest, or leave it waiting unchanged."""
    from hermes_cli import kanban_sync_client as ksc
    from hermes_cli.kanban_sync_rejections import _local
    now, queued = time.time_ns() // 1_000_000, 0
    children = []
    for table, uid in json.loads(receipt["children"]):
        row = _local(conn, table, uid)
        if row is None or conn.execute("SELECT 1 FROM kanban_sync_tombstones WHERE table_name=? AND row_uid=?",
                                       (table, uid)).fetchone() or conn.execute(
                "SELECT 1 FROM kanban_sync_outbox WHERE table_name=? AND row_uid=? AND state='pending'",
                (table, uid)).fetchone():
            return None
        children.append((table, uid, row))
    for table, uid, row in children:
        op_id = sync.uuid7()
        conn.execute("INSERT INTO kanban_sync_outbox (op_id, table_name, row_uid, op, fields, base_version, hlc, "
                     "created_at) VALUES (?, ?, ?, 'upsert', ?, NULL, '', ?)",
                     (op_id, table, uid, json.dumps(ksc._row_fields(conn, table, row)), now))
        conn.execute("INSERT INTO kanban_sync_key_supersession_children VALUES (?,?,?,?)",
                     (op_id, receipt["receipt_id"], table, uid))
        queued += 1
    conn.execute("UPDATE kanban_sync_key_supersessions SET state='children_queued' WHERE receipt_id=?",
                 (receipt["receipt_id"],))
    return queued


def _queue_step(conn: sqlite3.Connection, hub: Any, board: str, receipt: dict, out: dict) -> None:
    started = _mono()
    wait = _parent_wait(conn, hub, board, receipt)
    if wait:
        out["waiting"] = wait
        return
    with write_txn(conn):
        if not _fresh(started):
            out["waiting"] = "evidence_interval_exceeded"
        elif _receipt_state(conn, receipt) != "parent_queued" or _identity_wait(conn, hub, board, receipt) or (
                conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE op_id=?", (receipt["parent_op_id"],)).fetchone()):
            out["waiting"] = "receipt or identity changed during evidence read"
        else:
            task = conn.execute("SELECT idempotency_key, sync_version FROM tasks WHERE id=?",
                                (receipt["task_id"],)).fetchone()
            if task is None or task["sync_version"] is None or task["idempotency_key"] != receipt["new_key"]:
                out["waiting"] = "receipt changed during evidence read"
            else:
                queued = _queue_children(conn, receipt)
                if queued is None:
                    out["waiting"] = "child manifest changed or has pending work"
                else:
                    out.update(advanced=True, children_queued=queued)


def _delivery_wait(conn: sqlite3.Connection, hub: Any, board: str, receipt: dict) -> Optional[str]:
    """Why a queued child is not yet proven delivered: still queued, rejected, or absent from the Board."""
    from hermes_cli import kanban_sync_client as ksc
    from hermes_cli.kanban_sync_rejections import _evidence
    kids = [dict(r) for r in conn.execute(
        "SELECT * FROM kanban_sync_key_supersession_children WHERE receipt_id=? ORDER BY op_id", (receipt["receipt_id"],))]
    states = {kid["op_id"]: conn.execute("SELECT state FROM kanban_sync_outbox WHERE op_id=?",
                                         (kid["op_id"],)).fetchone() for kid in kids}
    for op_id, row in sorted(states.items()):
        if row is not None and row[0] == "rejected":
            return f"child op {op_id} rejected"
    for op_id, row in sorted(states.items()):
        if row is not None:
            return f"child op {op_id} still queued ({row[0]})"
    for kid in kids:
        try:
            e = _evidence(hub, board, kid["table_name"], kid["row_uid"])
        except (ksc.HubError, ksc.SyncUnavailable, OSError, AttributeError, ValueError) as exc:
            return f"authoritative evidence unavailable: {exc}"
        if e.get("state") != "live" or (e.get("row") or {}).get("uid") != kid["row_uid"]:
            return f"child {kid['table_name']} {kid['row_uid']} not delivered: the Board holds no live row"
    return None


def _finish_step(conn: sqlite3.Connection, hub: Any, board: str, receipt: dict, out: dict) -> None:
    started = _mono()
    wait = _delivery_wait(conn, hub, board, receipt)
    if wait:
        out["waiting"] = wait
        return
    with write_txn(conn):
        if not _fresh(started):
            out["waiting"] = "evidence_interval_exceeded"
        elif _receipt_state(conn, receipt) != "children_queued" or _identity_wait(conn, hub, board, receipt) or conn.execute(
                "SELECT 1 FROM kanban_sync_outbox o JOIN kanban_sync_key_supersession_children c ON c.op_id=o.op_id "
                "WHERE c.receipt_id=?", (receipt["receipt_id"],)).fetchone():
            out["waiting"] = "receipt, identity or child ops changed during evidence read"
        else:
            conn.execute("UPDATE kanban_sync_key_supersessions SET state='complete', completed_at=? WHERE receipt_id=?",
                         (int(time.time()), receipt["receipt_id"]))
            out.update(advanced=True, completed=True)


def is_replacement(conn: sqlite3.Connection, op_id: str) -> bool:
    """A recovery requires its complete approved payload; an older Board cannot silently downgrade it."""
    if not _has_tables(conn):
        return False
    return conn.execute("SELECT 1 FROM kanban_sync_key_supersessions WHERE parent_op_id=? UNION ALL "
                        "SELECT 1 FROM kanban_sync_key_supersession_children WHERE op_id=?", (op_id, op_id)).fetchone() is not None


def replacement_children(conn: sqlite3.Connection) -> set[str]:
    """Recovery child ops never authorize synthesis of another task (including a deleted original parent)."""
    if not _has_tables(conn):
        return set()
    return {r[0] for r in conn.execute("SELECT op_id FROM kanban_sync_key_supersession_children")}


def remap(conn: sqlite3.Connection, old_id: str, new_id: str) -> None:
    """Keep replacement custody when normal transient retry rotates an outbox id, in its writer transaction."""
    if not _has_tables(conn):
        return
    conn.execute("UPDATE kanban_sync_key_supersessions SET parent_op_id=? WHERE parent_op_id=?",
                 (new_id, old_id))
    conn.execute("UPDATE kanban_sync_key_supersession_children SET op_id=? WHERE op_id=?", (new_id, old_id))


def advance(conn: sqlite3.Connection, hub: Any, board: str, *, task_id: Optional[str] = None) -> list[dict]:
    """Step every open receipt: parent accepted -> children queued -> children delivered (``complete``). Idempotent."""
    if not _has_tables(conn):
        return []
    where, args = ("WHERE state IN ('parent_queued','children_queued') AND board=?", [board])
    if task_id:
        where, args = where + " AND task_id=?", args + [task_id]
    reports = []
    for receipt in [dict(r) for r in conn.execute(f"SELECT * FROM kanban_sync_key_supersessions {where}", args)]:
        out = {"task_id": receipt["task_id"], "receipt_id": receipt["receipt_id"], "advanced": False}
        reports.append(out)
        wait = _identity_wait(conn, hub, board, receipt)
        if wait:
            out["waiting"] = wait
        elif receipt["state"] == "parent_queued":
            _queue_step(conn, hub, board, receipt, out)
        else:
            _finish_step(conn, hub, board, receipt, out)
    return reports
