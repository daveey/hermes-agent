"""Explicit source artifacts, layered on the existing attachment row/blob transport.

The local journal is staging/replay metadata, not a second custody authority. An
attachment becomes portable only after the Board's verified blob receipt. No
failure deletes committed attachments or guesses a selection from historical rows.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_sync_attachments as blobs
from hermes_cli.kanban_db_connect import write_txn
from hermes_cli.kanban_source_publish import _blob_secret_line, _denylisted
from hermes_cli.kanban_workspace_source import ManifestInvalid, _parse_artifacts


class ArtifactRefused(ValueError):
    def __init__(self, code: str, detail: str):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}")


def _setup(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS kanban_source_artifact_stage ("
                 "task_id TEXT NOT NULL, source_path TEXT NOT NULL, digest TEXT NOT NULL, "
                 "attachment_uid TEXT NOT NULL, PRIMARY KEY(task_id, source_path, digest))")
    conn.execute("CREATE TABLE IF NOT EXISTS kanban_source_artifact_selection ("
                 "task_id TEXT NOT NULL, run_uid TEXT NOT NULL, entries TEXT NOT NULL, PRIMARY KEY(task_id,run_uid))")


def selected(conn, task_id, run_uid):
    """None means no explicit selection yet; [] is an explicitly empty selection."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='kanban_source_artifact_selection'").fetchone():
        return None
    row = conn.execute("SELECT entries FROM kanban_source_artifact_selection WHERE task_id=? AND run_uid=?",
                       (task_id, run_uid)).fetchone()
    return _entries(json.loads(row[0])) if row else None


def _entries(entries):
    try:
        return _parse_artifacts(entries, 2)
    except ManifestInvalid as exc:
        raise ArtifactRefused("artifact_mismatch", str(exc)) from exc


def _snapshot(raw):
    requested = Path(raw).expanduser()
    before = requested.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ArtifactRefused("artifact_refused", "select a regular file, not a leaf symlink or special file")
    path = requested.resolve(strict=True)
    if _denylisted(path.name):
        raise ArtifactRefused("artifact_refused", "selected path is not an allowed regular file")
    if _blob_secret_line(path.name.encode("utf-8")) is not None:
        raise ArtifactRefused("artifact_refused", "selected filename contains a credential")
    # NONBLOCK prevents a replacement FIFO from hanging before fstat. NOFOLLOW
    # refuses a leaf-link swap; device/inode comparison also catches parent swaps.
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ArtifactRefused("artifact_refused", "selected file changed before snapshot")
        with os.fdopen(fd, "rb", closefd=False) as src:
            data = src.read(kb.KANBAN_ATTACHMENT_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > kb.KANBAN_ATTACHMENT_MAX_BYTES:
        raise ArtifactRefused("artifact_refused", "selected file exceeds attachment size limit")
    if _blob_secret_line(data) is not None:
        raise ArtifactRefused("artifact_refused", "selected bytes contain a credential")
    return path, data, hashlib.sha256(data).hexdigest()


def _row(conn, task_id, entry):
    row = conn.execute("SELECT * FROM task_attachments WHERE uid=? AND task_id=?",
                       (entry["attachment_uid"], task_id)).fetchone()
    if row is None or row["filename"] != entry["filename"]:
        raise ArtifactRefused("artifact_mismatch", "selected attachment missing, foreign or renamed")
    if row["blob_key"] and row["blob_key"] != "sha256:" + entry["sha256"]:
        raise ArtifactRefused("artifact_mismatch", "selected digest differs from verified blob")
    return dict(row)


def _stage_one(conn, task_id, path, digest, dest, size):
    prior = conn.execute("SELECT attachment_uid FROM kanban_source_artifact_stage "
                         "WHERE task_id=? AND source_path=? AND digest=?", (task_id, str(path), digest)).fetchone()
    if prior:
        entry = {"attachment_uid": prior[0], "filename": path.name, "sha256": digest}
        _row(conn, task_id, entry)
        return entry
    kb._insert_completion_attachment(conn, task_id, filename=path.name, stored_path=str(dest),
                                      size=size, created_at=int(time.time()), uploaded_by="workspace_source")
    # Event insertion changes last_insert_rowid; identify the unique staged path instead.
    uid = conn.execute("SELECT uid FROM task_attachments WHERE task_id=? AND stored_path=?",
                       (task_id, str(dest))).fetchone()
    if not uid or not uid[0]:
        raise ArtifactRefused("artifact_unavailable", "attachment has no sync UID")
    conn.execute("INSERT INTO kanban_source_artifact_stage VALUES (?,?,?,?)", (task_id, str(path), digest, uid[0]))
    return {"attachment_uid": uid[0], "filename": path.name, "sha256": digest}


def _copy_snapshot(task_id, raw, board):
    path, data, digest = _snapshot(raw)
    key = hashlib.sha256(json.dumps([task_id, str(path), digest]).encode()).hexdigest()
    dest = kb.task_attachments_dir(task_id, board=board).resolve() / ("source-" + key) / path.name
    if not dest.resolve().is_relative_to(kb.attachments_root(board).resolve()):
        raise ArtifactRefused("artifact_refused", "staging path escapes attachment storage")
    if dest.exists():
        with dest.open("rb") as stream:
            existing = stream.read(kb.KANBAN_ATTACHMENT_MAX_BYTES + 1)
        if existing != data:
            raise ArtifactRefused("artifact_mismatch", "staged bytes changed")
    else:
        blobs._publish_file(dest, data)
    return path, digest, dest, len(data)


def _live_run(conn, task_id, run):
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_source_board as source
    row = conn.execute("SELECT t.status,t.current_run_id,t.lease_expires,r.ended_at,r.outcome "
                       "FROM tasks t JOIN task_runs r ON r.id=t.current_run_id AND r.task_id=t.id "
                       "WHERE t.id=?", (task_id,)).fetchone()
    if (run is None or run.task_id != task_id or run.state != "attached" or not row
            or row[0] != "running" or row[1] != run.run_id or row[3] is not None or row[4] is not None
            or not isinstance(row[2], (int, float)) or not math.isfinite(row[2])
            or row[2] <= time.time() * 1000 or sync.board_mode(conn) != "primary"
            or sync.run_uid(conn, run.run_id) != run.run_uid
            or sync._state(conn)["gateway_id"] != run.machine
            or source.attached_run(conn, run.run_id) != run):
        raise ArtifactRefused("artifact_refused", "selection requires the same live attached run")


def stage_for_run(conn, task_id, paths, *, board, expected_run_id):
    """Run-fenced staging for an already authenticated worker/lifecycle caller.

    The surface must supply its own bound run ID, never infer it from the task.
    This is not a replacement for surface caller authorization or Board custody.
    """
    from hermes_cli import kanban_source_board as source
    run = source.attached_run(conn, expected_run_id)
    _live_run(conn, task_id, run)
    if run is None or run.source_protocol < 2:
        # Protocol-1 (v1 worktree) runs keep ordinary attachment behaviour until
        # protocol 2 is advertised: no snapshot scan, no v2 artifact manifest.
        raise ArtifactRefused("artifact_refused", "artifact selection requires a source protocol 2 run")
    return stage(conn, task_id, paths, board=board, run_uid=run.run_uid, _expected_run=run)


def stage(conn, task_id, paths, *, board, run_uid, _expected_run=None):
    """Atomically replace explicit selection with immutable copied-byte snapshots.

    Caller owns lease/registration authorization. Files are durable before their
    rows commit; rollback/crash may leave an unreferenced deterministic file, never
    a row whose bytes were deleted by another caller's rollback cleanup. Retry
    reuses it. This helper does not upload, publish a manifest or release custody.
    """
    if conn.in_transaction:
        raise ArtifactRefused("artifact_refused", "stage outside the ending transaction")
    if not isinstance(run_uid, str) or not run_uid.strip():
        raise ArtifactRefused("artifact_refused", "selection requires its owning run UID")
    if not isinstance(paths, (list, tuple)) or len(paths) > 32:
        raise ArtifactRefused("artifact_refused", "select at most 32 paths")
    if not conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
        raise ArtifactRefused("artifact_refused", "unknown task")
    prior = selected(conn, task_id, run_uid)
    try:
        # Retain descriptors, never all selected byte buffers (up to 32 x 25 MB).
        staged = {}
        for raw in paths:
            item = _copy_snapshot(task_id, raw, board)
            staged[str(item[0])] = item
        with write_txn(conn):
            if _expected_run is not None:
                _live_run(conn, task_id, _expected_run)
            if selected(conn, task_id, run_uid) != prior:
                raise ArtifactRefused("artifact_refused", "selection changed during staging; retry explicitly")
            _setup(conn)
            entries = _entries([_stage_one(conn, task_id, *item) for item in staged.values()])
            conn.execute("INSERT INTO kanban_source_artifact_selection VALUES (?,?,?) "
                         "ON CONFLICT(task_id,run_uid) DO UPDATE SET entries=excluded.entries",
                         (task_id, run_uid, json.dumps(entries)))
        return entries
    except (OSError, TypeError) as exc:
        raise ArtifactRefused("artifact_unavailable", "selected bytes unavailable") from exc


def completion_mapping(conn, task_id, declared):
    """Trusted original-to-snapshot map restricted to declared current selections."""
    from hermes_cli import kanban_source_board as source
    task = kb.get_task(conn, task_id)
    run = source.attached_run(conn, task.current_run_id) if task else None
    if run is None:
        return {}
    mapping = {}
    for entry in selected(conn, task_id, run.run_uid) or []:
        row = _row(conn, task_id, entry)
        if row["stored_path"] not in declared:
            continue
        for original in conn.execute(
            "SELECT source_path FROM kanban_source_artifact_stage "
            "WHERE task_id=? AND attachment_uid=?", (task_id, entry["attachment_uid"]),
        ):
            mapping[original[0]] = row["stored_path"]
    return mapping


def inherit(conn, task_id, predecessor_uid, successor_uid):
    """Called only inside registration's live-run transaction after exact predecessor proof."""
    entries = selected(conn, task_id, predecessor_uid)
    if entries is None or selected(conn, task_id, successor_uid) is not None:
        return
    for entry in entries:
        _row(conn, task_id, entry)
    _setup(conn)
    conn.execute("INSERT INTO kanban_source_artifact_selection VALUES (?,?,?)",
                 (task_id, successor_uid, json.dumps(entries)))


def publication_fence(conn, task_id, run, expected):
    """Before and after the manifest RPC: same unended current run, same explicit selection.

    ``portable`` only proves its own transport window; a later authenticated stage
    may replace the selection before the Board records the manifest.
    """
    row = conn.execute("SELECT t.current_run_id, r.ended_at, r.outcome FROM tasks t "
                       "JOIN task_runs r ON r.id=t.current_run_id AND r.task_id=t.id WHERE t.id=?",
                       (task_id,)).fetchone()
    if not row or row[0] != run.run_id or row[1] is not None or row[2] is not None:
        raise ArtifactRefused("publish_run_ended", "the publishing run is no longer the live current run")
    if selected(conn, task_id, run.run_uid) != expected:
        raise ArtifactRefused("artifact_selection_changed", "selection changed after transport; retry explicitly")


def receipt_selection_current(conn, task_id, run_id, receipt):
    """In the ending's write transaction: a portable receipt must still describe the run's selection."""
    from hermes_cli import kanban_source_board as source
    run = source.attached_run(conn, run_id) if run_id is not None else None
    if run is None or run.run_uid != receipt.get("run_uid"):
        return True  # not this run's receipt; the Board's own run_uid check decides
    try:
        current = selected(conn, task_id, run.run_uid)
    except ValueError:  # an unreadable selection row is never proof the receipt is still current
        return False
    return current is None or current == (run.manifest or {}).get("artifacts", [])


def bounded_transport(hub, bound):
    """Give this operation a private client capped by its remaining shared budget."""
    from hermes_cli.kanban_source_artifact_http import SourceArtifactHub
    deadline = time.monotonic() + bound.left()
    if hub.deadline is not None:
        deadline = min(deadline, hub.deadline)
    return SourceArtifactHub(hub.ident, timeout=hub.timeout, deadline=deadline)


def portable(conn, task_id, prior, run_uid, *, bound=None):
    """Materialize the explicit selection (or the incoming manifest's selection).

    Push existing attachment metadata before the blob endpoint. Its response is
    still not custody proof: ensure() requires the verified per-UID storage receipt.
    """
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as client
    selection = selected(conn, task_id, run_uid)
    entries = selection
    if entries is None:
        entries = _entries((prior or {}).get("artifacts", []))
    if not entries:
        return []
    state = sync._state(conn)
    hub = getattr(sync._client(), "hub", None)
    if hub is None:
        raise ArtifactRefused("artifact_unavailable", "no Board attachment transport")
    if bound is not None:
        hub = bounded_transport(hub, bound)
    try:
        client.push(conn, hub, state["board_id"], state["gateway_id"], primary=True)
    except (OSError, client.HubError) as exc:
        raise ArtifactRefused("artifact_unavailable", "attachment metadata not synchronized") from exc
    result = []
    for entry in entries:
        if bound is not None:
            bound.left()
        result.extend(ensure(conn, task_id, [entry], hub, state["board_id"]))
        if bound is not None:
            bound.left()
    if selected(conn, task_id, run_uid) != selection:
        raise ArtifactRefused("artifact_refused", "selection changed during transport; retry explicitly")
    return result


def ensure(conn, task_id, entries, hub, board):
    """Verify/transfer only selected rows through existing blob transport, then re-read.

    Metadata must already be synchronized. Unknown upload outcomes keep the same
    UID and bytes for idempotent transport replay. Download verifies the manifest's
    digest, not merely whatever digest a later attachment row happens to carry.
    """
    from hermes_cli.kanban_sync_client import HubError
    entries = _entries(entries)
    try:
        for entry in entries:
            row = _row(conn, task_id, entry)
            if not row["blob_key"]:
                if not row["stored_path"]:
                    raise ArtifactRefused("artifact_unavailable", "selected bytes have no local custodian")
                data = blobs._read(Path(row["stored_path"]), row, board)
                if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                    raise ArtifactRefused("artifact_mismatch", "staged bytes differ from selected digest")
            if not blobs._transfer(conn, hub, board, row, expected_sha256=entry["sha256"]):
                raise ArtifactRefused("artifact_unavailable", "selected attachment transfer pending")
            current = _row(conn, task_id, entry)
            if current["blob_key"] != "sha256:" + entry["sha256"] or not current["stored_path"]:
                raise ArtifactRefused("artifact_mismatch", "attachment changed during transfer")
            blobs._valid(blobs._read(Path(current["stored_path"]), current, board), current["blob_key"])
        return entries
    except ArtifactRefused:
        raise
    except (OSError, HubError) as exc:
        raise ArtifactRefused("artifact_unavailable", "selected attachment transfer unavailable") from exc
    except ValueError as exc:
        raise ArtifactRefused("artifact_mismatch", "selected attachment verification failed") from exc
