"""Turn an unattended upstream merge conflict into one durable repair owner."""
from __future__ import annotations

import argparse
import hashlib
import logging
import re
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)
AUTHOR = "upstream-merge"
_RESULT = "__HERMES_UPSTREAM_CONFLICT_TASK__="
_SKIPPED = "__HERMES_UPSTREAM_CONFLICT_HUB_ONLY__"
_TIMEOUT_S = 60


def report(cwd: Path, upstream: str, conflicts: list[str]) -> str | None:
    """Best effort: card failures must remain visible without changing update custody.

    Filed in a fresh interpreter: the caller is the updater, whose ``sys.modules`` still holds the
    pre-update code while the checkout already holds the new one. Importing the kanban modules here
    mixed the two (new kanban_capabilities asking the old config_effective for a name it lacked) and
    the card was refused as a capability mismatch (kanban-c50ee42be6).
    """
    from hermes_cli._launchers import runtime_command
    from hermes_constants import get_hermes_home

    from hermes_cli._startup_fast import project_root_str
    root = Path(project_root_str())
    args = ["--cwd", str(cwd), "--upstream", upstream, *(a for p in conflicts for a in ("--conflict", p))]
    try:
        proc = subprocess.run(
            runtime_command(root, args, module="hermes_cli.update_upstream_conflict", home=get_hermes_home()), cwd=str(root),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=_TIMEOUT_S, check=False)
        found = re.findall(rf"^{re.escape(_RESULT)}(t_\w+)$", proc.stdout or "", re.MULTILINE)
        if proc.returncode == 0 and _SKIPPED in (proc.stdout or "").splitlines():
            message = "upstream integration belongs to the hub; no local repair card filed; keeping the fork-only update"
            log.info(message)
            print(f"    {message}")
            return None
        if proc.returncode == 0 and found:
            return found[-1]
        detail = f"exit {proc.returncode}: " + (proc.stderr or proc.stdout or "")[-2000:]
    except (OSError, subprocess.SubprocessError) as exc:
        detail = f"{type(exc).__name__}: {exc}"
    log.warning("Could not file upstream merge conflict card (%s)", detail)
    print("    Warning: could not file upstream merge conflict card; see agent.log")
    return None


def main(argv: list[str] | None = None) -> int:
    """Child entry point: file (or refresh) the owner card and print its id on a marker line."""
    parser = argparse.ArgumentParser(prog="hermes_cli.update_upstream_conflict")
    parser.add_argument("--cwd", required=True, type=Path)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--conflict", action="append", default=[])
    ns = parser.parse_args(argv)
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing() as conn:
        task_id = _report(conn, ns.cwd, ns.upstream, ns.conflict)
    print(f"{_RESULT}{task_id}" if task_id else _SKIPPED, flush=True)
    return 0


def _report(conn, cwd: Path, upstream: str, conflicts: list[str]) -> str | None:
    from hermes_cli import kanban_db as kb

    from hermes_cli import update_conflict_reporting as routing
    config = routing.configured()
    if config is not None:
        return routing.report(conn, config, cwd, upstream, conflicts)
    from hermes_cli.kanban_capability_registry import self_gateway
    from hermes_cli.kanban_hub_admission_placement import is_hub
    from hermes_cli.kanban_placement import config as placement_config

    # A worker's local board cannot deduplicate against the hub's integration owner.
    # Explicit observation routing above remains available; unknown identity is not authority.
    gateway = self_gateway()
    if not gateway or not is_hub(gateway, placement_config()):
        return None
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


if __name__ == "__main__":
    sys.exit(main())
