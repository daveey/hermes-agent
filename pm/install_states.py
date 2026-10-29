"""Reclaim idle per-checkout dependency state using one collector.

Startup/``pm gc`` retain conservative discovery: a container sharing a data root may
not see the host's checkouts. The hourly host worktree GC explicitly selects
``--all-missing-roots`` to include deleted ~/code/_wt and temporary checkouts.
Both policies retain recent state, held install locks and generation leases.
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


def _orphan(state: Path, data_root: Path, all_missing_roots: bool) -> bool:
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
        return all_missing_roots or _provably_deleted(root, data_root)
    except OSError:
        return False  # Permission/mount errors are not proof of absence.
    return False


def orphan_install_states(installs: Path, *, all_missing_roots: bool = False) -> list[Path]:
    """Discover missing recorded roots; broader host-local discovery is opt-in."""
    if not installs.is_dir():
        return []
    return [state for state in sorted(installs.iterdir())
            if state.is_dir() and not state.is_symlink()
            and _orphan(state, installs.resolve().parent, all_missing_roots)]


def _leases_held(state: Path) -> bool:
    from pm.filesystem import lock_fd

    # leases_held() also prunes unlocked files, touching directories during a
    # dry run and resetting the idle window. Probe without mutating the tree.
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


@contextlib.contextmanager
def _install_lock(state: Path, *, dry_run: bool):
    """Hold the nonblocking lock until removal finishes, rather than probe then release."""
    from pm.filesystem import lock_fd

    fd = None
    try:
        flags = os.O_RDWR | (0 if dry_run else os.O_CREAT)
        try:
            fd = os.open(state / ".install.lock", flags, 0o600)
        except FileNotFoundError:
            yield dry_run  # Dry runs must not create locks or change idle timestamps.
            return
        except OSError:
            yield False
            return
        yield lock_fd(fd, wait=False)
    finally:
        if fd is not None:
            os.close(fd)


def _idle_size(state: Path, cutoff: float) -> int | None:
    """Return reclaimable allocated bytes, or None if recently touched/unreadable.

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


def _remove_state(state: Path) -> bool:
    # Keep the record and locked inode until all payloads are gone. Partial
    # removal remains discoverable on the next pass (including Windows holds).
    for child in state.iterdir():
        if child.name in ("inputs", ".install.lock"):
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                child.unlink()
    if any(child.name not in ("inputs", ".install.lock") for child in state.iterdir()):
        logger.warning("keep %s: partly removed; retry next pass", state)
        return False
    shutil.rmtree(state, ignore_errors=True)
    return not state.exists()


def collect_orphan_install_states(installs: Path, *, all_missing_roots: bool = False,
                                  dry_run: bool = False, min_idle_seconds: float = 3600) -> list[Path]:
    """Remove idle, unheld orphans. Return actual removals (never dry-run plans)."""
    removed = []
    freed = 0
    planned = 0
    cutoff = time.time() - min_idle_seconds
    for state in orphan_install_states(installs, all_missing_roots=all_missing_roots):
        size = _idle_size(state, cutoff)
        if size is None:
            logger.info("keep %s: touched within %.0fs or unreadable", state, min_idle_seconds)
            continue
        if _leases_held(state):
            logger.info("keep %s: generation lease held", state)
            continue
        had_lock = (state / ".install.lock").exists()
        with _install_lock(state, dry_run=dry_run) as acquired:
            if not acquired or _leases_held(state):
                logger.info("keep %s: install lock or generation lease held", state)
                continue
            # Revalidate after acquiring the lock: an installer could have run
            # between discovery and acquisition. Creating a missing lock itself
            # touches the state root, so that legacy case uses the pre-lock scan.
            if not _orphan(state, installs.resolve().parent, all_missing_roots):
                continue
            if had_lock and _idle_size(state, cutoff) is None:
                continue
            if dry_run:
                planned += 1
                logger.info("would remove %s (%.3f GiB)", state, size / 2**30)
            elif _remove_state(state):
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
