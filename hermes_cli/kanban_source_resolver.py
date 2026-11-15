"""The workspace_source resolver (plan v3 §6/§6b, v4 S7/R4b, build decision Q3): turn a grant snapshot into a local
worktree at the manifest's exact branch and commit, or refuse with a coded :class:`WorkspaceSourceRefusal`.

Never resets, forces or prunes; never reads or writes ``refs/remotes/origin/*`` (the fetch lands in
``refs/hermes/source/<task>``). Bounded by ``materialize_timeout_s`` (host-scope ``materialize_timeout`` past it).
"""

from __future__ import annotations

import socket
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_cli import kanban_source_clones as clones
from hermes_cli.kanban_workspace_source import (
    PORTABLE_STATES, Manifest, ManifestInvalid, Settings, SourceSnapshot, WorkspaceSourceRefusal)
from hermes_cli import kanban_workspace_source as _kws


@dataclass(frozen=True)
class ResolvedSource:
    path: str
    branch: str  # short name
    commit: Optional[str]
    manifest_revision: Optional[int]
    mode: str  # holder_reuse | return_home | materialized
    repository: Optional[str] = None
    source_kind: Optional[str] = None

    def event_payload(self, *, holder: Optional[str], run_uid: Optional[str]) -> dict:
        return {"hostname": socket.gethostname(), "holder": holder, "run_uid": run_uid, "repository": self.repository,
                "branch": self.branch, "commit": self.commit, "manifest_revision": self.manifest_revision,
                "path": self.path, "source_kind": self.source_kind, "mode": self.mode}


class _Bound:
    """The materialize deadline: every git call is capped by what is left of it."""

    def __init__(self, seconds: float, clock: Callable[[], float]):
        self.clock, self.deadline, self.seconds = clock, clock() + seconds, seconds

    def left(self) -> float:
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise WorkspaceSourceRefusal("materialize_timeout", f"source not ready within {self.seconds:.0f}s")
        return remaining

    def git(self, path: str | Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        try:
            return clones.git(path, *args, timeout=min(self.left(), 60.0), check=check)
        except TimeoutError as exc:
            raise WorkspaceSourceRefusal("materialize_timeout", str(exc)) from exc


def _head(bound: _Bound, path: str | Path) -> Optional[str]:
    out = bound.git(path, "rev-parse", "--verify", "-q", "HEAD", check=False)
    return out.stdout.strip() or None


def _is_ancestor(bound: _Bound, path: str | Path, older: str, newer: str) -> bool:
    return bound.git(path, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def _worktrees(bound: _Bound, clone: str) -> list[dict[str, str]]:
    """``git worktree list --porcelain`` of ``clone`` as ``[{worktree, HEAD, branch}]``."""
    out, entries, cur = bound.git(clone, "worktree", "list", "--porcelain").stdout, [], {}
    for line in out.splitlines():
        if not line.strip():
            if cur:
                entries.append(cur)
            cur = {}
            continue
        key, _, value = line.partition(" ")
        cur[key] = value
    if cur:
        entries.append(cur)
    return entries


def _occupied_by_other_task(conn: sqlite3.Connection, task_id: str, path: str) -> Optional[str]:
    row = conn.execute("SELECT id FROM tasks WHERE workspace_path = ? AND id != ? "
                       "AND status NOT IN ('done', 'archived') LIMIT 1", (path, task_id)).fetchone()
    return row[0] if row else None


def _clean(bound: _Bound, path: str | Path) -> bool:
    return not bound.git(path, "status", "--porcelain", "--untracked-files=all").stdout.strip()


def _bring_to(bound: _Bound, conn, task_id: str, dest: str, manifest: Manifest) -> None:
    """An existing worktree of the clone with B checked out: clean, unclaimed, then reuse or fast-forward."""
    if (other := _occupied_by_other_task(conn, task_id, dest)) is not None:
        raise WorkspaceSourceRefusal("occupied_path", f"{dest} is {other}'s live workspace")
    if not _clean(bound, dest):
        raise WorkspaceSourceRefusal("dirty_destination", f"{dest} has local changes")
    head = _head(bound, dest)
    if head == manifest.commit:
        return
    if head is not None and _is_ancestor(bound, dest, head, manifest.commit):
        bound.git(dest, "merge", "--ff-only", "--no-edit", manifest.commit)
        return
    raise WorkspaceSourceRefusal("diverged_local_branch", f"{dest} HEAD {head} is not an ancestor of {manifest.commit}")


def materialize(conn: sqlite3.Connection, task_id: str, manifest: Manifest, cfg: Settings, bound: _Bound, *,
                lookup: Optional[Callable[[str], dict]] = None) -> ResolvedSource:
    clone = clones.clone_for(manifest.repository_id, cfg, lookup=lookup)  # proof re-run: wrong_repo, ...
    bound.left()
    remote = bound.git(clone.path, "ls-remote", "origin", manifest.branch, check=False)
    if remote.returncode != 0:
        raise WorkspaceSourceRefusal("origin_unreachable", f"ls-remote origin {manifest.branch}: {remote.stderr.strip()}")
    tips = [line.split("\t") for line in remote.stdout.splitlines() if line.endswith("\t" + manifest.branch)]
    if not tips:
        raise WorkspaceSourceRefusal("remote_branch_missing", f"{manifest.full_name} has no {manifest.branch}",
                                     evidence={"branch": manifest.branch, "commit": manifest.commit})
    if tips[0][0] != manifest.commit:
        raise WorkspaceSourceRefusal("remote_drift", f"{manifest.branch} is at {tips[0][0]}, manifest says "
                                     f"{manifest.commit}", evidence={"remote": tips[0][0], "commit": manifest.commit})
    ref = f"refs/hermes/source/{task_id}"
    # ``--refmap=`` turns off git's opportunistic remote-tracking update: only the task ref is written (S4).
    bound.git(clone.path, "fetch", "--no-tags", "--refmap=", "origin", f"+{manifest.branch}:{ref}")
    if bound.git(clone.path, "rev-parse", "--verify", "-q", ref).stdout.strip() != manifest.commit:
        raise WorkspaceSourceRefusal("remote_drift", f"{manifest.branch} moved during the fetch")
    bound.git(clone.path, "cat-file", "-e", f"{manifest.commit}^{{commit}}")
    base: dict[str, Any] = dict(branch=manifest.short_branch, commit=manifest.commit,
                                manifest_revision=manifest.manifest_revision, repository=manifest.full_name,
                                source_kind=manifest.source_kind)
    trees = _worktrees(bound, clone.path)
    if (home := next((t["worktree"] for t in trees if t.get("branch") == manifest.branch), None)) is not None:
        _bring_to(bound, conn, task_id, home, manifest)
        return ResolvedSource(path=home, mode="return_home", **base)
    local = bound.git(clone.path, "rev-parse", "--verify", "-q", manifest.branch, check=False).stdout.strip()
    if not local:
        bound.git(clone.path, "branch", "--no-track", manifest.short_branch, manifest.commit)
    elif local != manifest.commit:
        if not _is_ancestor(bound, clone.path, local, manifest.commit):
            raise WorkspaceSourceRefusal("diverged_local_branch", f"local {manifest.branch} {local} is not an "
                                         f"ancestor of {manifest.commit}")
        bound.git(clone.path, "update-ref", manifest.branch, manifest.commit, local)
    # A nested checkout inherits ancestor build configuration (e.g. Nim's config.nims)
    # from the clone's unrelated HEAD. Keep new trees beside, never inside, the clone.
    target = str(Path(clone.path).parent / ".worktrees" / str(manifest.repository_id) / task_id)
    if Path(target).exists():
        raise WorkspaceSourceRefusal("occupied_path", f"{target} already exists")
    if (other := _occupied_by_other_task(conn, task_id, target)) is not None:
        raise WorkspaceSourceRefusal("occupied_path", f"{target} is {other}'s live workspace")
    bound.git(clone.path, "worktree", "add", target, manifest.short_branch)
    return ResolvedSource(path=target, mode="materialized", **base)


def _local_holder_copy(bound: _Bound, task: Any, manifest: Optional[Manifest], cfg: Settings) -> Optional[str]:
    """The holder's own copy: by ``manifest.branch`` in any registered clone's worktrees (S3), else the card's local
    ``workspace_path`` (with no manifest that is the only locator, S7)."""
    if manifest is not None:
        for path in cfg.clones.values():
            if not Path(path).is_dir():
                continue
            try:
                trees = _worktrees(bound, path)
            except subprocess.CalledProcessError:
                continue
            if (hit := next((t["worktree"] for t in trees if t.get("branch") == manifest.branch), None)) is not None:
                return hit
    ws = getattr(task, "workspace_path", None)
    if ws and Path(ws).is_dir():
        if manifest is None:
            return ws
        branch = bound.git(ws, "symbolic-ref", "-q", "HEAD", check=False).stdout.strip()
        if branch == manifest.branch:
            return ws
    return None


def resolve_source_workspace(conn: sqlite3.Connection, task: Any, snapshot: SourceSnapshot, *, me: str,
                             cfg: Optional[Settings] = None, ttl_s: Optional[float] = None,
                             lookup: Optional[Callable[[str], dict]] = None,
                             clock: Callable[[], float] = time.monotonic) -> ResolvedSource:
    """Resolve a card whose GRANT snapshot carries custody or a manifest (never the replica). Raises
    :class:`WorkspaceSourceRefusal`; the caller releases (host scope) or releases and blocks (card scope)."""
    cfg = cfg or _kws.settings()
    if not cfg.enabled:
        raise WorkspaceSourceRefusal("source_disabled", "kanban.workspace_source is disabled on this host")
    bound = _Bound(cfg.materialize_bound(ttl_s), clock)
    resolved = _resolve_source_workspace(conn, task, snapshot, me=me, cfg=cfg, bound=bound, lookup=lookup)
    if snapshot.manifest_value is not None or (snapshot.custody and snapshot.custody.source_protocol == 2):
        from hermes_cli.kanban_source_artifact_launch import verify
        from hermes_cli.kanban_source_launch import AttachRefused
        try:
            verify(conn, task.id, snapshot, bound)
        except AttachRefused as exc:
            raise WorkspaceSourceRefusal("source_identity_refused", exc.detail) from exc
    return resolved


def _resolve_source_workspace(conn, task, snapshot, *, me, cfg, bound, lookup):
    manifest = None
    if snapshot.manifest_value is not None:
        try:
            manifest = snapshot.manifest()
        except ManifestInvalid as exc:
            raise WorkspaceSourceRefusal("manifest_invalid", str(exc)) from exc
    custody = snapshot.custody
    if manifest is None and custody is not None and custody.source_protocol == 2:
        from dataclasses import asdict
        from hermes_cli import kanban_source_registration as registration
        from hermes_cli.kanban_source_launch import AttachRefused
        try:
            previous = registration.predecessor_for_grant(conn, task.id, snapshot.grant, asdict(custody))
        except AttachRefused as exc:
            raise WorkspaceSourceRefusal("source_identity_refused", exc.detail) from exc
        return ResolvedSource(path=previous.path, branch=previous.branch, commit=None,
                              manifest_revision=0, mode="resume", source_kind="scratch_external")
    if custody is not None and custody.source_holder == me:
        local = _local_holder_copy(bound, task, manifest, cfg)
        if local is not None:
            if manifest is not None:
                head = _head(bound, local)
                if head != manifest.commit and not (head and _is_ancestor(bound, local, manifest.commit, head)):
                    raise WorkspaceSourceRefusal("diverged_local_branch",
                                                 f"{local} HEAD {head} neither is nor descends from {manifest.commit}")
            branch = (manifest.short_branch if manifest else
                      (getattr(task, "branch_name", None) or f"wt/{getattr(task, 'id', '')}"))
            return ResolvedSource(path=local, branch=branch, commit=manifest.commit if manifest else None,
                                  manifest_revision=manifest.manifest_revision if manifest else None,
                                  mode="holder_reuse", repository=manifest.full_name if manifest else None,
                                  source_kind=manifest.source_kind if manifest else None)
        if manifest is None or custody.state not in PORTABLE_STATES:
            raise WorkspaceSourceRefusal("custody_source_missing", "this host holds custody but its copy is gone",
                                         evidence={"state": custody.state, "workspace_path": getattr(
                                             task, "workspace_path", None)})
        return materialize(conn, task.id, manifest, cfg, bound, lookup=lookup)
    if manifest is None or custody is None or custody.state not in PORTABLE_STATES:
        raise WorkspaceSourceRefusal("custody_not_portable", f"custody {custody.state if custody else 'missing'} "
                                     f"held by {custody.source_holder if custody else None}")
    if custody.manifest_revision != manifest.manifest_revision:
        raise WorkspaceSourceRefusal("custody_not_portable", "custody and manifest revisions disagree")
    return materialize(conn, task.id, manifest, cfg, bound, lookup=lookup)
