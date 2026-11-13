"""[daveey] Checkout paths no running Hermes process loads: a checkout move over only these owes no restart.

hermes-hub applies every main commit within minutes (instant core updates), and each apply restarted the gateway
and every serve backend: 78 restarts in 3 h for 129 commits on 2026-11-13, while 28 of those commits touched only
tests, docs or CI (t_01a1d471b754e5bc). A running process never imports or reads those files, so a checkout that
moved past its boot commit by such commits only is still running the code on disk.

``RESTART_IGNORE_GLOBS`` is the one list for that decision. ``gateway.code_skew.detect_code_skew`` reads such a
drift as no skew (no "Restart required", no skew retirement), and hermes-sync reads this tuple from the
checkout's HEAD (``git show HEAD:hermes_cli/restart_ignore_paths.py``, parsed as a literal, so keep it a plain
tuple of strings) to fast-forward such a range without the gateway/serve restart. Its naming mirrors hermes-sync's
``DASHBOARD_IGNORE_GLOBS``; that list is broader (``**/*.md``) and covers hermes-mods plugin trees, not core.

The list is exact on purpose. Never add ``*.md`` in general: SKILL.md, AGENTS.md, SOUL.md and prompt files are
runtime inputs. Patterns are repo-relative: ``<dir>/**`` is everything under that top-level directory, any other
pattern matches a top-level file name only (``fnmatch``, case-sensitive).
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from typing import Iterable, Optional

from hermes_platform.resolver import locate_command

RESTART_IGNORE_GLOBS = (
    "tests/**",
    "docs/**",
    ".github/**",
    "README*",
    "CONTRIBUTING*",
    "SECURITY*",
    ".mailmap",
)

_GIT_TIMEOUT_S = 15
_GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES")
_HEX = frozenset("0123456789abcdef")


def path_restart_ignored(path: str, globs: Iterable[str] = RESTART_IGNORE_GLOBS) -> bool:
    """True when the repo-relative ``path`` matches one of ``globs`` (see the module docstring)."""
    if not path or path.startswith("/") or "\\" in path:
        return False
    for glob in globs:
        if glob.endswith("/**"):
            if path.startswith(glob[:-2]) and len(path) > len(glob) - 2:
                return True
        elif "/" not in path and fnmatch.fnmatchcase(path, glob):
            return True
    return False


def _is_sha(value: object) -> bool:
    return isinstance(value, str) and len(value) in (40, 64) and set(value) <= _HEX


def _git(root: str, *args: str) -> Optional[subprocess.CompletedProcess]:
    """None when git could not run at all (not found, OSError, timeout): no verdict, as opposed to a git exit."""
    # Resolved like boot_pinned_imports does (t_01a1d49d2f2a8ae2), not by a bare "git" from the ambient PATH.
    command = locate_command("git").command
    if not command:
        return None
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    try:
        return subprocess.run([command[0], "--no-replace-objects", "-C", root, *args], capture_output=True,
                              timeout=_GIT_TIMEOUT_S, env=env, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _names_no_commit(root: str, *shas: str) -> bool:
    """True when git, reading the repo fine, says one of ``shas`` is no commit there (exit 1 of
    ``rev-parse --verify --quiet``). An unreadable repo (exit 128), a timeout or no git is not that statement."""
    for sha in shas:
        found = _git(root, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{sha}^{{commit}}")
        if found is None or found.returncode not in (0, 1):
            return False
        if found.returncode == 1:
            return True
    return False


def range_restart_verdict(root: str, base: str, head: str,
                          globs: Iterable[str] = RESTART_IGNORE_GLOBS) -> Optional[bool]:
    """``range_restart_free`` with the transient case kept apart: True/False are final for the pair (a commit
    range never changes), None means git did not answer (not found, timeout, OSError, an unexpected exit) and
    the same question may get a definite answer later, so callers must not cache it. A sha that names no commit
    in the repo (a boot commit gc'd after a force-push) is a definite False. A disk sha can still appear later
    (a shallow checkout deepened by fetch, a ref written before its objects), but a cached False only keeps the
    pair reading as skew until the cache clears or the process restarts: fail closed, at worst one extra restart."""
    if not (_is_sha(base) and _is_sha(head)) or base == head:
        return False
    globs = tuple(globs)
    ancestor = _git(root, "merge-base", "--is-ancestor", base, head)
    if ancestor is not None and ancestor.returncode == 128 and _names_no_commit(root, base, head):
        return False
    if ancestor is None or ancestor.returncode not in (0, 1):
        return None
    if ancestor.returncode == 1:
        return False
    # --ignore-submodules=none: a diff.ignoreSubmodules=all config must not hide a gitlink bump (a code change).
    listed = _git(root, "log", "-m", "--no-renames", "--ignore-submodules=none", "--submodule=short",
                  "--name-only", "-z", "--format=", f"{base}..{head}")
    if listed is None or listed.returncode != 0:
        return None
    paths = [p.decode("utf-8", "surrogateescape").strip("\n") for p in listed.stdout.split(b"\0")]
    paths = [p for p in paths if p]
    # A range of empty commits only is left to the old rule (a move is a move): nothing proves it needs no restart.
    return bool(paths) and all(path_restart_ignored(p, globs) for p in paths)


def range_restart_free(root: str, base: str, head: str, globs: Iterable[str] = RESTART_IGNORE_GLOBS) -> bool:
    """True when ``base`` is a strict ancestor of ``head`` and every commit in ``base..head`` (merges against
    each parent) touches only ``globs`` paths. Anything unprovable (not full shas, equal, unrelated or reversed
    revisions, a git failure) is False: the caller then treats the move as a code change, exactly as before."""
    return range_restart_verdict(root, base, head, globs) is True
