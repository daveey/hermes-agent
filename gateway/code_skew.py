"""Detect when the gateway is running stale code after a hot ``git pull``.

The gateway's ``sys.modules`` is frozen at boot.  If the checkout is updated
underneath it, a first-time lazy import can resolve a freshly-pulled module
against a stale cached dependency -> ImportError.  We snapshot the revision at
startup so risky callers (e.g. ``/model`` switching) can refuse with a clear
"restart the gateway" message.  If the revision can't be read (non-git install,
IO error) the boot snapshot stays ``None`` and detection no-ops — never a false positive.
"""

from __future__ import annotations

from pathlib import Path

from hermes_cli import restart_ignore_paths as _restart_ignore_paths  # at boot: a lazy import would be pinned
from hermes_cli._startup_fast import project_root_str

_PROJECT_ROOT = Path(project_root_str())
_boot_fingerprint: str | None = None


def _fingerprint() -> str | None:
    """Current checkout fingerprint via the CLI's worktree-aware git-rev reader
    (``hermes_cli.main`` is always already imported in a gateway process)."""
    try:
        from hermes_cli.main import _read_git_revision_fingerprint

        return _read_git_revision_fingerprint(_PROJECT_ROOT)
    except Exception:
        return None


def record_boot_fingerprint() -> None:
    """Snapshot the checkout revision at gateway startup (idempotent).

    Prefers the revision ``hermes_bootstrap`` read when the process started: by the time a gateway
    or serve lifespan gets here, ``hermes_cli.main`` has already imported much of the tree, and an
    update that landed meanwhile must not pass as the boot commit (agent-529f2244ac)."""
    global _boot_fingerprint
    if _boot_fingerprint is None:
        _boot_fingerprint = _process_boot_fingerprint() or _fingerprint()
        current = _fingerprint()
        if _boot_fingerprint and current and current != _boot_fingerprint:
            import logging

            logging.getLogger(__name__).warning(
                "code-skew at startup: checkout moved from %s to %s while this process was starting; "
                "modules imported so far may mix both, later first-time imports load from %s",
                _short(_boot_fingerprint), _short(current), _short(_boot_fingerprint))


def _process_boot_fingerprint() -> str | None:
    """The revision ``hermes_bootstrap`` read at process start. A pytest process honours it only
    with ``HERMES_BOOT_PINNED_IMPORTS=1`` (as ``boot_pinned_imports.install`` does): tests stand in
    their own revisions through ``_fingerprint`` and must not get the worktree's real HEAD."""
    import os

    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get(
            "HERMES_BOOT_PINNED_IMPORTS", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return None
    try:
        from hermes_cli.git_fingerprint import process_boot_fingerprint

        return process_boot_fingerprint(_PROJECT_ROOT)
    except ImportError:  # a tree from before the module existed
        return None


def install_boot_pinned_imports() -> None:
    """After ``record_boot_fingerprint``: pin later first-time in-tree imports to the boot commit
    once the checkout moves, so a lazy import never mixes revisions (``gateway.boot_pinned_imports``)."""
    try:
        from gateway.boot_pinned_imports import install
        from hermes_cli.main import _read_git_revision_fingerprint

        # Bind the reader and root now: later monkeypatches/reassignments must not flip the latch.
        root = _PROJECT_ROOT
        install(root, _boot_fingerprint, lambda: _read_git_revision_fingerprint(root))
    except Exception:
        import logging

        logging.getLogger(__name__).warning("boot-pinned imports did not install", exc_info=True)


def _short(fingerprint: str) -> str:
    """Render a ``git:<ref>:<sha>`` fingerprint as a compact label."""
    sha = fingerprint.rsplit(":", 1)[-1]
    return sha[:10] if sha and sha != "unresolved" and len(sha) > 10 else (sha or fingerprint)


def current_code_sha() -> str | None:
    """Full SHA for the checkout currently on disk, or None when unresolved."""
    fingerprint = _fingerprint()
    if fingerprint is None:
        return None
    sha = fingerprint.rsplit(":", 1)[-1]
    return sha if sha and sha != "unresolved" else None


def detect_code_skew() -> tuple[str, str] | None:
    """``(boot_rev, disk_rev)`` short labels if the checkout drifted since boot, else ``None``.

    [daveey] A forward drift whose every commit touches only ``RESTART_IGNORE_GLOBS`` paths (tests, docs, CI)
    is no skew: this process runs exactly the code on disk, and hermes-sync moves the hub checkout over such a
    range without restarting anything (t_01a1d471b754e5bc)."""
    current = _fingerprint() if _boot_fingerprint is not None else None
    if current is None or current == _boot_fingerprint:
        return None
    if _drift_restart_free(_boot_fingerprint, current):
        return None
    return _short(_boot_fingerprint), _short(current)


# (boot sha, disk sha) -> range_restart_verdict. A commit range never changes, so a definite verdict is final; the
# watchdogs poll every few seconds and would otherwise run two git commands per poll for the same pair. A transient
# git failure (None: timeout, OSError, git not found) is not cached: it reads as skew now and is asked again on the
# next poll, so it never pins the pair as skewed for the life of the process (t_01a1d49d2f2a8ae2). A sha git says
# is no commit (boot commit gc'd after a force-push) is a definite False and is cached (PR #2151 review). A disk
# sha may still appear later (shallow checkout deepened, ref written before its objects): the cached False then
# keeps that pair skewed until the cache clears or the process restarts, failing closed (at worst one restart).
_restart_free_verdicts: dict[tuple[str, str], bool] = {}


def _drift_restart_free(boot_fingerprint: str | None, current: str) -> bool:
    boot_sha = (boot_fingerprint or "").rsplit(":", 1)[-1]
    disk_sha = current.rsplit(":", 1)[-1]
    key = (boot_sha, disk_sha)
    verdict = _restart_free_verdicts.get(key)
    if verdict is None:
        # range_restart_verdict never raises: an unprovable drift is False/None = skew, as before
        answer = _restart_ignore_paths.range_restart_verdict(str(_PROJECT_ROOT), boot_sha, disk_sha)
        if answer is None:
            return False
        if len(_restart_free_verdicts) > 64:
            _restart_free_verdicts.clear()
        _restart_free_verdicts[key] = verdict = answer
    return verdict


_import_mix_noted: set[tuple[str, str]] = set()


def note_import_error(exc: BaseException, where: str) -> bool:
    """Name an ImportError raised while the checkout differs from this process's boot revision.

    One ``code-skew import mix`` WARNING per (boot, disk) pair, so the error signal shows the cause
    (a process running two revisions; restart it) instead of a bare ``cannot import name``
    (agent-529f2244ac). Returns True when the error happened under skew. Never raises: the
    fingerprint readers and ``range_restart_verdict`` swallow their own failures. Not free: the first
    ImportError for an uncached (boot, disk) pair runs a few git subprocesses (``_drift_restart_free``,
    15 s timeout each); later ones hit the verdict cache unless git failed transiently."""
    if not isinstance(exc, ImportError):
        return False
    boot = _boot_fingerprint or _process_boot_fingerprint()
    if not boot:
        return False
    current = _fingerprint()
    if not current or current == boot:
        return False
    if _drift_restart_free(boot, current):
        # a tests/docs/CI-only drift: the process runs the code on disk, so this is no mix (t_01a1d49d2f2a8ae2)
        return False
    pair = (boot, current)
    if pair not in _import_mix_noted:
        _import_mix_noted.add(pair)
        import logging

        logging.getLogger(__name__).warning(
            "code-skew import mix: %s raised %s: %s; this process booted on %s but the checkout "
            "is now %s, so it runs two revisions — restart it to load one",
            where, type(exc).__name__, exc, _short(boot), _short(current))
    return True
