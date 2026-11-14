"""Writer-local file custody, additive to immutable completion evidence refs."""
from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import socket
from pathlib import Path


def latest_receipt(events) -> dict:
    """Expose custody independently of truncated event-history views."""
    for event in reversed(events):
        if event.kind == "completed":
            payload = event.payload or {}
            return {"artifact_receipt": payload.get("artifact_receipt", [])}
    return {"artifact_receipt": []}


def format_receipt(payload) -> str:
    """Append writer-qualified custody without changing legacy notice text."""
    records = payload.get("artifact_receipt") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return ""
    lines = []
    for record in records:
        if not isinstance(record, dict) or not record.get("machine") or not record.get("path"):
            continue
        line = f"{record['machine']}:{record['path']}"
        for key in ("retained_path", "sha256", "attachment_uid", "warning"):
            if record.get(key):
                line += f" — {key}: {record[key]}"
        lines.append(line)
    return "\nArtifacts:\n" + "\n".join(lines) if lines else ""


def executing_machine(conn) -> str:
    """The local sync identity is the executor, not tasks.home_gateway."""
    from hermes_cli import kanban_db_sync as sync

    if sync.board_mode(conn) != "off":
        return sync._state(conn)["gateway_id"]
    return socket.gethostname()


def declared_files(metadata: dict) -> list[str]:
    from hermes_cli.kanban_metadata_files import cleaned_artifact_paths

    paths = cleaned_artifact_paths(metadata)
    receipt = metadata.get("completion_receipt")
    if isinstance(receipt, dict):
        for item in receipt.get("items", []):
            superseded = item.get("superseded_by", "")
            if isinstance(superseded, str) and superseded.startswith(("/", "~/")):
                paths.append(superseded)
            for evidence in item.get("evidence", []):
                ref = evidence.get("ref", "")
                if isinstance(ref, str) and ref.startswith(("/", "~/")):
                    paths.append(ref)
    return list(dict.fromkeys(paths))


def _in_managed_workspace(conn, task_id: str, source: Path) -> bool:
    from hermes_cli.kanban_db_workspace import _scratch_workspace, _managed_scratch_path_info

    workspace = _scratch_workspace(conn, task_id)
    return bool(workspace is not None and _managed_scratch_path_info(workspace)[0]
                and source.resolve().is_relative_to(workspace.resolve()))


def record_files(conn, task_id: str, metadata: dict, originals: list[str], now: int,
                 staged: list[Path], *, uploaded_by: str, relocated: dict[str, str]) -> None:
    """Attach small external files through the same byte-sync as scratch copies.

    The generated receipt never rewrites an observed evidence identity. A local
    attachment path is useful only alongside its machine; attachment_uid locates
    the bytes on another replica after normal Board blob synchronization.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_metadata_files import ATTACHED_KEY

    machine = executing_machine(conn)
    kept = dict(relocated)
    kept.update(metadata.get(ATTACHED_KEY, {}))
    records = []
    for raw in originals:
        record: dict = {"machine": machine, "path": raw}
        records.append(record)
        source = Path(kept.get(raw, raw)).expanduser()
        dest = None
        try:
            if not source.is_file():
                record["warning"] = "File missing or not a regular file on executing machine"
                continue
            from gateway.platforms.base import validate_media_delivery_path

            if validate_media_delivery_path(str(source)) is None:
                record["warning"] = "Artifact denied by delivery policy on executing machine"
                continue
            size = source.stat().st_size
            record["size"] = size
            if size <= kb.KANBAN_ATTACHMENT_MAX_BYTES:
                row = conn.execute(
                    "SELECT * FROM task_attachments WHERE task_id=? AND stored_path=?",
                    (task_id, str(source)),
                ).fetchone()
                if row is None:
                    directory = kb.task_attachments_dir(task_id)
                    directory.mkdir(parents=True, exist_ok=True)
                    dest = kb._unique_attachment_path(directory, source.name, set(staged))
                    staged.append(dest)
                    kb._copy_capped(source, dest, raw)
                    kb._insert_completion_attachment(
                        conn, task_id, filename=dest.name, stored_path=str(dest),
                        size=dest.stat().st_size, created_at=now, uploaded_by=uploaded_by,
                    )
                    source = dest
                    row = conn.execute(
                        "SELECT * FROM task_attachments WHERE task_id=? AND stored_path=?",
                        (task_id, str(dest)),
                    ).fetchone()
                record["attachment_id"] = row["id"]
                if "uid" in row.keys():
                    record["attachment_uid"] = row["uid"]
                record["attachment_path"] = row["stored_path"]
            elif _in_managed_workspace(conn, task_id, source):
                # Large files cannot use blob sync, but must outlive scratch GC.
                directory = kb.task_attachments_dir(task_id) / "local-artifacts"
                directory.mkdir(parents=True, exist_ok=True)
                dest = kb._unique_attachment_path(directory, source.name, set(staged))
                staged.append(dest)
                with source.open("rb") as src, dest.open("xb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                    dst.flush()
                    os.fsync(dst.fileno())
                source = dest
                record["retained_path"] = str(dest)
            with source.open("rb") as stream:
                record["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
        except (OSError, kb.ArtifactPreservationError) as exc:
            # Do not claim a delivery that could not be read or attached.
            if dest is not None and "attachment_id" not in record:
                with contextlib.suppress(OSError):
                    dest.unlink(missing_ok=True)
            record.pop("retained_path", None)
            if _in_managed_workspace(conn, task_id, Path(raw).expanduser()):
                raise kb.ArtifactPreservationError(
                    f"could not preserve scratch artifact {raw}: {exc}"
                ) from exc
            record["warning"] = f"Artifact unavailable on executing machine: {exc}"
    if records:
        metadata["artifact_receipt"] = records
    else:
        metadata.pop("artifact_receipt", None)
