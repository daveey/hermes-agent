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


# Capsule compatibility is stricter than the ordinary restart-ignore heuristic.
# A missing/dirty initial observation is sticky for this process, never adopted late.
_capsule_boot: dict[str, str | None] = {}
# Boot captures still running: the status read started at bootstrap, its result is not in yet.
_capsule_capturing: dict[str, object] = {}


def _capsule_clean_sha(root) -> str | None:
    """Known, clean, quiescent Git revision, or unknown; no positive-result cache.

    Bracket the bounded status read with HEAD reads. This is an operational update
    fence, not protection against a same-uid attacker rewriting and restoring code.
    """
    started = _capsule_status_start(root)
    return _capsule_status_finish(started) if started is not None else None


def _capsule_status_start(root):
    """Read HEAD, check the quiescence markers and start the status process; None is unknown."""
    import re
    import subprocess
    import threading

    root = Path(_key(root))
    before = read_git_revision_fingerprint(root)
    sha = (before or '').rsplit(':', 1)[-1]
    if re.fullmatch(r'[0-9a-f]{40}', sha) is None:
        return None
    try:
        git_dir = root / '.git'
        if git_dir.is_file():
            text = git_dir.read_text(encoding='utf-8-sig').strip()
            if not text.startswith('gitdir: '):
                return None
            git_dir = (root / text[len('gitdir: '):]).resolve()
        if any((git_dir / name).exists() for name in (
                'index.lock', 'HEAD.lock', 'MERGE_HEAD', 'rebase-merge', 'rebase-apply', 'CHERRY_PICK_HEAD')):
            return None
        # Read at most one byte: any output means dirty. A timer bounds even a
        # Git process which never closes stdout; no arbitrary status buffer.
        process = subprocess.Popen(
            ['git', '-C', str(root), 'status', '--porcelain=v1', '-z', '--untracked-files=normal'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={'PATH': os.defpath, 'GIT_OPTIONAL_LOCKS': '0', 'GIT_NO_LAZY_FETCH': '1',
                 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull})
        timer = threading.Timer(2, process.kill)
        timer.start()
        return root, before, sha, process, timer
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _capsule_status_finish(started) -> str | None:
    """Wait for a started status read and bracket it with the second HEAD read."""
    import subprocess

    root, before, sha, process, timer = started
    try:
        with process:
            try:
                dirty = process.stdout.read(1)
                if dirty:
                    process.kill()
                code = process.wait(timeout=2)
            finally:
                timer.cancel()
        return sha if not dirty and code == 0 and read_git_revision_fingerprint(root) == before else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def record_capsule_boot_identity(root) -> None:
    """Bootstrap-only capture; repeated calls cannot repair an unknown/dirty boot."""
    key = _key(root)
    if key not in _capsule_boot and key not in _capsule_capturing:
        fingerprint = process_boot_fingerprint(root)
        sha = _capsule_clean_sha(root)
        _capsule_boot[key] = sha if fingerprint and fingerprint.rsplit(':', 1)[-1] == sha else None


def begin_capsule_boot_identity(root) -> None:
    """Start the bootstrap capture now and let the launch continue while Git reads the tree.

    HEAD is read, the quiescence markers are checked and the status process is started on
    this thread, before dependencies or application code load, exactly as
    ``record_capsule_boot_identity`` does. Only waiting for Git moves: readers join the
    capture, and one that cannot finish leaves the boot unknown for this process.
    """
    import threading

    key = _key(root)
    if key in _capsule_boot or key in _capsule_capturing:
        return
    fingerprint = process_boot_fingerprint(root)
    pid = os.getpid()
    started = _capsule_status_start(root)

    def capture():
        sha = _capsule_status_finish(started) if started is not None else None
        if os.getpid() == pid:
            _capsule_boot.setdefault(key, sha if fingerprint and fingerprint.rsplit(':', 1)[-1] == sha else None)

    thread = threading.Thread(target=capture, name='capsule-boot-identity', daemon=True)
    _capsule_capturing[key] = thread
    thread.start()


def _capsule_boot_sha(key) -> str | None:
    thread = _capsule_capturing.get(key)
    if thread is not None:
        thread.join()
        # A capture that did not record (a fork lost its thread) is unknown, never retried.
        _capsule_boot.setdefault(key, None)
        _capsule_capturing.pop(key, None)
    return _capsule_boot.get(key)


def capsule_code_sha(root) -> str | None:
    """Strict boot/disk identity for capsule launches; absence is not proof."""
    boot = _capsule_boot_sha(_key(root))
    return boot if boot and _capsule_clean_sha(root) == boot else None
