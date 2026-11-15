"""Dispatcher seam for workspace_source v1 (R4b): route on the GRANT snapshot before any ``workspace_kind`` branch, and
turn a resolver refusal into a release (host scope, with the skip memo) or a release plus a human block (card scope).

Inert until core PR-2: the Board grants custody/manifest cards only to clients that send ``source_protocol``, which no
PR-1 core does, so every snapshot here has no source and the legacy resolvers run exactly as before.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_workspace_source as ws
from hermes_cli.kanban_workspace_source import WorkspaceSourceRefusal


def _me(conn: sqlite3.Connection) -> Optional[str]:
    try:
        row = conn.execute("SELECT gateway_id FROM kanban_sync_state LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def resolve(conn: sqlite3.Connection, claimed: Any, *, board: Optional[str], kbw: Any):
    """``(workspace, branch_name, source)``: a card whose grant snapshot carries custody or a manifest goes to the
    source resolver whatever its ``workspace_kind`` (the legacy HEAD-create, ``default_workdir`` anchor and other-task
    branch reuse are unreachable for it); every other card takes the legacy path unchanged.

    S2 (v4, v4.1 §B): a primary grant made with ``source_protocol`` whose response has no snapshot keys is an old
    Board. This process then stops sending the protocol, and a card whose REPLICA already carries custody or a
    manifest is refused here (released, host-scope memo) instead of falling back to the legacy path."""
    snapshot = ws.pop_grant_snapshot(claimed.id)
    from hermes_cli import kanban_source_launch as launch
    launch.note_grant(claimed.id, snapshot)
    try:
        return _resolve(conn, claimed, snapshot, launch, board=board, kbw=kbw)
    except BaseException:
        launch.drop_grant(claimed.id)  # launch() never runs for this claim: do not keep its grant
        raise


def _resolve(conn: sqlite3.Connection, claimed: Any, snapshot: Any, launch: Any, *, board: Optional[str], kbw: Any):
    if snapshot is None and launch.old_board_grant(conn, claimed):
        replica = _replica(conn, claimed.id)
        if replica is not None and replica.has_source:
            raise WorkspaceSourceRefusal("board_lacks_source", "the Board answered without a source snapshot",
                                         evidence={"manifest_revision": _replica_revision(replica)})
    if snapshot is not None and snapshot.has_source:
        from hermes_cli.kanban_source_resolver import resolve_source_workspace
        ttl = (claimed.claim_expires - time.time()) if getattr(claimed, "claim_expires", None) else None
        try:
            if not ws.settings().enabled:
                # Worker-side gate (the Board's source_protocol gate is the other half): a disabled host never
                # materializes, whatever the Board granted it.
                raise WorkspaceSourceRefusal("source_disabled", "kanban.workspace_source is disabled on this host")
            source = resolve_source_workspace(conn, claimed, snapshot, me=_me(conn) or "", ttl_s=ttl)
        except WorkspaceSourceRefusal as exc:
            # The replica may lag or have no manifest. Bind refusal identity to this grant, not that replica.
            try:
                revision = snapshot.manifest().manifest_revision
            except ws.ManifestInvalid:
                revision = snapshot.custody.manifest_revision if snapshot.custody else None
            exc.evidence["manifest_revision"] = revision
            raise
        workspace = (kbw.resolve_workspace(claimed, board=board) if claimed.workspace_kind == "scratch"
                     and source.source_kind == "scratch_external"
                     else Path(source.path))
        return workspace, source.branch, source
    if claimed.workspace_kind == "worktree":
        workspace, branch = kbw.resolve_worktree_for_dispatch(conn, claimed, board=board)
        return workspace, branch, None
    return kbw.resolve_workspace(claimed, board=board), None, None


def _replica(conn: sqlite3.Connection, task_id: str) -> Optional[ws.SourceSnapshot]:
    try:
        row = conn.execute("SELECT workspace_source, workspace_custody FROM tasks WHERE id = ?", (task_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return ws.replica_snapshot(row) if row is not None else None


def _replica_revision(snapshot: ws.SourceSnapshot) -> Optional[int]:
    try:
        return snapshot.manifest().manifest_revision
    except ws.ManifestInvalid:
        return snapshot.custody.manifest_revision if snapshot.custody else None


def materialized_event(conn: sqlite3.Connection, claimed: Any, snapshot: Any, source: Any) -> None:
    """``workspace_source_materialized`` (v2 §6 step 5), emitted once the run attached (v3 §6)."""
    from hermes_cli import kanban_db as kb
    run_id = getattr(claimed, "current_run_id", None)
    row = conn.execute("SELECT uid FROM task_runs WHERE id = ?", (run_id,)).fetchone() if run_id else None
    custody = snapshot.custody if snapshot is not None else None
    payload = source.event_payload(holder=_me(conn), run_uid=row[0] if row else None)
    payload["prev_holder"] = custody.source_holder if custody is not None else None
    if snapshot is not None and snapshot.manifest_value is not None:
        manifest = snapshot.manifest()
        payload.update(repository=manifest.full_name, commit=manifest.commit,
                       manifest_revision=manifest.manifest_revision, source_kind=manifest.source_kind)
    elif custody is not None:
        payload["manifest_revision"] = custody.manifest_revision
    with kb.write_txn(conn):
        kb._append_event(conn, claimed.id, "workspace_source_materialized", payload, run_id=run_id)


def _revision(exc: WorkspaceSourceRefusal) -> Optional[int]:
    value = exc.evidence.get("manifest_revision")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def refused(conn: sqlite3.Connection, claimed: Any, exc: BaseException, result: Any) -> bool:
    """Handle a :class:`WorkspaceSourceRefusal` (True) or leave the exception to the legacy path (False).

    Both scopes end the run as an infrastructure release (the breaker never counts it and custody is untouched: the
    run never attached) and record ``workspace_source_refused``. Host scope memoizes ``(task, manifest_revision)`` so
    this host stops claiming that revision (a slow host does not livelock, Q3); card scope blocks ``needs_input``."""
    if not isinstance(exc, WorkspaceSourceRefusal):
        return False
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_failure_release as release
    from hermes_cli import kanban_db_sync as sync
    import socket
    revision = _revision(exc)
    payload = {"code": exc.code, "scope": exc.scope, "hostname": socket.gethostname(), "manifest_revision": revision,
               "detail": exc.detail[:300]}
    with kb.write_txn(conn):
        primary = release.queue(conn, claimed.id, claimed.current_run_id, outcome="released",
                                unused_grant=True, error=f"workspace_source: {exc}")
        kb._append_event(conn, claimed.id, "workspace_source_refused", payload)
        if exc.scope == "host":
            ws.remember_skip(conn, claimed.id, revision, exc.code)
        elif primary:
            from hermes_cli.kanban_source_refusal_block import defer
            defer(conn, claimed, exc)
    if not primary:
        from hermes_cli.kanban_failure_accounting import _record_task_failure
        _record_task_failure(conn, claimed.id, f"workspace_source: {exc}", outcome="released", release_claim=True,
                             end_run=True, infrastructure=True)
    if exc.scope == "card" and sync.board_mode(conn) != "primary":
        from hermes_cli.kanban_source_refusal_block import block
        if block(conn, claimed.id, exc):
            result.auto_blocked.append(claimed.id)
    return True
