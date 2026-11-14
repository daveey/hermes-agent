"""Read large completion files before the board write lock. [daveey]

``record_files`` runs inside ``complete_task``'s and ``request_review``'s write
transaction, which holds the board-wide SQLite write lock. Hashing (or copying)
a multi-GB evidence file there stalls the dispatcher and every other worker
until ``DEFAULT_BUSY_TIMEOUT_MS`` fails them. Files over the in-transaction cap
(``KANBAN_ATTACHMENT_MAX_BYTES``) are read here instead, before the lock: their
SHA256 and, for managed-scratch files, a durable executor-local copy written to
a hidden temp name. Under the lock ``record_files`` re-verifies each file by
stat identity (device, inode, size, mtime) and only renames the copy; it never
reads a large file itself.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import os
import uuid
from pathlib import Path
from typing import Optional

_CHUNK = 1024 * 1024
_CACHE: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "kanban_artifact_prestage", default=None,
)


def signature(st: os.stat_result) -> tuple:
    """Identity a file keeps unless it is replaced or written."""
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _read(source: Path, dest: Optional[Path]) -> Optional[tuple[tuple, str]]:
    """Digest (and optionally copy) one file; None when it changed while read."""
    before = signature(source.stat())
    digest = hashlib.sha256()
    with source.open("rb") as src, (dest.open("xb") if dest else contextlib.nullcontext()) as dst:
        while chunk := src.read(_CHUNK):
            digest.update(chunk)
            if dst is not None:
                dst.write(chunk)
        if dst is not None:
            dst.flush()
            os.fsync(dst.fileno())
    if signature(source.stat()) != before:
        return None
    return before, digest.hexdigest()


def _fill(conn, task_id: str, metadata: dict, cache: dict) -> None:
    from gateway.platforms.base import validate_media_delivery_path
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_artifact_receipt import (
        attachment_board, declared_files, in_managed_workspace,
    )

    for raw in declared_files(metadata):
        source = Path(raw).expanduser()
        dest = None
        try:
            if not source.is_file() or validate_media_delivery_path(str(source)) is None:
                continue
            if source.stat().st_size <= kb.KANBAN_ATTACHMENT_MAX_BYTES:
                continue  # Small files stay bounded under the lock.
            if in_managed_workspace(conn, task_id, source):
                board = attachment_board(conn, task_id)
                directory = kb.task_attachments_dir(task_id, board=board) / "local-artifacts"
                directory.mkdir(parents=True, exist_ok=True)
                dest = directory / f".prestage-{uuid.uuid4().hex}"
            observed = _read(source, dest)
            error = None if observed else "changed while it was read before the completion lock"
        except OSError as exc:
            observed, error = None, f"unreadable before the completion lock: {exc}"
        if observed is None:
            if dest is not None:
                with contextlib.suppress(OSError):
                    dest.unlink(missing_ok=True)
            # record_files reports the failure without reading the file itself.
            cache[str(source)] = {"error": error}
            continue
        cache[str(source)] = {"signature": observed[0], "sha256": observed[1], "temp": dest}


@contextlib.contextmanager
def prestaged(conn, task_id: str, metadata):
    """Make large-file digests and copies available to ``record_files``.

    Enter BEFORE ``write_txn``. Copies the transaction never claimed are removed
    on exit; claimed copies follow the caller's staged-copy rollback.
    """
    cache: dict = {}
    token = _CACHE.set(cache)
    try:
        if isinstance(metadata, dict):
            _fill(conn, task_id, metadata, cache)
        yield
    finally:
        _CACHE.reset(token)
        for entry in cache.values():
            if entry.get("temp") is not None:  # unclaimed by the transaction
                with contextlib.suppress(OSError):
                    entry["temp"].unlink(missing_ok=True)


def take(source: Path, st: os.stat_result) -> Optional[dict]:
    """The pre-lock observation of *source*, only if it is still the same file.

    An entry with ``error`` reports why the pre-lock read failed.
    """
    cache = _CACHE.get()
    entry = cache.get(str(source)) if cache else None
    if entry is None or "error" in entry:
        return entry
    if entry["signature"] != signature(st):
        return None
    return entry
