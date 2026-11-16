"""Protocol-2 scratch registration and retained-predecessor recovery.

Explicit registration is not yet CLI-wired. Dispatch uses read-only predecessor
resolution, then successor attach; scratch cwd is never mutated here.
"""
from __future__ import annotations

import math
import sqlite3
import time
from pathlib import Path
from typing import NoReturn

from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_source_board as board
from hermes_cli import kanban_source_launch as launch
from hermes_cli import kanban_workspace_source as ws
from hermes_cli.kanban_source_publish import _Deadline


def _refuse(detail: str) -> NoReturn:
    raise launch.AttachRefused("source_identity_refused", detail)


def _current(conn, task_id):
    row = conn.execute("SELECT current_run_id, workspace_kind, lease_expires FROM tasks WHERE id=?", (task_id,)).fetchone()
    if sync.board_mode(conn) != "primary" or not row or row[1] != "scratch" or not row[0]:
        _refuse("not a current primary scratch run")
    if not row[2] or row[2] <= time.time() * 1000:
        _refuse("local lease expired")
    uid = sync.run_uid(conn, row[0])
    machine = sync._state(conn)["gateway_id"]
    if not uid or not machine:
        _refuse("missing run or machine identity")
    return int(row[0]), uid, machine


def _source_owner(conn, task_id, path) -> str | None:
    """Another live task whose durable source observation names ``path``.

    Scratch registration keeps ``tasks.workspace_path`` at the scratch cwd, so the external
    worktree's owner is only in the local source journal. A definitely refused observation
    attached nothing and owns nothing; pending, attached and retained ones keep the path.
    """
    try:
        row = conn.execute("SELECT r.task_id FROM kanban_source_runs r JOIN tasks t ON t.id = r.task_id "
                           "WHERE r.path = ? AND r.task_id != ? AND r.state != 'refused' "
                           "AND t.status NOT IN ('done', 'archived') LIMIT 1", (path, task_id)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def _owned_elsewhere(conn, task_id, path) -> None:
    from hermes_cli.kanban_source_resolver import _occupied_by_other_task
    if _occupied_by_other_task(conn, task_id, path) is not None:
        _refuse("source belongs to another active task")


def _git(bound, path, *args):
    """Read-only git, capped by the caller's materialize budget when one is given."""
    if bound is None:
        return launch._git(str(path), *args)
    out = bound.git(path, *args, check=False)
    return out.stdout.strip() if out.returncode == 0 else None


class _BudgetDeadline(_Deadline):
    """The resolver's materialize budget in the publisher's proof-deadline shape."""

    def __init__(self, bound):
        self.bound = bound

    def left(self, cap: float = 60.0) -> float:
        return min(self.bound.left(), cap)


def _local_path(conn, task_id, path, bound=None):
    dest = Path(path).resolve()
    _owned_elsewhere(conn, task_id, str(dest))
    if not (dest / ".git").is_file():
        _refuse("source must be a linked worktree (git -C <configured clone> worktree add), not a clone")
    branch = _git(bound, dest, "symbolic-ref", "-q", "--short", "HEAD")
    task = conn.execute("SELECT branch_name FROM tasks WHERE id=?", (task_id,)).fetchone()
    expected = (task[0] or "").strip() if task else ""
    if branch != (expected or f"wt/{task_id}"):
        _refuse(f"worktree branch {branch or '(detached)'} is not this task's branch {expected or f'wt/{task_id}'}")
    registered = False
    for clone in ws.settings().clones.values():
        listing = _git(bound, clone, "worktree", "list", "--porcelain")
        if listing and any(line == f"worktree {dest}" for line in listing.splitlines()):
            registered = True
    if not registered:
        _refuse("worktree is not a linked worktree of any clone in kanban.workspace_source.clones")
    head = _git(bound, dest, "rev-parse", "--verify", "HEAD")
    if not head:
        _refuse("worktree has no readable HEAD")
    return str(dest), branch, head


def recover_local(conn: sqlite3.Connection, task_id: str, identity: dict, *, bound=None) -> board.AttachedRun:
    """Locate exact durable observation; never use custody.attached_run_uid or latest sorting."""
    machine = sync._state(conn)["gateway_id"]
    if not isinstance(identity, dict) or identity.get("task_id") != task_id or identity.get("machine") != machine:
        _refuse("foreign predecessor")
    try:
        rows = conn.execute("SELECT run_id FROM kanban_source_runs WHERE task_id=? AND run_uid=?",
                            (task_id, identity.get("run_uid"))).fetchall()
    except sqlite3.OperationalError:
        _refuse("no durable source observation")
    if len(rows) != 1:
        _refuse("unknown or ambiguous predecessor")
    run = board.attached_run(conn, rows[0][0])
    if run is None or run.machine != machine or run.source_protocol != 2 or run.manifest_revision != 0 or not run.path:
        _refuse("observation is not this machine's premanifest protocol-2 source")
    _local_path(conn, task_id, run.path, bound)
    validate_identity(run, bound=bound)
    return run


def source_identity(path: str, source_kind: str, *, bound=None) -> dict:
    """Reuse publisher clone proof; bind a local observation to its repository and common-dir.

    With ``bound`` (the resolver's materialize budget) every proof call is capped by what is left of it, and a
    spent budget raises the resolver's ``materialize_timeout`` rather than an identity refusal.
    """
    from hermes_cli import kanban_source_publish as publish
    try:
        clone = publish.prove_origin(path, ws.settings(),
                                     deadline=_BudgetDeadline(bound) if bound is not None else None)
    except publish.PublishRefused as exc:
        _refuse(f"source repository proof failed: {exc}")
    return identity_from_clone(clone, source_kind)


def identity_from_clone(clone, source_kind: str) -> dict:
    return {"repository_id": clone.repository_id, "full_name": clone.full_name,
            "common_dir": str((Path(clone.path) / ".git").resolve()), "source_kind": source_kind}


def match_manifest(identity: dict, manifest: dict) -> None:
    repo = manifest["repository"]
    if (identity["repository_id"] != repo["id"] or identity["full_name"] != repo["full_name"]
            or identity["source_kind"] != manifest["source_kind"]):
        _refuse("source repository or kind differs from manifest")


def validate_identity(run: board.AttachedRun, *, current=None, bound=None) -> dict:
    expected_kind = (run.manifest or {}).get("source_kind") or (run.source_identity or {}).get("source_kind", "scratch_external")
    if not run.path:
        _refuse("source observation has no path")
    if current is None:
        current = source_identity(run.path, expected_kind, bound=bound)
    if run.source_identity != current:
        _refuse("source repository, common-dir or kind changed since observation")
    if run.manifest:
        match_manifest(current, run.manifest)
    return current


def _initial_lane(conn, task_id, run_id):
    from hermes_cli import kanban_db as kb
    if kb._retry_status_for_run(conn, task_id, run_id) != "ready":
        _refuse("initial requires an implementation claim")


def _persist_observation(conn, run: board.AttachedRun, *, predecessor=None) -> board.AttachedRun:
    """Serialize first observation with local lease and immutable identity checks.

    Git reads happen before this transaction; never hold the database writer over Git or RPC.
    Replaying an identity keeps the first head and state rather than overwriting its evidence.
    """
    from hermes_cli.kanban_db_connect import write_txn
    with write_txn(conn):
        if _current(conn, run.task_id) != (run.run_id, run.run_uid, run.machine):
            _refuse("local run changed during source observation")
        if run.mode == "initial":
            _initial_lane(conn, run.task_id, run.run_id)
        # Recheck ownership under the writer lock so two registrations of one path serialize here. This runs
        # on every observation, replays included: a definitely refused row owns nothing, so another task may
        # have registered the path since, and reopening it without the check would attach both.
        _owned_elsewhere(conn, run.task_id, run.path)
        prior = board.attached_run(conn, run.run_id)
        if prior is None:
            board.record_run(conn, run, allow_nested=True)
            prior = run
        elif prior.state == "refused":
            # Reopen in the same transaction as the ownership check, so a replay never looks refused (and
            # never owns nothing) while its attach RPC may still apply.
            conn.execute("UPDATE kanban_source_runs SET state = 'pending' WHERE run_id = ? AND run_uid = ? "
                         "AND state = 'refused'", (int(run.run_id), run.run_uid))
            prior = board.attached_run(conn, run.run_id) or prior
        fields = ("task_id", "run_uid", "machine", "path", "branch", "mode", "source_protocol", "manifest_revision",
                  "source_identity")
        if any(getattr(prior, field) != getattr(run, field) for field in fields):
            _refuse("current run already has a different observation")
        if predecessor is not None:
            from hermes_cli import kanban_source_artifacts as artifacts
            try:
                artifacts.inherit(conn, run.task_id, predecessor.run_uid, run.run_uid)
            except artifacts.ArtifactRefused as exc:
                raise launch.AttachRefused(exc.code, exc.detail) from exc
        return prior


def _attach_settled(conn, run: board.AttachedRun, mode: str) -> None:
    """Send the protocol-2 attach for a durable observation and settle it (M3).

    ``_persist_observation`` has already reopened a definitely refused observation to ``pending`` under the
    ownership check, so an unanswered replay never looks refused. Only a refusal proving nothing is attached
    settles ``refused``; the row is kept as evidence. Uncertain refusals leave it ``pending``: an earlier
    unanswered attach of this run may have applied.
    """
    try:
        board.attach(conn, run.task_id, run.run_uid, mode=mode, manifest_revision=0, source_protocol=2)
    except board.SourceCallRefused as exc:
        definite = exc.code in board.NOT_ATTACHED
        if definite:
            board.settle_state(conn, run.run_id, run.run_uid, "pending", "refused")
        raise launch.AttachRefused(exc.code, exc.detail, uncertain=not definite) from exc
    board.update_run(conn, run.run_id, state="attached")


def register_initial(conn: sqlite3.Connection, task_id: str, source_path: str, *,
                     expected_run_id: int | None = None) -> board.AttachedRun:
    """Explicit mid-run registration, durable before RPC; same-run replay uses that observation.

    A tool surface passes its dispatcher-bound run; the observation transaction
    rechecks that the same run is still current, so the binding holds to commit.
    """
    if not ws.settings().enabled:
        raise launch.AttachRefused("source_disabled", "kanban.workspace_source is disabled on this host")
    run_id, uid, machine = _current(conn, task_id)
    if expected_run_id is not None and run_id != expected_run_id:
        _refuse("caller run is not the task's current run")
    _initial_lane(conn, task_id, run_id)
    path, branch, head = _local_path(conn, task_id, source_path)
    identity = source_identity(path, "scratch_external")
    prior = board.attached_run(conn, run_id)
    if prior is not None:
        if (prior.task_id, prior.run_uid, prior.machine, prior.path, prior.mode, prior.source_protocol) != (
                task_id, uid, machine, path, "initial", 2):
            _refuse("current run already has a different observation")
        if prior.source_identity != identity or prior.branch != branch:
            _refuse("source identity changed since initial observation")
        run = prior
    else:
        run = board.AttachedRun(run_id, task_id, uid, "initial", 0, branch, path, head, None, "pending", machine, 2,
                                identity)
    run = _persist_observation(conn, run)
    _attach_settled(conn, run, "initial")
    return board.attached_run(conn, run_id)


def predecessor_for_grant(conn: sqlite3.Connection, task_id: str, grant: dict, custody: dict, *,
                          bound=None) -> board.AttachedRun:
    """Read-only recovery, bound to the exact current grant and its retained custody."""
    _run_id, uid, machine = _current(conn, task_id)
    if not isinstance(grant, dict):
        _refuse("missing authoritative grant envelope")
    lease = grant.get("lease")
    if not isinstance(lease, dict):
        _refuse("missing authoritative lease")
    if (grant.get("status") != "applied" or lease.get("task_id") != task_id or lease.get("run_uid") != uid
            or lease.get("holder") != machine or not isinstance(lease.get("expires"), (int, float))
            or not math.isfinite(lease["expires"]) or lease["expires"] <= time.time() * 1000):
        _refuse("grant does not carry this run's current live lease")
    if (custody.get("source_protocol") != 2 or custody.get("manifest_revision") != 0
            or custody.get("source_holder") != machine or custody.get("state") != "retained"):
        _refuse("not same-holder retained premanifest custody")
    previous = recover_local(conn, task_id, grant.get("source_predecessor"), bound=bound)
    if custody.get("retained_run_uid") != previous.run_uid:
        _refuse("grant predecessor does not match retained custody")
    if previous.run_uid == uid:
        _refuse("predecessor is the current run")
    return previous


def resume_premanifest(conn: sqlite3.Connection, task_id: str, grant: dict, custody: dict) -> board.AttachedRun:
    """Recover and attach one successor from the exact grant, preserving scratch cwd."""
    previous = predecessor_for_grant(conn, task_id, grant, custody)
    run_id, uid, machine = _current(conn, task_id)
    prior = board.attached_run(conn, run_id)
    if prior is not None:
        if (prior.task_id, prior.run_uid, prior.machine, prior.path, prior.mode, prior.source_protocol) != (
                task_id, uid, machine, previous.path, "resume", 2):
            _refuse("successor already has a different observation")
        if prior.source_identity != previous.source_identity:
            _refuse("successor identity differs from predecessor")
    else:
        seen = launch.observe(previous.path, previous.branch)
        prior = board.AttachedRun(run_id, task_id, uid, "resume", 0, previous.branch,
                                  previous.path, seen["head"], None, "pending", machine, 2, previous.source_identity)
    _persist_observation(conn, prior, predecessor=previous)
    _attach_settled(conn, prior, "resume")
    return board.attached_run(conn, run_id)
