"""Cheap checkout fingerprint (``git:<ref>:<sha>``) read from ``.git`` files, without spawning git.

Stdlib only on purpose: ``hermes_bootstrap`` imports this before any other Hermes module so the
process records the revision it actually started on (``record_process_boot_fingerprint``). The
boot-pin guard (``gateway.code_skew`` / ``gateway.boot_pinned_imports``) installs later, in
``start_gateway`` or the serve lifespan; reading the revision only then let a ``hermes update`` that
landed during startup pass as the boot commit, so lazy imports mixed revisions (agent-529f2244ac).
"""

from __future__ import annotations

import os
from pathlib import Path

# Resolved checkout root -> fingerprint read when the process started (first value wins).
_process_boot: dict[str, str | None] = {}


def read_packed_ref(common_dir: Path, ref: str) -> str | None:
    """Look up a ref in .git/packed-refs without spawning git.

    packed-refs lines look like ``<sha> <ref>`` with optional ``^<sha>``
    peel lines and ``#``-prefixed comments / ``# pack-refs with:`` header.
    """
    try:
        text = (common_dir / "packed-refs").read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if not line or line.startswith("#") or line.startswith("^"):
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[1].strip() == ref:
            return parts[0].strip()
    return None


def read_git_revision_fingerprint(repo_root: Path) -> str | None:
    """Return a cheap checkout fingerprint without spawning git."""
    git_dir = repo_root / ".git"
    try:
        if git_dir.is_file():
            for line in git_dir.read_text(encoding="utf-8-sig", errors="replace").splitlines():
                key, _, value = line.partition(":")
                if key.strip() == "gitdir" and value.strip():
                    git_dir = (repo_root / value.strip()).resolve()
                    break
        # Worktrees point HEAD at a per-worktree gitdir but pack their refs
        # in the main repo's gitdir (referenced via ``commondir``). Resolve
        # that up front so packed-refs lookups hit the right file.
        common_dir = git_dir
        commondir_file = git_dir / "commondir"
        if commondir_file.exists():
            try:
                rel = commondir_file.read_text(encoding="utf-8-sig", errors="replace").strip()
                if rel:
                    common_dir = (git_dir / rel).resolve()
            except OSError:
                pass
        head = (git_dir / "HEAD").read_text(encoding="utf-8-sig", errors="replace").strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            # Loose refs may live in the worktree gitdir OR the common dir
            # (branches created via `git worktree add` typically live in the
            # common dir's refs/heads/).
            for candidate in (git_dir, common_dir):
                ref_file = candidate / ref
                if ref_file.exists():
                    return f"git:{ref}:{ref_file.read_text(encoding='utf-8-sig', errors='replace').strip()}"
            packed_sha = read_packed_ref(common_dir, ref)
            if packed_sha:
                return f"git:{ref}:{packed_sha}"
            # Ref name is known but unresolved — still stable across launches,
            # and the version/release fallback in the caller will invalidate
            # after `hermes update`.
            return f"git:{ref}:unresolved"
        return f"git:HEAD:{head}"
    except OSError:
        return None


def _key(root) -> str:
    return os.path.realpath(str(root))


def record_process_boot_fingerprint(root) -> str | None:
    """Read ``root``'s revision once per process; later calls keep the first value."""
    key = _key(root)
    if key not in _process_boot:
        _process_boot[key] = read_git_revision_fingerprint(Path(key))
    return _process_boot[key]


def process_boot_fingerprint(root) -> str | None:
    """The revision ``record_process_boot_fingerprint`` read at process start, else None."""
    return _process_boot.get(_key(root))
