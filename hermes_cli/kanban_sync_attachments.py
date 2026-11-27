"""Retryable attachment byte custody, independent of the row-feed cursor. [daveey]

Rows themselves are the durable work queue. Local retry receipts only schedule
bounded attempts; no transient apply hint is required after a restart. Only the
service assigns blob_key, after it has verified private object storage.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
import re
import tempfile
import time

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_attachment_names import _safe_attachment_name
from hermes_cli.kanban_db_connect import write_txn
from hermes_cli.kanban_home_transfer import frozen

_log = logging.getLogger(__name__)
_now = time.time
_KEY = re.compile(r"sha256:([0-9a-f]{64})\Z")
_COMPONENT = re.compile(r"[A-Za-z0-9_-]+\Z")


def _setup(conn):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='kanban_attachment_retry'").fetchone():
        return
    with write_txn(conn):
        conn.execute("CREATE TABLE IF NOT EXISTS kanban_attachment_retry ("
                     "uid TEXT PRIMARY KEY, blob_key TEXT, attempts INTEGER NOT NULL, "
                     "retry_at REAL NOT NULL, error TEXT)")


def _destination(row, board):
    if not _COMPONENT.fullmatch(row["task_id"]) or not _COMPONENT.fullmatch(row["uid"]):
        raise ValueError("invalid attachment identity")
    root = kb.attachments_root(board).resolve()
    path = root / row["task_id"] / row["uid"] / _safe_attachment_name(row["filename"])
    if not path.resolve().is_relative_to(root):
        raise ValueError("attachment destination escapes storage")
    return path


def _read(path, row, board):
    root = kb.attachments_root(board).resolve()
    if not path.resolve().is_relative_to(root):
        raise ValueError("attachment source escapes storage")
    with path.open("rb") as source:
        data = source.read(kb.KANBAN_ATTACHMENT_MAX_BYTES + 1)
    if len(data) > kb.KANBAN_ATTACHMENT_MAX_BYTES or len(data) != row["size"]:
        raise ValueError("attachment size mismatch")
    return data


def _valid(data, key):
    match = _KEY.fullmatch(key or "")
    if match is None or hashlib.sha256(data).hexdigest() != match[1]:
        raise ValueError("attachment digest mismatch")


def _publish_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".blob-", delete=False) as out:
            temporary = Path(out.name)
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        temporary.replace(path)
        if hasattr(os, "O_DIRECTORY"):
            for parent in (path.parent, *path.parent.parents):
                fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _record_local(conn, row, path, key, *, data=None):
    # stored_path is local-only. Suppress echo of the service's verified receipt.
    # A deleted/rebound row must not be resurrected after network I/O.
    # Read current metadata, not the pre-I/O snapshot: another writer may have
    # cleared the path while these bytes were being verified.
    if data is None:
        # A read-only verification must not reserve a writer. Any actual byte or
        # metadata publication still rechecks custody and identity under the lock.
        current = conn.execute("SELECT stored_path,blob_key FROM task_attachments WHERE uid=? "
                               "AND id=? AND task_id=? AND size=? AND filename=? AND created_at=? AND blob_key IS ?",
                               (row["uid"], row["id"], row["task_id"], row["size"], row["filename"],
                                row["created_at"], row["blob_key"])).fetchone()
        if frozen(conn, row["task_id"]):
            return False
        # A concurrent pull may already have applied the storage receipt. Changed
        # or deleted rows need no stale local write; they are not a custody refusal.
        if current is None:
            return True
        if current["stored_path"] == str(path) and current["blob_key"] == key:
            return True
    with write_txn(conn):
        if frozen(conn, row["task_id"]):
            return False
        current = conn.execute("SELECT stored_path,blob_key FROM task_attachments WHERE uid=? "
                               "AND id=? AND task_id=? AND size=? AND filename=? AND created_at=? AND blob_key IS ?",
                               (row["uid"], row["id"], row["task_id"], row["size"], row["filename"],
                                row["created_at"], row["blob_key"])).fetchone()
        if current is None:
            return True
        if data is not None:
            # Byte publication and local metadata share the custody reservation.
            # No network is performed under this writer lock.
            _publish_file(path, data)
        if current["stored_path"] == str(path) and current["blob_key"] == key:
            return True
        previous = conn.execute("SELECT applying FROM kanban_sync_state").fetchone()[0]
        conn.execute("UPDATE kanban_sync_state SET applying=1")
        try:
            conn.execute("UPDATE task_attachments SET stored_path=?,blob_key=? WHERE uid=? "
                         "AND id=? AND task_id=? AND size=? AND filename=? AND created_at=? AND blob_key IS ?",
                         (str(path), key, row["uid"], row["id"], row["task_id"], row["size"],
                          row["filename"], row["created_at"], row["blob_key"]))
        finally:
            conn.execute("UPDATE kanban_sync_state SET applying=?", (previous,))
    return True


def _transfer(conn, hub, board, row, *, expected_sha256=None):
    if frozen(conn, row["task_id"]):
        return False
    key = row["blob_key"]
    if expected_sha256 is not None and key and key != "sha256:" + expected_sha256:
        raise ValueError("attachment differs from selected digest")
    if not key:
        if not row["stored_path"]:
            return False  # metadata arrived first; only its custodian can supply bytes
        path = Path(row["stored_path"])
        data = _read(path, row, board)
        digest = hashlib.sha256(data).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise ValueError("upload bytes differ from selected digest")
        receipt = hub.put_attachment(board, row["uid"], digest, data)
        if (not isinstance(receipt, dict) or receipt.get("blob_key") != "sha256:" + digest
                or receipt.get("sha256") != digest or receipt.get("size") != len(data)):
            raise ValueError("invalid attachment storage receipt")
        return _record_local(conn, row, path, receipt["blob_key"])
    path = Path(row["stored_path"]) if row["stored_path"] else _destination(row, board)
    downloaded = None
    if path.is_file():
        try:
            data = _read(path, row, board)
            _valid(data, key)
        except (OSError, ValueError):
            data = None
    else:
        data = None
    if data is None:
        path = _destination(row, board)
        data = hub.get_attachment_blob(board, row["uid"])
        if len(data) != row["size"] or len(data) > kb.KANBAN_ATTACHMENT_MAX_BYTES:
            raise ValueError("attachment size mismatch")
        _valid(data, key)
        downloaded = data
    return _record_local(conn, row, path, key, data=downloaded)


def transfer_pending(conn, hub, board, *, limit=2):
    """At most two transfers per store/round; failures never roll back row sync.

    Retry identity includes blob_key so a late storage receipt bypasses the
    backoff for metadata-only rows. Verified rows are rehashed periodically;
    missing or damaged local files recover without requiring a new feed event.
    """
    if not callable(getattr(hub, "put_attachment", None)):
        return 0  # older embedding transports can still sync metadata
    from hermes_cli.kanban_sync_client import HubError
    _setup(conn)
    now = _now()
    rows = conn.execute(
        "SELECT a.* FROM task_attachments a LEFT JOIN kanban_attachment_retry r ON r.uid=a.uid "
        "WHERE a.uid IS NOT NULL "
        "AND (r.uid IS NULL OR r.blob_key IS NOT a.blob_key OR r.retry_at<=?) "
        # First transfers and newly available/retryable bytes precede maintenance.
        "ORDER BY CASE WHEN r.uid IS NULL OR r.blob_key IS NOT a.blob_key "
        "OR (r.error IS NOT NULL AND (a.blob_key IS NOT NULL OR a.stored_path <> '')) THEN 0 ELSE 1 END, "
        "coalesce(r.retry_at,0),a.id", (now,))
    selected = []
    try:
        for raw in rows:
            if len(selected) >= limit:
                break
            if not frozen(conn, raw["task_id"]):
                selected.append(dict(raw))
    finally:
        rows.close()  # Never retain a read snapshot across network I/O / writer upgrade.
    transferred = 0
    stamps = []
    for row in selected:
        error, done = None, False
        try:
            done = _transfer(conn, hub, board, row)
            transferred += int(done)
        except (OSError, ValueError, HubError) as exc:
            error = type(exc).__name__  # no remote response bodies/credentials in durable state
            _log.warning("kanban attachment %s transfer pending (%s)", row["uid"], error)
        stamps.append((row["uid"], row["blob_key"], now + (600 if done else 30), error, row))
    if stamps:
        with write_txn(conn):
            stamps = [stamp[:4] for stamp in stamps if not frozen(conn, stamp[4]["task_id"])
                      and conn.execute("SELECT 1 FROM task_attachments WHERE uid=? AND task_id=? "
                                       "AND size=? AND filename=? AND created_at=?",
                                       (stamp[0], stamp[4]["task_id"], stamp[4]["size"],
                                        stamp[4]["filename"], stamp[4]["created_at"])).fetchone()]
            conn.executemany("INSERT INTO kanban_attachment_retry(uid,blob_key,attempts,retry_at,error) "
                             "VALUES (?,?,1,?,?) ON CONFLICT(uid) DO UPDATE SET blob_key=excluded.blob_key, "
                             "attempts=attempts+1,retry_at=excluded.retry_at,error=excluded.error", stamps)
    return transferred
