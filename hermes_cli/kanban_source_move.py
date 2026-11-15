"""workspace_source v1 move / reservation (core PR-2; plan v2 §7, v3 §9, v4 S5).

* ``dir`` stays ``workspace_bound`` forever.
* A worktree card moves only with **portable custody**: feature enabled, a valid replica manifest, Board custody
  ``published``/``unchanged`` at the manifest's revision and, when this host holds the source, a clean local worktree
  with ``HEAD == manifest.commit`` (:func:`parked_refusal`).
* A RUNNING card publishes first (§4 steps 1-8) and is checked against that fresh result (:func:`publish_for_move`);
  the ``continue_task`` that ends the run carries the receipt.
* ``--reclaim`` refuses worktree cards and every card with source custody or a manifest (``reclaim_source_unsupported``).
* A scratch card that registered an external worktree (protocol 2) is a source card too: it publishes before a running
  move and moves parked only with portable custody; its holder's local check reads the publishing run's external
  worktree from the journal, never the scratch cwd. A plain scratch card is unaffected.
* A portable worktree card is not host-only: its capability needs drop ``host_repo`` (:func:`capability_kind`,
  :func:`effective_requires`), also the copy ``create_task`` stored in ``requires``.
"""

from __future__ import annotations

import json
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


def _source_card(row: sqlite3.Row) -> bool:
    """A card whose source travels with Board custody: every worktree card, and a scratch card that registered an
    external worktree (it has custody or a manifest). A plain scratch card has neither and moves freely."""
    kind = row["workspace_kind"]
    return kind == "worktree" or (kind != "dir" and (row["workspace_source"] is not None
                                                    or row["workspace_custody"] is not None))


def _holder_source_path(conn: sqlite3.Connection, task_id: str, row: sqlite3.Row, manifest: Any) -> Optional[str]:
    """Where the holder's published source lives: a worktree card's workspace, or for a scratch card the external
    worktree this host's run registered (journal), never the scratch cwd. That run is the publisher, or, after an
    unchanged ending (the manifest still names the original publisher), this host's latest run that attached at
    exactly this manifest (revision and commit). None when this host has no such record."""
    if row["workspace_kind"] == "worktree":
        return row["workspace_path"]
    try:
        rows = conn.execute("SELECT run_uid, manifest_revision, manifest, path FROM kanban_source_runs "
                            "WHERE task_id = ? ORDER BY run_id DESC", (task_id,)).fetchall()
    except sqlite3.OperationalError:  # no journal table: this host never attached a source run
        return None
    publisher = manifest.published_by.get("run_uid")
    for run_uid, revision, attached, path in rows:
        if not path:
            continue
        if run_uid == publisher:
            return path
        try:
            commit = json.loads(attached).get("commit") if attached else None
        except (TypeError, ValueError, AttributeError):
            commit = None
        if revision == manifest.manifest_revision and commit == manifest.commit:
            return path
    return None


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
    if not _source_card(row):
        return None
    manifest_value, custody_value = fresh or (row["workspace_source"], row["workspace_custody"])
    code = portable(manifest_value, custody_value)
    if code == "source_disabled":
        return "workspace_bound", f"a {kind} source card runs only where its source is (workspace_source disabled)"
    if code:
        return code, f"a {kind} source card moves only with portable custody ({code})"
    custody = custody_value if isinstance(custody_value, ws.Custody) else ws.parse_custody(custody_value)
    manifest = ws.parse_manifest(manifest_value)
    commit = manifest.commit
    if custody is not None and custody.source_holder == me:
        path = _holder_source_path(conn, task_id, row, manifest)
        if path is None or not _local_matches(path, commit):
            return ("workspace_source_unpublished",
                    f"the local source is not clean at the published commit {commit[:12]}; let a run publish it")
    return None


def reclaim_refusal(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    row = _row(conn, task_id)
    if row is None:
        return None
    if _source_card(row):
        return "reclaim_source_unsupported"
    return None


def publish_for_move(conn: sqlite3.Connection, task_id: str) -> tuple[Optional[dict], Optional[tuple[Any, Any]], str]:
    """Publish a running card before its move check: ``(receipt, (manifest, custody), "")`` or
    ``(None, None, refusal_code)``. A run that never attached returns ``(None, None, "")`` (nothing to publish)."""
    row = _row(conn, task_id)
    # Any non-dir card: a scratch card's external worktree attaches mid-run, so its replica may not show custody yet;
    # receipt_for_current_run is the authority (no attached run: nothing to publish).
    if row is None or row["workspace_kind"] == "dir":
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
