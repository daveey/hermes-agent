"""Writer-local file custody, additive to immutable completion evidence refs."""
from __future__ import annotations

import contextlib
import hashlib
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


def in_managed_workspace(conn, task_id: str, source: Path) -> bool:
    from hermes_cli.kanban_db_workspace import _scratch_workspace, _managed_scratch_path_info

    workspace = _scratch_workspace(conn, task_id)
    return bool(workspace is not None and _managed_scratch_path_info(workspace)[0]
                and source.resolve().is_relative_to(workspace.resolve()))


def attachment_board(conn, task_id: str):
    """The task's own board, never the process's current board.

    A card on a non-default board can complete while another board is current
    (machine flows resolve through the pin); its copies must land under its own
    board's attachments root, where per-board cleanup looks for them.
    """
    from hermes_cli.kanban_db_workspace import _scratch_workspace, _managed_scratch_path_info

    workspace = _scratch_workspace(conn, task_id)
    if workspace is not None:
        managed, board = _managed_scratch_path_info(workspace)
        if managed and board:
            return board
    from hermes_cli.kanban_host_counts import board_of

    return board_of(conn)


def _names_missing_file(conn, task_id: str, raw: str) -> bool:
    """Whether an absent evidence ref claims a file, so its absence earns a warning.

    Evidence refs are free text: ``/api/v1/tasks``, ``/src/x.py:12`` or ``/repo
    commit abc`` start with ``/`` but name no file. Scratch refs always count
    (they are preserved or fail closed); otherwise only an exact path whose
    parent directory exists below the filesystem root.
    """
    path = Path(raw).expanduser()
    if in_managed_workspace(conn, task_id, path):
        return True
    if any(ch.isspace() for ch in raw) or ":" in raw or path.exists():
        return False
    parent = path.parent
    return parent != Path(parent.anchor) and parent.is_dir()


def _large(conn, task_id: str, source: Path, stat, record: dict, staged: list[Path], board) -> None:
    """Record a file over the attachment cap from its pre-lock observation only.

    This runs under the board write lock, so it never reads the file: the digest
    (and a managed-scratch copy) come from :mod:`kanban_artifact_prestage`,
    re-verified here by stat identity.
    """
    from hermes_cli import kanban_artifact_prestage as prestage
    from hermes_cli import kanban_db as kb

    observed = prestage.take(source, stat)
    managed = in_managed_workspace(conn, task_id, source)
    if observed is None or "error" in observed:
        reason = observed["error"] if observed else "changed after it was read before the completion lock"
        if managed:
            raise kb.ArtifactPreservationError(f"{reason}; retry")
        record["warning"] = f"Over {kb.KANBAN_ATTACHMENT_MAX_BYTES} bytes and not hashed: {reason}"
        return
    if managed:
        # Large files cannot use blob sync, but must outlive scratch GC.
        directory = kb.task_attachments_dir(task_id, board=board) / "local-artifacts"
        dest = kb._unique_attachment_path(directory, source.name, set(staged))
        staged.append(dest)
        observed["temp"].rename(dest)
        observed["temp"] = None
        record["retained_path"] = str(dest)
    record["sha256"] = observed["sha256"]


def record_files(conn, task_id: str, metadata: dict, originals: list[str], now: int,
                 staged: list[Path], *, uploaded_by: str, relocated: dict[str, str]) -> None:
    """Attach small external files through the same byte-sync as scratch copies.

    The generated receipt never rewrites an observed evidence identity. A local
    attachment path is useful only alongside its machine; attachment_uid locates
    the bytes on another replica after normal Board blob synchronization.
    ``role`` separates declared deliverables (``artifact``, uploaded to chat)
    from ``evidence`` refs (custody only). Free-text evidence that names no file
    gets no record. Files over the attachment cap are never read here (``_large``).
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_metadata_files import ATTACHED_KEY, cleaned_artifact_paths

    machine = executing_machine(conn)
    board = attachment_board(conn, task_id)
    declared = set(cleaned_artifact_paths(metadata))
    kept = dict(relocated)
    kept.update(metadata.get(ATTACHED_KEY, {}))
    records = []
    for raw in originals:
        role = "artifact" if raw in declared else "evidence"
        source = Path(kept.get(raw, raw)).expanduser()
        if role == "evidence" and not source.is_file() and not _names_missing_file(conn, task_id, raw):
            continue
        record: dict = {"machine": machine, "path": raw, "role": role}
        records.append(record)
        dest = None
        try:
            if not source.is_file():
                record["warning"] = "File missing or not a regular file on executing machine"
                continue
            from gateway.platforms.base import validate_media_delivery_path

            if validate_media_delivery_path(str(source)) is None:
                record["warning"] = "Artifact denied by delivery policy on executing machine"
                continue
            stat = source.stat()
            record["size"] = stat.st_size
            if stat.st_size > kb.KANBAN_ATTACHMENT_MAX_BYTES:
                _large(conn, task_id, source, stat, record, staged, board)
                continue
            row = conn.execute(
                "SELECT * FROM task_attachments WHERE task_id=? AND stored_path=?",
                (task_id, str(source)),
            ).fetchone()
            if row is None:
                directory = kb.task_attachments_dir(task_id, board=board)
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
            with source.open("rb") as stream:
                record["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
        except (OSError, kb.ArtifactPreservationError) as exc:
            # Do not claim a delivery that could not be read or attached.
            if dest is not None and "attachment_id" not in record:
                with contextlib.suppress(OSError):
                    dest.unlink(missing_ok=True)
            record.pop("retained_path", None)
            record.pop("sha256", None)
            if in_managed_workspace(conn, task_id, Path(raw).expanduser()):
                raise kb.ArtifactPreservationError(
                    f"could not preserve scratch artifact {raw}: {exc}"
                ) from exc
            record["warning"] = f"Artifact unavailable on executing machine: {exc}"
    if records:
        metadata["artifact_receipt"] = records
    else:
        metadata.pop("artifact_receipt", None)
