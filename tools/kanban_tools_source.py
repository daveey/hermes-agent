"""Source snapshot staging at the already-authorized lifecycle tool boundary."""
from pathlib import Path


SOURCE_ARGS = ("source_path", "artifacts")


def heartbeat_source(conn, task_id, args, *, worker_task, expected_run_id):
    """Mid-run initial registration and artifact selection for the bound worker.

    The worker terminal is fenced from Kanban CLI writes on purpose; this is the
    supported surface. Only the dispatcher-spawned worker of this exact task may
    use it (task scope + run id), never an operator or orchestrator on its behalf.
    Returns None when no source field was given (plain liveness heartbeat).
    """
    from hermes_cli.kanban_artifact_receipt import attachment_board
    from hermes_cli import kanban_source_artifacts as artifacts
    from hermes_cli import kanban_source_board as source
    from hermes_cli import kanban_source_launch as launch
    from hermes_cli import kanban_source_registration as registration

    if not any(name in args for name in SOURCE_ARGS):
        return None
    if worker_task != task_id or expected_run_id is None:
        raise ValueError("source registration needs this task's dispatcher-bound worker run")
    path, selection = args.get("source_path"), args.get("artifacts")
    if path is not None and (not isinstance(path, str) or not path.strip()):
        raise ValueError("source_path must be a non-empty path")
    if selection is not None and (not isinstance(selection, list) or not all(isinstance(p, str) for p in selection)):
        raise ValueError("artifacts must be a list of paths")
    try:
        if path is not None:
            registration.register_initial(conn, task_id, path, expected_run_id=expected_run_id)
        if selection is not None:
            artifacts.stage_for_run(conn, task_id, selection, board=attachment_board(conn, task_id),
                                    expected_run_id=expected_run_id)
    except (launch.AttachRefused, artifacts.ArtifactRefused) as exc:
        raise ValueError(f"{exc.code}: {exc.detail}") from exc
    run = source.attached_run(conn, expected_run_id)
    chosen = artifacts.selected(conn, task_id, run.run_uid) if run else None
    return {"mode": run.mode if run else None, "state": run.state if run else None,
            "path": run.path if run else None, "artifacts": len(chosen or [])}


def register_for_ending(conn, task_id, args, *, worker_task, expected_run_id):
    """Explicit first attachment in the owning worker's ending, before staging/receipt.

    No source_path means no new adoption. A refused or uncertain attach raises,
    retaining the durable observation and leaving the ending unperformed.
    """
    if "source_path" not in args:
        return
    path = args["source_path"]
    if not isinstance(path, str) or not path.strip() or not Path(path).is_absolute():
        raise ValueError("source_path must be a non-empty absolute path")
    heartbeat_source(conn, task_id, {"source_path": path},
                     worker_task=worker_task, expected_run_id=expected_run_id)


def stage_completion(conn, task_id, metadata, *, expected_run_id):
    """Called after worker ownership, foreign-card and completion gates.

    No caller run means operator completion, not permission to impersonate the
    current worker. Existing non-source tasks retain ordinary artifact staging.
    Committed snapshots survive a later refused/rolled-back ending for replay.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_artifact_receipt import attachment_board
    from hermes_cli import kanban_source_artifacts as artifacts
    from hermes_cli import kanban_source_board as source

    if expected_run_id is None or not isinstance(metadata, dict) or "artifacts" not in metadata:
        return metadata
    task = kb.get_task(conn, task_id)
    current = source.attached_run(conn, task.current_run_id) if task else None
    if current is None or current.source_protocol < 2:
        # v1 (protocol-1) worktree cards keep the legacy attachment path.
        return metadata
    try:
        entries = artifacts.stage_for_run(
            conn, task_id, metadata["artifacts"], board=attachment_board(conn, task_id),
            expected_run_id=expected_run_id,
        )
        # Only rows in this exact selection may supply the delivery path. A
        # historical task/path match alone could select an older byte snapshot.
        mapping = {}
        for entry in entries:
            row = artifacts._row(conn, task_id, entry)
            for original in conn.execute(
                "SELECT source_path FROM kanban_source_artifact_stage "
                "WHERE task_id=? AND attachment_uid=?", (task_id, entry["attachment_uid"]),
            ):
                mapping[original[0]] = row["stored_path"]
        result = dict(metadata)
        result["artifacts"] = [mapping[str(Path(path).expanduser().resolve())] for path in metadata["artifacts"]]
        return result
    except (artifacts.ArtifactRefused, OSError, KeyError) as exc:
        raise kb.ArtifactPreservationError(str(exc)) from exc
