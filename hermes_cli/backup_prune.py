"""Pruning of quick state snapshots (``state-snapshots/<id>/``)."""
import logging
import shutil
from pathlib import Path
from typing import Callable, Optional, Sequence

logger = logging.getLogger("hermes_cli.backup")

QUICK_DEFAULT_KEEP = 20


def _prune_quick_snapshots(
    root: Path,
    keep: int = QUICK_DEFAULT_KEEP,
    protect: Optional[Callable[[Path], bool]] = None,
) -> int:
    """Remove oldest quick snapshots beyond the keep limit. Returns count deleted.

    ``protect``: a snapshot beyond the limit for which it returns True is left in place.
    """
    if not root.exists():
        return 0

    dirs = sorted(
        (
            d
            for d in root.iterdir()
            if d.is_dir() and not d.name.startswith(".") and not d.name.endswith(".partial")
        ),
        key=lambda d: d.name,
        reverse=True,
    )

    deleted = 0
    for d in dirs[keep:]:
        if protect is not None and protect(d):
            continue
        try:
            shutil.rmtree(d)
            deleted += 1
        except OSError as exc:
            logger.warning("Failed to prune snapshot %s: %s", d.name, exc)

    return deleted


def prune_after_snapshot(
    root: Path, keep: int, failed_dbs: Sequence[str], oversized_skipped: Sequence[str],
    current: Optional[str] = None,
) -> None:
    """Prune after a new quick snapshot landed in ``root``.

    #68805 review: a snapshot is incomplete when a present DB failed to capture or
    was skipped for size, and an older snapshot may then hold the only recoverable
    copy. A failed capture keeps every older snapshot. A size skip keeps only the
    older snapshots that really contain a skipped DB: the ones taken under the same
    cap never held it, and keeping them all leaked one snapshot per update (392 of
    them, 53 GB, behind a 22 GB state.db).
    """
    if oversized_skipped:
        print(
            "  ⚠ DB file(s) skipped for size: "
            + ", ".join(oversized_skipped)
            + " — any older snapshot that contains them is kept"
        )
        logger.warning("Quick snapshot skipped oversized DB file(s): %s", ", ".join(oversized_skipped))
    if failed_dbs:
        logger.warning(
            "Skipping snapshot prune because %d DB(s) failed to capture "
            "— preserving older snapshots as recovery source",
            len(failed_dbs),
        )
        return
    if not oversized_skipped:
        _prune_quick_snapshots(root, keep=keep)
        return
    # An incomplete snapshot never prunes itself: keep is at least 1 (0 or a negative keep did nothing
    # here before), and ``current`` (the snapshot just made) is exempt wherever its name sorts.
    _prune_quick_snapshots(
        root,
        keep=max(keep, 1),
        protect=lambda d: d.name == current or any((d / rel).is_file() for rel in oversized_skipped),
    )
