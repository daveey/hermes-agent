"""Keep scratch-workspace files that a handoff's metadata names. [daveey]

Workers cite workspace files in ``kanban_complete`` / ``request_review``
metadata (``replay_report=<workspace>/REPORT.md``, ``plan=<workspace>/PLAN.md``).
The managed scratch workspace is removed right after completion, and only
``metadata["artifacts"]`` used to be copied out, so reviewers found those
citations dangling and could not recompute the numbers they back.

:func:`preserve_metadata_files` runs inside the transition's txn, after the
declared artifacts are staged. Every regular file under the task's managed
scratch workspace that a metadata string names is copied into the task's
attachments dir (total budget :data:`METADATA_FILES_MAX_BYTES`) and recorded as
an attachment row. The metadata string is rewritten to the copy, and
``attached_from_workspace`` maps each original path to its copy. Unlike a
declared artifact, a named file that cannot be kept never fails the
transition: it is skipped, the reason is logged, and it is recorded under
``attached_from_workspace_skipped``. Paths outside the workspace are untouched.
Completion receipts are the exception: their observed identities are never rewritten,
and completion refuses any scratch evidence that cannot be preserved. The existing
attachment map locates the durable copy without pretending it was observed anew.
"""

from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterator, Optional

_log = logging.getLogger(__name__)

METADATA_FILES_MAX_BYTES = 20 * 1024 * 1024
ATTACHED_KEY = "attached_from_workspace"
SKIPPED_KEY = "attached_from_workspace_skipped"
# Top-level keys this pass never scans or rewrites: declared artifacts are staged
# (and rewritten) by ``_persist_scratch_completion_artifacts``; the rest are ours.
_SKIP_KEYS = frozenset({"artifacts", "_staged_artifacts", ATTACHED_KEY, SKIPPED_KEY})
_TRAILING_PUNCT = ".,;:!?)]}"


def merge_completion_prose_artifacts(
    conn: sqlite3.Connection, task_id: str, metadata: Optional[dict], *, summary: Optional[str],
    result: Optional[str],
) -> Optional[dict]:
    """Legacy workers' scratch deliverables named only in completion prose."""
    from hermes_cli import kanban_db as kb

    workspace = kb._scratch_workspace(conn, task_id)
    if workspace is None:
        return metadata
    if not kb._is_managed_scratch_path(workspace):
        return metadata
    text = "\n".join(part for part in (summary, result) if part)
    if not text:
        return metadata
    prefix = re.escape(str(workspace))
    discovered: list[str] = []
    for match in re.finditer(prefix + r"(?:[/\\][^\s`\"'<>]+)", text):
        raw = match.group(0).rstrip(".,;:!?)]}")
        candidate = Path(raw)
        if candidate.is_file():
            discovered.append(str(candidate))
    if not discovered:
        return metadata
    updated = dict(metadata) if isinstance(metadata, dict) else {}
    existing = updated.get("artifacts")
    merged = list(existing) if isinstance(existing, (list, tuple)) else []
    seen = {str(path) for path in merged}
    for path in discovered:
        if path not in seen:
            merged.append(path)
            seen.add(path)
    updated["artifacts"] = merged
    return updated


def cleaned_artifact_paths(metadata: Any) -> list[str]:
    """Non-blank string paths declared in ``metadata[\"artifacts\"]``."""
    if not isinstance(metadata, dict):
        return []
    raw = metadata.get("artifacts")
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(p).strip() for p in raw if isinstance(p, str) and str(p).strip()]


def _iter_strings(value: Any, top: bool = True) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if top and key in _SKIP_KEYS:
                continue
            yield from _iter_strings(item, top=False)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item, top=False)


def _rewrite(value: Any, mapping: dict[str, str], top: bool = True) -> Any:
    if isinstance(value, str):
        for raw in sorted(mapping, key=len, reverse=True):
            value = value.replace(raw, mapping[raw])
        return value
    if isinstance(value, dict):
        return {
            key: item if top and key in (_SKIP_KEYS | {"completion_receipt"}) else _rewrite(item, mapping, top=False)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_rewrite(item, mapping, top=False) for item in value]
    if isinstance(value, tuple):
        return tuple(_rewrite(item, mapping, top=False) for item in value)
    return value


def _candidates(metadata: dict, prefixes: list[str]) -> list[str]:
    """Workspace paths named by metadata strings, in first-seen order.

    A string whose whole value is one existing file under the workspace names
    only that file: it may contain spaces, and tokenizing it as well would
    yield its leading fragment (``<ws>/replay`` from ``<ws>/replay report.md``)
    as a phantom path that is then recorded as skipped. [daveey]
    """
    pattern = re.compile(
        "(?:" + "|".join(re.escape(p) for p in prefixes) + r")[/\\][^\s`\"'<>]+"
    )
    seen: dict[str, None] = {}
    for text in _iter_strings(metadata):
        whole = text.strip()
        # A value that is exactly one path may contain spaces the regex stops at.
        if any(whole.startswith(p + "/") for p in prefixes) and Path(whole).is_file():
            seen.setdefault(whole)
            continue
        for match in pattern.finditer(text):
            seen.setdefault(match.group(0).rstrip(_TRAILING_PUNCT))
    return list(seen)


def preserve_metadata_files(
    conn: sqlite3.Connection, task_id: str, metadata: dict, now: int, *,
    uploaded_by: str, known: Optional[dict[str, str]] = None,
    max_total_bytes: Optional[int] = None,
) -> list[Path]:
    """Copy workspace files named in *metadata* to attachments; mutate *metadata*.

    ``known`` maps resolved source paths already staged as declared artifacts to
    their copies, so a file named both ways is copied once. Returns the new copies
    so a caller whose txn rolls back can discard them.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_workspace import _managed_scratch_path_info, _scratch_workspace

    if not isinstance(metadata, dict):
        return []
    workspace = _scratch_workspace(conn, task_id)
    if workspace is None:
        return []
    is_managed, board = _managed_scratch_path_info(workspace)
    if not is_managed:
        return []
    try:
        root = workspace.resolve()
    except OSError:
        return []
    prefixes = sorted({str(workspace).rstrip("/"), str(root).rstrip("/")}, key=len, reverse=True)
    candidates = _candidates(metadata, prefixes)
    if not candidates:
        return []

    budget = METADATA_FILES_MAX_BYTES if max_total_bytes is None else max_total_bytes
    attachment_dir = kb.task_attachments_dir(task_id, board=board)
    by_source: dict[Path, str] = {Path(src): dest for src, dest in (known or {}).items()}
    attached: dict[str, str] = {}
    skipped: dict[str, str] = {}
    copies: list[Path] = []
    used: set[Path] = set()
    total = 0
    for raw in candidates:
        try:
            src = Path(raw).resolve()
        except OSError as exc:
            skipped[raw] = f"unresolvable path: {exc}"
            continue
        if not src.is_relative_to(root):
            skipped[raw] = "resolves outside the scratch workspace"
            continue
        if src in by_source:
            attached[raw] = by_source[src]
            continue
        if not src.exists():
            skipped[raw] = "not found in the scratch workspace"
            continue
        if not src.is_file():
            skipped[raw] = "not a regular file"
            continue
        size = src.stat().st_size
        if total + size > budget:
            skipped[raw] = (
                f"{size} bytes exceeds the {budget}-byte total cap for metadata-named "
                f"workspace files ({budget - total} bytes left)"
            )
            continue
        dest: Optional[Path] = None
        try:
            attachment_dir.mkdir(parents=True, exist_ok=True)
            dest = kb._unique_attachment_path(attachment_dir, src.name, used)
            kb._copy_capped(src, dest, raw)
            copied = dest.stat().st_size
            if total + copied > budget:
                raise kb.ArtifactPreservationError(f"grew past the {budget}-byte total cap while copying")
        except (OSError, kb.ArtifactPreservationError) as exc:  # a cited file never fails the transition
            if dest is not None:
                with contextlib.suppress(OSError):
                    dest.unlink(missing_ok=True)
            skipped[raw] = f"copy failed: {exc}"
            continue
        used.add(dest)
        copies.append(dest)
        total += copied
        stored = str(dest.resolve())
        kb._insert_completion_attachment(
            conn, task_id, filename=dest.name, stored_path=stored, size=copied,
            created_at=now, uploaded_by=uploaded_by,
        )
        by_source[src] = stored
        attached[raw] = stored

    for raw, reason in skipped.items():
        _log.warning(
            "kanban %s: not preserving workspace file named in handoff metadata %s: %s",
            task_id, raw, reason,
        )
    if attached:
        metadata.update(_rewrite(metadata, attached))
        metadata[ATTACHED_KEY] = attached
    if skipped:
        metadata[SKIPPED_KEY] = skipped
    return copies


def stage_completion_artifacts(
    conn: sqlite3.Connection, task_id: str, metadata: dict, now: int, *,
    uploaded_by: str = "kanban_complete",
) -> list[Path]:
    """Copy scratch artifacts to the attachments dir and record each as an
    attachment row; returns the copies so the caller can discard them if its
    transaction rolls back. Workspace files that other metadata values name
    (``replay_report=<ws>/REPORT.md``) are kept too, best-effort
    (:func:`preserve_metadata_files`). [daveey]"""
    from hermes_cli import kanban_db as kb

    from hermes_cli.kanban_artifact_receipt import declared_files, record_files

    originals = declared_files(metadata)
    metadata.pop("artifact_receipt", None)
    staged: list[Path] = []
    # Only this staging pass can attest relocation; caller-provided maps
    # must not make missing evidence appear preserved.
    metadata.pop(ATTACHED_KEY, None)
    metadata.pop(SKIPPED_KEY, None)
    with discard_on_error(staged):
        declared = kb._cleaned_artifact_paths(metadata)
        kb._persist_scratch_completion_artifacts(conn, task_id, metadata)
        staged.extend(Path(stored_path) for stored_path in metadata.pop("_staged_artifacts", []))
        for path in staged:
            kb._insert_completion_attachment(
                conn, task_id, filename=path.name, stored_path=str(path),
                size=path.stat().st_size, created_at=now, uploaded_by=uploaded_by,
            )
        known: dict[str, str] = {}
        for original, kept in zip(declared, kb._cleaned_artifact_paths(metadata)):
            if kept != original:
                with contextlib.suppress(OSError):
                    known[str(Path(original).expanduser().resolve())] = kept
        staged.extend(preserve_metadata_files(
            conn, task_id, metadata, now, uploaded_by=uploaded_by, known=known,
        ))
        if uploaded_by == "kanban_complete" and "completion_receipt" in metadata:
            _verify_receipt_files(conn, task_id, metadata)
        record_files(conn, task_id, metadata, originals, now, staged, uploaded_by=uploaded_by,
                     relocated=dict(zip(declared, kb._cleaned_artifact_paths(metadata))))
    return staged


@contextlib.contextmanager
def discard_on_error(staged: list[Path]):
    """File copies share the caller's transaction fate, including commit failure."""
    succeeded = False
    try:
        yield
        succeeded = True
    finally:
        if not succeeded:
            for path in staged:
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)


def _verify_receipt_files(conn, task_id: str, metadata: dict) -> None:
    """Keep observed identity immutable; existing attachment mapping locates copies."""
    from hermes_completion_receipt import validate_completion_receipt
    from hermes_cli.kanban_db import ArtifactPreservationError
    from hermes_cli.kanban_db_workspace import _scratch_workspace, _managed_scratch_path_info

    receipt = validate_completion_receipt(metadata["completion_receipt"])
    workspace = _scratch_workspace(conn, task_id)
    if workspace is None or not _managed_scratch_path_info(workspace)[0]:
        return
    prefixes = {str(workspace).rstrip("/"), str(workspace.resolve()).rstrip("/")}
    refs = {e["ref"] for item in receipt["items"] for e in item["evidence"]}
    refs.update(item["superseded_by"] for item in receipt["items"] if "superseded_by" in item)
    attached = metadata.get(ATTACHED_KEY, {})
    for ref in refs:
        # Never normalize an evidence identity silently. Workspace aliases and
        # embedded paths must be resubmitted as the exact absolute file reference
        # that this staging pass preserved (not lost during scratch cleanup).
        scratch_ref = any(prefix + "/" in ref for prefix in prefixes)
        if ref.startswith(("./", "../", "~/")):
            path = Path(ref).expanduser()
            path = path if path.is_absolute() else workspace / path
            scratch_ref = scratch_ref or path.resolve().is_relative_to(workspace.resolve())
        if scratch_ref and ref not in attached:
            raise ArtifactPreservationError("completion_receipt evidence could not be preserved: " + ref)
