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
    branch reuse are unreachable for it); every other card takes the legacy path unchanged."""
    snapshot = ws.pop_grant_snapshot(claimed.id)
    if snapshot is not None and snapshot.has_source:
        from hermes_cli.kanban_source_resolver import resolve_source_workspace
        ttl = (claimed.claim_expires - time.time()) if getattr(claimed, "claim_expires", None) else None
        source = resolve_source_workspace(conn, claimed, snapshot, me=_me(conn) or "", ttl_s=ttl)
        _materialized_event(conn, claimed, snapshot, source)
        return Path(source.path), source.branch, source
    if claimed.workspace_kind == "worktree":
        workspace, branch = kbw.resolve_worktree_for_dispatch(conn, claimed, board=board)
        return workspace, branch, None
    return kbw.resolve_workspace(claimed, board=board), None, None


def _materialized_event(conn: sqlite3.Connection, claimed: Any, snapshot: Any, source: Any) -> None:
    """``workspace_source_materialized`` (v2 §6 step 5). PR-2 moves it after ``source_attach``."""
    from hermes_cli import kanban_db as kb
    run_id = getattr(claimed, "current_run_id", None)
    row = conn.execute("SELECT uid FROM task_runs WHERE id = ?", (run_id,)).fetchone() if run_id else None
    holder = snapshot.custody.source_holder if snapshot.custody is not None else None
    with kb.write_txn(conn):
        kb._append_event(conn, claimed.id, "workspace_source_materialized",
                         source.event_payload(holder=holder, run_uid=row[0] if row else None), run_id=run_id)


def _revision(exc: WorkspaceSourceRefusal, conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    if isinstance(exc.evidence.get("manifest_revision"), int):
        return exc.evidence["manifest_revision"]
    try:
        row = conn.execute("SELECT workspace_source FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return ws.parse_manifest(row[0]).manifest_revision if row and row[0] else None
    except (sqlite3.OperationalError, ws.ManifestInvalid):
        return None


def refused(conn: sqlite3.Connection, claimed: Any, exc: BaseException, result: Any) -> bool:
    """Handle a :class:`WorkspaceSourceRefusal` (True) or leave the exception to the legacy path (False).

    Both scopes end the run as an infrastructure release (the breaker never counts it and custody is untouched: the
    run never attached) and record ``workspace_source_refused``. Host scope memoizes ``(task, manifest_revision)`` so
    this host stops claiming that revision (a slow host does not livelock, Q3); card scope blocks ``needs_input``."""
    if not isinstance(exc, WorkspaceSourceRefusal):
        return False
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_failure_accounting import _record_task_failure
    import socket
    revision = _revision(exc, conn, claimed.id)
    payload = {"code": exc.code, "scope": exc.scope, "hostname": socket.gethostname(), "manifest_revision": revision,
               "detail": exc.detail[:300]}
    _record_task_failure(conn, claimed.id, f"workspace_source: {exc}", outcome="spawn_failed", release_claim=True,
                         end_run=True, infrastructure=True, event_payload_extra={"workspace_source_refused": payload})
    with kb.write_txn(conn):
        kb._append_event(conn, claimed.id, "workspace_source_refused", payload)
        if exc.scope == "host":
            ws.remember_skip(conn, claimed.id, revision, exc.code)
    if exc.scope == "card":
        evidence = "; ".join(f"{k}={v}" for k, v in exc.evidence.items()) or exc.detail or exc.code
        kb.block_task(conn, claimed.id, kind="needs_input", reason=(
            f"workspace_source {exc.code}: {exc.detail}. 1) Check the task branch on the remote and the custody "
            "holder's worktree. 2) Re-publish the source from its holder, then unblock this card."),
            human_request={"action": f"Restore the task branch ({exc.code}) and unblock this card. Unblock = retry "
                                     "materializing; archive = drop the card.",
                           "why_human": "the source moved or vanished outside a lease; only a person can decide "
                                        "which copy is right",
                           "interaction": "reply", "scope": f"card {claimed.id}", "evidence": evidence[:500]})
        result.auto_blocked.append(claimed.id)
    return True
