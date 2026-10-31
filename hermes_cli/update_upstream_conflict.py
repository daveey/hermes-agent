"""Turn an unattended upstream merge conflict into one durable repair owner."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

log = logging.getLogger(__name__)
AUTHOR = "upstream-merge"


def report(cwd: Path, upstream: str, conflicts: list[str]) -> str | None:
    """Best effort: card failures must remain visible without changing update custody."""
    try:
        from hermes_cli.kanban_db_connect import connect_closing

        with connect_closing() as conn:
            return _report(conn, cwd, upstream, conflicts)
    except Exception:  # health: allow BLE001 -- escalation must not mask the original merge refusal
        log.warning("Could not file upstream merge conflict card", exc_info=True)
        print("    Warning: could not file upstream merge conflict card; see agent.log")
        return None


def _report(conn, cwd: Path, upstream: str, conflicts: list[str]) -> str:
    from hermes_cli import kanban_db as kb

    key = "upstream-merge-conflict:" + hashlib.sha256(str(cwd.resolve()).encode()).hexdigest()[:24]
    evidence = f"Upstream SHA: `{upstream}`\nConflicting paths:\n" + "\n".join(f"- `{p}`" for p in conflicts)
    # Serialize lookup + create so overlapping unattended invocations cannot create two owners.
    with kb.write_txn(conn, allow_nested=True):
        row = conn.execute(
            "SELECT id, status, body FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1", (key,),
        ).fetchone()
        if row and row[1] not in {"done", "failed", "cancelled"}:
            if evidence not in (row[2] or "") and not conn.execute(
                "SELECT 1 FROM task_comments WHERE task_id = ? AND author = ? AND body = ?",
                (row[0], AUTHOR, evidence),
            ).fetchone():
                kb.add_comment(conn, row[0], AUTHOR, evidence)
            return row[0]
        # Finished owners must not swallow a new incident; release their stable key atomically.
        if row:
            conn.execute("UPDATE tasks SET idempotency_key = NULL WHERE id = ?", (row[0],))
        return kb.create_task(
            conn, title="Resolve unattended upstream merge conflict", assignee="default", created_by=AUTHOR,
            idempotency_key=key, workspace_kind="scratch", requires=["terminal", "file", "git"],
            completion_contract="local-only",
            body=f"`hermes update` could not merge upstream into `{cwd}`. Nothing moved.\n\n{evidence}\n\n"
                 "In an isolated worktree off fresh origin/main, merge the upstream SHA, preserve fork and upstream "
                 "behavior, test the affected files, commit, then land ONLY with "
                 "`python3 scripts/ci/upstream_sync.py --head <merge sha>`. No direct main push or admin override. "
                 "Acceptance: exact-head upstream-sync full suite green, gate exits 0 LANDED, upstream SHA is "
                 "an ancestor of origin/main, and the next unattended updater no longer reports this conflict. "
                 "Remove the worktree after landing.",
        )
