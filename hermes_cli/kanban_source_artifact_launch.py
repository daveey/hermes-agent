"""Verify the grant's selected bytes before any source attachment or spawn."""
from __future__ import annotations

from hermes_cli import kanban_source_artifacts as artifacts
from hermes_cli.kanban_workspace_source import WorkspaceSourceRefusal


def _transport(conn, bound):
    from hermes_cli import kanban_db_sync as sync

    hub = getattr(sync._client(), "hub", None)
    if hub is None:
        raise artifacts.ArtifactRefused("artifact_unavailable", "no Board attachment transport")
    return artifacts.bounded_transport(hub, bound), sync._state(conn)["board_id"]


def verify(conn, task_id, snapshot, bound, *, predecessor_run_uid=None):
    """Incoming manifest wins over stale host selections; revision0 uses exact predecessor.

    The resolver passes the predecessor it already proved under ``bound``; recovering it
    here is the fallback for direct callers and is capped by the same budget.
    """
    try:
        if snapshot.manifest_value is not None:
            entries = snapshot.manifest().artifacts
        else:
            if predecessor_run_uid is None:
                from dataclasses import asdict
                from hermes_cli import kanban_source_registration as registration
                predecessor_run_uid = registration.predecessor_for_grant(
                    conn, task_id, snapshot.grant, asdict(snapshot.custody), bound=bound).run_uid
            entries = artifacts.selected(conn, task_id, predecessor_run_uid) or []
        if not entries:
            return
        hub, board = _transport(conn, bound)
        for entry in entries:
            bound.left()
            # Attachment rows can lag the authoritative grant. Missing metadata
            # is retryable, not proof that the manifest's digest is wrong.
            if not conn.execute("SELECT 1 FROM task_attachments WHERE uid=?", (entry["attachment_uid"],)).fetchone():
                raise artifacts.ArtifactRefused("artifact_unavailable", "selected metadata not synchronized")
            artifacts.ensure(conn, task_id, [entry], hub, board)
            bound.left()
    except WorkspaceSourceRefusal as exc:
        if exc.code != "materialize_timeout":
            raise
        raise WorkspaceSourceRefusal("artifact_unavailable", "selected transfer deadline exhausted") from exc
    except artifacts.ArtifactRefused as exc:
        code = "artifact_unavailable" if exc.code == "artifact_unavailable" else "artifact_mismatch"
        raise WorkspaceSourceRefusal(code, exc.detail) from exc
