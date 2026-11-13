"""workspace_source v1 move / reservation (core PR-2; plan v2 §7, v3 §9, v4 S5).

* ``dir`` stays ``workspace_bound`` forever.
* A worktree card moves only with **portable custody**: feature enabled, a valid replica manifest, Board custody
  ``published``/``unchanged`` at the manifest's revision and, when this host holds the source, a clean local worktree
  with ``HEAD == manifest.commit`` (:func:`parked_refusal`).
* A RUNNING card publishes first (§4 steps 1-8) and is checked against that fresh result (:func:`publish_for_move`);
  the ``continue_task`` that ends the run carries the receipt.
* ``--reclaim`` refuses worktree cards and manifest-bearing cards (``reclaim_source_unsupported``).
* A portable worktree card is not host-only: its capability needs drop ``host_repo`` (:func:`capability_kind`,
  :func:`effective_requires`), also the copy ``create_task`` stored in ``requires``.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping, Optional

from hermes_cli import kanban_workspace_source as ws

PORTABLE_STATES = ("published", "unchanged")


def _row(conn: sqlite3.Connection, task_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(f"SELECT workspace_kind, workspace_path, {ws.source_columns(conn)} FROM tasks "
                        "WHERE id = ?", (task_id,)).fetchone()


def _local_matches(path: Optional[str], commit: str) -> bool:
    from hermes_cli.kanban_source_launch import observe
    seen = observe(path, None)
    return bool(seen["clean"] and seen["head"] == commit)


def portable(manifest_value: Any, custody_value: Any) -> Optional[str]:
    """None when (manifest, custody) is portable, else the refusal code (no local check)."""
    if not ws.settings().enabled:
        return "source_disabled"
    if manifest_value is None:
        return "workspace_source_unpublished"
    try:
        manifest = ws.parse_manifest(manifest_value)
    except ws.ManifestInvalid:
        return "manifest_invalid"
    custody = custody_value if isinstance(custody_value, ws.Custody) else ws.parse_custody(custody_value)
    if custody is None:
        return "workspace_source_not_on_board"
    if custody.state not in PORTABLE_STATES or custody.manifest_revision != manifest.manifest_revision:
        return "custody_retained"
    return None


def parked_refusal(conn: sqlite3.Connection, task_id: str, *, me: str,
                   fresh: Optional[tuple[Any, Any]] = None) -> Optional[tuple[str, str]]:
    """``(code, message)`` when this card may not move, None when it may. ``fresh`` is a just-published
    ``(manifest, custody)`` that replaces the (possibly stale) replica columns."""
    row = _row(conn, task_id)
    if row is None:
        return None
    kind = row["workspace_kind"]
    if kind == "dir":
        return "workspace_bound", "a dir card runs only where its workspace is"
    if kind != "worktree":
        return None
    manifest_value, custody_value = fresh or (row["workspace_source"], row["workspace_custody"])
    code = portable(manifest_value, custody_value)
    if code == "source_disabled":
        return "workspace_bound", "a worktree card runs only where its workspace is (workspace_source disabled)"
    if code:
        return code, f"a worktree card moves only with portable custody ({code})"
    custody = custody_value if isinstance(custody_value, ws.Custody) else ws.parse_custody(custody_value)
    commit = ws.parse_manifest(manifest_value).commit
    if custody is not None and custody.source_holder == me and not _local_matches(row["workspace_path"], commit):
        return ("workspace_source_unpublished",
                f"the local worktree is not clean at the published commit {commit[:12]}; let a run publish it")
    return None


def reclaim_refusal(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    row = _row(conn, task_id)
    if row is None:
        return None
    if row["workspace_kind"] == "worktree" or row["workspace_source"] is not None:
        return "reclaim_source_unsupported"
    return None


def publish_for_move(conn: sqlite3.Connection, task_id: str) -> tuple[Optional[dict], Optional[tuple[Any, Any]], str]:
    """Publish a running card before its move check: ``(receipt, (manifest, custody), "")`` or
    ``(None, None, refusal_code)``. A run that never attached returns ``(None, None, "")`` (nothing to publish)."""
    row = _row(conn, task_id)
    if row is None or row["workspace_kind"] != "worktree":
        return None, None, ""
    from hermes_cli import kanban_source_board as board_calls
    from hermes_cli.kanban_source_preflight import receipt_for_current_run
    run_id, receipt = receipt_for_current_run(conn, task_id, publish=True)
    if receipt is None:
        return None, None, ""
    if receipt["state"] == "retained":
        return None, None, str(receipt.get("reason") or "custody_retained").removeprefix("publish_error=")
    run = board_calls.attached_run(conn, run_id)
    manifest = run.manifest if run is not None else None
    custody = ws.Custody(source_holder=None, state=receipt["state"], manifest_revision=receipt["manifest_revision"])
    return receipt, (manifest, custody), ""


def capability_kind(card: Mapping[str, Any]) -> str:
    """The workspace kind capability checks use: a portable worktree card needs no host checkout."""
    kind = card.get("workspace_kind") or "scratch"
    if kind == "worktree" and portable(card.get("workspace_source"), card.get("workspace_custody")) is None:
        return "worktree_source"
    return kind


def effective_requires(card: Mapping[str, Any], requires: Any) -> list:
    """``requires`` as the move and pickup checks must read it: a portable worktree card drops ``host_repo``.

    ``create_task`` stores ``validate_assignment``'s merged list, so every worktree card's stored ``requires`` holds
    the implicit ``host_repo`` and an explicit one is indistinguishable from it. Portable custody already proves the
    source travels (the Board's repo proof gates the grant), so ``host_repo`` is dropped whenever
    :func:`capability_kind` is ``worktree_source``; any other host need is a gateway need (``ssh:<host>`` ...) or a
    ``runs_on`` label, which stay. LIVE t_01a1d5b1c1798e2a: a published card was refused
    ``machine worker-2 lacks capability:host_repo``."""
    needs = list(requires or [])
    if capability_kind(card) == "worktree_source":
        needs = [n for n in needs if n != "host_repo"]
    return needs
