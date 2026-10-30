"""Archive-then-remove for aged terminal scratch workspaces holding git clones. [daveey]

Completion cleanup and ``gc_task_scratch`` keep every scratch tree that holds
Git metadata, so a worker's clone survives its card forever. Past
``kanban.worktree_retention_days`` this module saves each standalone repo's
unique work under ``<kanban home>/archive/scratch/<board>/<task>/<relpath>/``:

* ``commits.bundle``: refs and HEAD not reachable from the repo's
  remote-tracking refs (omitted when there are none), verified before use;
* ``tracked.diff``: ``git diff --binary HEAD`` (omitted when empty);
* ``untracked.tar``: ``git ls-files --others --exclude-standard`` (omitted when
  empty);
* ``meta.json``: remotes, HEAD, branch, task id, completed_at.

Anything it cannot prove safe keeps the workspace: a linked worktree or
submodule (a ``.git`` FILE: the worktree GC owns those), a bare repository,
any scan error, or any failed archive step. Symlinks are recorded, never
followed.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_GIT_TIMEOUT = 600


class ArchiveRefused(Exception):
    """The tree must be kept; the message says why."""


def find_repos(root: Path) -> list[Path]:
    """Standalone repos (a ``.git`` directory) below *root*, *root* included.

    Raises :class:`ArchiveRefused` for a linked worktree/submodule, a bare
    repository, a non-directory root, or any scan error. Never follows
    symlinks and never descends into a ``.git`` directory.
    """
    repos: list[Path] = []
    pending = [root]
    try:
        if not stat.S_ISDIR(root.lstat().st_mode):
            raise ArchiveRefused(f"{root} is not a directory")
        while pending:
            directory = pending.pop()
            names: set[str] = set()
            with os.scandir(directory) as entries:
                for entry in entries:
                    names.add(entry.name)
                    mode = entry.stat(follow_symlinks=False).st_mode
                    if entry.name == ".git":
                        if stat.S_ISDIR(mode):
                            repos.append(Path(directory))
                            continue
                        raise ArchiveRefused(
                            f"linked worktree or submodule at {entry.path} (left to the worktree GC)")
                    if stat.S_ISDIR(mode):
                        pending.append(Path(entry.path))
            if ".git" not in names and {"HEAD", "objects"} <= names and names & {"refs", "reftable"}:
                raise ArchiveRefused(f"bare repository at {directory}")
    except OSError as exc:
        raise ArchiveRefused(f"scan uncertain: {exc!r}") from exc
    return sorted(repos)


def _env(repo: Path) -> dict[str, str]:
    from hermes_cli._subprocess_compat import noninteractive_repo_git_env
    env = noninteractive_repo_git_env(repo)
    if env is None:
        raise ArchiveRefused(f"cannot harden git for {repo} (filter discovery failed)")
    return env


def _git(repo: Path, env: dict[str, str], *args: str, ok=(0,)) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, timeout=_GIT_TIMEOUT,
        check=False, stdin=subprocess.DEVNULL, env=env,
    )
    if proc.returncode not in ok:
        err = proc.stderr.decode("utf-8", "replace").strip()[:500]
        raise ArchiveRefused(f"git {' '.join(args[:3])} failed in {repo} (rc {proc.returncode}): {err}")
    return proc


def _text(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout.decode("utf-8", "replace").strip()


def archive_repo(repo: Path, dest: Path, *, task_id: str, completed_at: Optional[int]) -> dict:
    """Write one repo's archive into *dest* (created). Raises on any failure."""
    env = _env(repo)
    dest.mkdir(parents=True, exist_ok=True)  # the root repo archives into the fresh partial dir
    head = _text(_git(repo, env, "rev-parse", "--verify", "-q", "HEAD^{commit}", ok=(0, 1))) or None
    branch = _text(_git(repo, env, "symbolic-ref", "-q", "--short", "HEAD", ok=(0, 1))) or None
    remotes: dict[str, str] = {}
    for line in _text(_git(repo, env, "remote", "-v")).splitlines():
        parts = line.split()
        if len(parts) >= 2:
            remotes.setdefault(parts[0], parts[1])

    # Commits not reachable from any remote-tracking ref.
    refs = [r for r in _text(_git(repo, env, "for-each-ref", "--format=%(refname)")).splitlines()
            if r and not r.startswith("refs/remotes/")]
    if head:
        refs.append("HEAD")
    unpushed = [r for r in refs
                if _text(_git(repo, env, "rev-list", "-n1", r, "--not", "--remotes"))]
    files = ["meta.json"]
    if unpushed:
        bundle = dest / "commits.bundle"
        _git(repo, env, "bundle", "create", str(bundle), *unpushed, "--not", "--remotes")
        _git(repo, env, "bundle", "verify", str(bundle))
        files.append(bundle.name)

    base = head or _text(_git(repo, env, "hash-object", "-t", "tree", "--stdin"))
    diff = _git(repo, env, "diff", "--binary", "--no-ext-diff", "--no-textconv", "--no-color", base).stdout
    if diff:
        (dest / "tracked.diff").write_bytes(diff)
        files.append("tracked.diff")

    raw = _git(repo, env, "ls-files", "--others", "--exclude-standard", "-z").stdout
    untracked = [os.fsdecode(p) for p in raw.split(b"\0") if p]
    if untracked:
        with tarfile.open(dest / "untracked.tar", "w") as tar:
            for rel in untracked:
                # Embedded repos are listed as directories; they are archived on their own.
                tar.add(str(repo / rel.rstrip("/")), arcname=rel.rstrip("/"), recursive=False)
        files.append("untracked.tar")

    meta = {
        "task_id": task_id, "completed_at": completed_at, "source": str(repo),
        "head": head, "branch": branch, "remotes": remotes, "unpushed_refs": unpushed,
        "untracked_count": len(untracked), "files": files, "archived_at": int(time.time()),
    }
    (dest / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return meta


def archive_workspace(workspace: Path, archive_dir: Path, *, task_id: str,
                      completed_at: Optional[int]) -> Path:
    """Archive every standalone repo in *workspace* to *archive_dir*; return the final dir.

    Built in a ``.partial`` sibling and renamed into place, so a failure leaves
    no half archive that looks complete. Raises :class:`ArchiveRefused` (or
    ``OSError``) on any failure; the caller then keeps the workspace.
    """
    repos = find_repos(workspace)
    if not repos:
        raise ArchiveRefused("no standalone git repository found")
    partial = archive_dir.with_name(archive_dir.name + f".partial-{os.getpid()}")
    shutil.rmtree(partial, ignore_errors=True)
    try:
        partial.mkdir(parents=True)
        for repo in repos:
            rel = repo.relative_to(workspace)
            archive_repo(repo, partial / rel if rel.parts else partial,
                         task_id=task_id, completed_at=completed_at)
        final = archive_dir
        if final.exists():
            final = archive_dir.with_name(f"{archive_dir.name}-{int(time.time())}")
        partial.rename(final)
        return final
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
