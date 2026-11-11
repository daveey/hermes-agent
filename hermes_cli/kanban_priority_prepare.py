"""Raw canonical capture and immutable staging for operator priority migration."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_priority_manifest import STATUSES, validate
from hermes_cli.kanban_priority_migration import plan_global, save_manifest


def capture(hub, *, board_id: str, exceptions: list[str]) -> dict:
    """Read raw Board pages at one feed head, without importing/clipping values."""
    rows = []
    after = ""
    head = None
    for _ in range(10000):
        page = hub.snapshot_page(board_id, "tasks", after=after)
        if page.get("table") != "tasks" or type(page.get("head")) is not int:
            raise ValueError("invalid canonical snapshot page")
        if head is None:
            head = page["head"]
        if page["head"] != head:
            raise ValueError("canonical inventory changed; capture a new manifest")
        batch = page.get("rows")
        if not isinstance(batch, list) or type(page.get("more")) is not bool:
            raise ValueError("invalid canonical inventory")
        for row in batch:
            if not isinstance(row, dict) or row.get("status") not in STATUSES:
                raise ValueError("invalid canonical task status")
            key = row.get("id")
            if not isinstance(key, str) or key <= after:
                raise ValueError("invalid snapshot cursor ordering")
            after = key
            rows.append(row)
        if not page["more"]:
            break
        if not batch or page.get("next") != after:
            raise ValueError("snapshot cursor failed to advance")
    else:
        raise ValueError("snapshot page limit exceeded")
    # A final read closes the final-page/return gap at an observed head. Apply
    # still has local CAS; this is not a distributed transaction or a lease.
    final = hub.snapshot_page(board_id, "tasks", limit=1)
    if final.get("head") != head:
        raise ValueError("canonical inventory changed; capture a new manifest")
    return validate(plan_global(rows, board_id=board_id, exceptions=exceptions))


def stage(conn, manifest: dict, *, path: Path, task_id: str, writer: str) -> dict:
    """Save a fresh immutable backup and attach identical bytes on the local owner."""
    from hermes_cli.kanban_db_sync import sync_state
    from hermes_cli.kanban_supervisor_requests import _non_worker_refusal

    refusal = _non_worker_refusal()
    if refusal:
        raise PermissionError(refusal)
    validate(manifest)
    identity = sync_state(conn) or {}
    if identity.get("mode") != "primary" or identity.get("gateway_id") != writer:
        raise ValueError("staging requires this primary writer")
    if identity.get("board_id") != manifest["board_id"]:
        raise ValueError("manifest belongs to a different board")
    owner = conn.execute("SELECT created_on FROM tasks WHERE id=?", (task_id,)).fetchone()
    if owner is None or owner[0] != writer:
        raise ValueError("attachment owner must belong to this writer")
    save_manifest(path, manifest)
    # Preserve the backup even if attachment storage subsequently fails. The
    # existing attachment writer owns its transaction; staging changes no priority.
    # Apply revalidates writer, attachment and complete owned inventory atomically.
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    attachment_id = kb.store_attachment_bytes(conn, task_id, path.name, data,
                                               content_type="application/json")
    attachment = kb.get_attachment(conn, attachment_id)
    if attachment is None or Path(attachment.stored_path).read_bytes() != data:
        raise ValueError("attached manifest readback mismatch; retain original backup")
    return {"attachment_id": attachment_id, "manifest_sha256": digest,
            "writer": writer, "backup": str(path)}
