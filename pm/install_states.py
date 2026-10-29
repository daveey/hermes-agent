"""Reclaim idle per-checkout dependency state using one collector.

Dependency state lives under ``<home>/installs/<install_key>`` where the key hashes the
checkout path (:func:`pm.environments.install_key`). Every ``hermes -w`` worktree, review tree
or scratch clone that boots the agent therefore commits a full environment (venv, test venv,
PM runtime, ~200 MB each), and nothing removed it when the tree was deleted: one developer
host carried 402 such orphans (80 GB) after a fortnight of worktree campaigns.

The state dir records the checkout it belongs to in ``inputs/.project-root`` (written by
:func:`pm.environments.record_activation_inputs`). A missing path alone does not prove the
checkout is gone: a data root shared with a container (``-v ~/.hermes:/opt/data``) or an
unmounted volume records paths this process cannot see. So a dir is an orphan only when its
checkout sat where this process can see that it was deleted -- under the data root that owns
``installs/`` (scratch clones, the runtime checkout) or directly in a ``.worktrees/`` dir that
still exists. Removal still fails closed on anything a live process could hold: the install
lock, or a dependency / PM runtime generation lease.

A checkout elsewhere (``~/.cache/review-1/head``, a scratch clone outside the data root) can never
be proved deleted, so its missing checkout alone would keep the state forever. It is reclaimed
once the state has also gone unused for ``worktrees.install_state_retention_days``: every sync
recreates ``inputs/`` and every launch takes a generation lease (moving its
``.leases`` dir), so a checkout that still exists behind a mount boundary and is used at all keeps
its state. One unused for longer is rebuilt by ``hermes pm repair`` / the next activation.

Startup/``pm gc`` retain conservative discovery. The hourly host worktree GC explicitly selects
``--all-missing-roots`` [daveey] to include deleted ~/code/_wt and temporary checkouts; it also
requires the whole state idle for an hour. Every policy keeps held install locks and generation
leases; ``--dry-run`` reports what would go.
"""
from __future__ import annotations

import argparse
import contextlib
import logging
import os
import shutil
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _provably_deleted(root: Path, data_root: Path) -> bool:
    if root.exists():
        return False
    return root.is_relative_to(data_root) or (root.parent.name == ".worktrees" and root.parent.is_dir())


def _last_used(state: Path) -> float:
    """Newest mtime among what a sync (``inputs/`` is recreated, ``facts.json`` rewritten) or a
    launch (a generation's ``.leases`` dir) moves. Not the state dir itself: a part-way removal
    moves it, and that must not restart the clock for the retry."""
    paths = [state / "inputs", state / "facts.json"]
    for generations in (state / "environments", state / "pm-runtime" / "generations"):
        if generations.is_dir():
            paths += [generation / ".leases" for generation in generations.iterdir()]
    mtimes = [0.0]
    for path in paths:
        with contextlib.suppress(OSError):
            mtimes.append(path.lstat().st_mtime)
    return max(mtimes)


def _retention_seconds(data_root: Path) -> float | None:
    """The store-wide retention, from the config of the data root that owns ``installs/``.
    Every profile of that root shares the store, so the active profile's config must not decide
    for the others: a named profile without the key would apply the default to a state the root
    opted out of reclaiming."""
    from hermes_cli.config import cfg_get, load_config_readonly
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(data_root)
    try:
        days = cfg_get(load_config_readonly(), "worktrees", "install_state_retention_days", default=14)
    finally:
        reset_hermes_home_override(token)
    return days * 86400 if isinstance(days, (int, float)) and days > 0 else None


def _is_orphan(state: Path, data_root: Path, retention: float | None, all_missing_roots: bool = False) -> bool:
    """A recorded checkout that is gone: provably deleted, missing and unused past *retention*, or
    (``all_missing_roots``, host-local sweeps only [daveey]) missing at all."""
    if not state.is_dir() or state.is_symlink():
        return False
    try:
        text = (state / "inputs" / ".project-root").read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError):
        text = ""
    if not text or not Path(text).is_absolute():
        logger.info("keep %s: no recorded root (or unreadable/invalid record)", state)
        return False
    root = Path(text)
    try:
        root.stat()
    except FileNotFoundError:
        return all_missing_roots or _provably_deleted(root, data_root) or (
            retention is not None and time.time() - _last_used(state) > retention)
    except OSError:
        return False  # Permission/mount errors are not proof of absence.
    return False


def orphan_install_states(installs: Path, *, all_missing_roots: bool = False) -> list[Path]:
    """State dirs whose recorded checkout is provably deleted, or missing and unused past the
    retention period. Dirs without a record, and dirs whose checkout exists, are left alone.
    ``all_missing_roots`` (host-local discovery, opt-in) counts every missing checkout."""
    if not installs.is_dir():
        return []
    data_root = installs.resolve().parent
    retention = _retention_seconds(installs.parent)
    return [state for state in sorted(installs.iterdir())
            if _is_orphan(state, data_root, retention, all_missing_roots)]


@contextlib.contextmanager
def _install_lock(state: Path):
    """Hold the state's install lock (``runtime_lock``'s) without waiting: yields False while
    another process holds it, or when it cannot even be opened."""
    from pm.filesystem import lock_path

    try:
        fd = lock_path(state / ".install.lock", timeout=0)
    except OSError:
        fd = None
    if fd is None:
        yield False
        return
    try:
        yield True
    finally:
        os.close(fd)


@contextlib.contextmanager
def _probe_install_lock(state: Path):
    """Dry-run twin of :func:`_install_lock`: never creates the lock file nor changes an idle
    timestamp; a state without one counts as unheld. [daveey]"""
    from pm.filesystem import lock_fd

    try:
        fd = os.open(state / ".install.lock", os.O_RDWR)
    except FileNotFoundError:
        yield True
        return
    except OSError:
        yield False
        return
    try:
        yield lock_fd(fd, wait=False)
    finally:
        os.close(fd)


def _leased(state: Path) -> bool:
    """True while a generation lease (dependency env or PM runtime) is held.

    [daveey] Probes without mutating the tree: ``runtime_state.leases_held()`` prunes unlocked lease
    files, which touches directories during a dry run and resets the idle window."""
    from pm.filesystem import lock_fd

    for generations in (state / "environments", state / "pm-runtime" / "generations"):
        if not generations.is_dir():
            continue
        for generation in generations.iterdir():
            leases = generation / ".leases"
            if not leases.is_dir():
                continue
            for lease in leases.iterdir():
                try:
                    fd = os.open(lease, os.O_RDWR)
                except OSError:
                    return True
                try:
                    if not lock_fd(fd, wait=False):
                        return True
                finally:
                    os.close(fd)
    return False


def _idle_size(state: Path, cutoff: float) -> int | None:
    """Return reclaimable allocated bytes, or None if recently touched/unreadable. [daveey]

    Do not follow symlinks. Count a hardlinked inode only if all its links are in
    this tree; bytes shared outside it are not freed by unlinking this tree.
    """
    inodes = {}

    def failed(error):
        raise error

    try:
        for root, dirs, files in os.walk(state, onerror=failed, followlinks=False):
            for path in [Path(root), *(Path(root) / name for name in files),
                         *(Path(root) / name for name in dirs if (Path(root) / name).is_symlink())]:
                info = path.lstat()
                if info.st_mtime > cutoff:
                    return None
                key = (info.st_dev, info.st_ino)
                count, _, _ = inodes.get(key, (0, info, path.is_dir()))
                inodes[key] = (count + 1, info, path.is_dir() and not path.is_symlink())
    except OSError:
        return None
    return sum(getattr(info, "st_blocks", 0) * 512 for count, info, directory in inodes.values()
               if directory or count >= info.st_nlink)


_KEPT_UNTIL_LAST = frozenset({"inputs", ".install.lock"})


def _remove_contents(state: Path) -> bool:
    """Remove everything but the record and the held lock; True when nothing else is left.
    The record goes last, and only once everything else is gone: the startup prune runs on a
    daemon thread and a removal can fail part-way (an open file on Windows, a read-only dir), and
    a dir that lost its record first would never be recognised again."""
    for child in state.iterdir():
        if child.name in _KEPT_UNTIL_LAST:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                child.unlink()
    return not any(child.name not in _KEPT_UNTIL_LAST for child in state.iterdir())


def collect_orphan_install_states(installs: Path, *, all_missing_roots: bool = False, dry_run: bool = False,
                                  min_idle_seconds: float | None = None) -> list[Path]:
    """Remove every unheld orphan state dir under *installs*; returns what was removed (never
    dry-run plans).

    Each candidate is re-checked under its install lock right before removal: removing a large
    orphan takes long enough for another state to be synced or launched (lease taken and
    released) since the candidates were listed, and that use must keep it.

    [daveey] ``all_missing_roots`` (the hourly host worktree GC) counts every missing checkout, so it
    also requires the whole tree idle for ``min_idle_seconds`` (default 1 h; 0 otherwise).
    ``dry_run`` logs what would go without touching anything. One summary line reports the bytes."""
    if min_idle_seconds is None:
        min_idle_seconds = 3600 if all_missing_roots else 0
    removed: list[Path] = []
    freed = 0
    planned = 0
    if installs.is_dir():
        data_root = installs.resolve().parent
        retention = _retention_seconds(installs.parent)
        cutoff = time.time() - min_idle_seconds
        for state in [s for s in sorted(installs.iterdir())
                      if _is_orphan(s, data_root, retention, all_missing_roots)]:
            size = _idle_size(state, cutoff)
            if size is None:
                logger.info("keep %s: touched within %.0fs or unreadable", state, min_idle_seconds)
                continue
            had_lock = (state / ".install.lock").exists()
            emptied = False
            with (_probe_install_lock(state) if dry_run else _install_lock(state)) as locked:
                # Revalidate under the lock: an installer could have run between discovery and
                # acquisition. Creating a missing lock itself touches the state root, so that
                # legacy case uses the pre-lock idle scan.
                if (not locked or not _is_orphan(state, data_root, retention, all_missing_roots)
                        or (had_lock and _idle_size(state, cutoff) is None) or _leased(state)):
                    logger.info("keep %s: install lock or generation lease held, or back in use", state)
                    continue
                if dry_run:
                    planned += 1
                    logger.info("would remove %s (%.3f GiB)", state, size / 2**30)
                    continue
                emptied = _remove_contents(state)
                if emptied:  # still under the lock: only the record is left, so it goes last
                    shutil.rmtree(state / "inputs", ignore_errors=True)
                    # POSIX: unlink while still holding it. A launcher already waiting on this file
                    # then sees it gone once it gets the lock and retries on a fresh one (lock_path);
                    # unlinked after release, the path could be a file that launcher just made.
                    if os.name != "nt":
                        with contextlib.suppress(OSError):
                            (state / ".install.lock").unlink()
            if not emptied:
                logger.warning("keep %s: partly removed; retry next pass", state)
                continue
            # Non-recursive: a launch from a checkout recreated at the same path may have taken the
            # lock the moment it was released and written here; its files make rmdir fail, not vanish.
            with contextlib.suppress(OSError):
                if os.name == "nt":  # an open file cannot be unlinked: a waiting launcher keeps it
                    (state / ".install.lock").unlink()
                state.rmdir()
            if not state.exists():
                removed.append(state)
                freed += size
                logger.info("removed %s (%.3f GiB)", state, size / 2**30)
    logger.info("install-state gc: removed %d; freed ~%.3f GiB allocated (shared/cloned bytes may differ); "
                "would remove %d", len(removed), freed / 2**30, planned)
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installs", type=Path, help="override the owning home's installs directory")
    parser.add_argument("--all-missing-roots", action="store_true",
                        help="host-local only: this process sees every recorded checkout's filesystem")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    from pm.environments import installs_root

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    collect_orphan_install_states(args.installs if args.installs is not None else installs_root(),
                                  all_missing_roots=args.all_missing_roots, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
