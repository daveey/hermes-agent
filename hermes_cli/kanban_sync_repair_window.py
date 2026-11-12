"""Per-board state of the sync client's repair window (``kanban_sync_client._defer_repair``). [daveey]

Moved out of ``kanban_sync_client`` (at its line cap); the client re-exports every name, and the dicts are shared
objects, so ``ksc._REPAIRED`` and friends stay the same state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional

# Per board file: the inputs the last completed repair pass saw (see ``_repair_inputs``).
_REPAIRED: dict[str, tuple] = {}
# Per board file: when that pass finished (``_repair_clock``), and when a deferred pass is due.
_REPAIRED_AT: dict[str, float] = {}
_REPAIR_DEFERRED: dict[str, float] = {}
# Per board file: the live repair revision a deferring round credited to feed applies alone (see ``_defer_repair``).
# Cleared by a completed pass and by ``reset_board_watch``.
_REPAIR_CREDIT: dict[str, int] = {}


def applies_chain(start: Any, applies: Iterable[tuple], end: Any) -> bool:
    """True when the feed applies' (before, after) revisions account for every live revision step from ``start``
    to ``end``: no gap (a local write between them), no unknown (None) revision."""
    at = start
    for before, after in applies:
        if before is None or after is None or before != at:
            return False
        at = after
    return at is not None and at == end


def deferred_repairs_due(boards: list[tuple[str, Path]], now: float) -> list[tuple[str, Path]]:
    """Boards whose deferred repair pass is due at ``now`` (``_repair_clock`` time)."""
    return [(slug, path) for slug, path in boards
            if (due := _REPAIR_DEFERRED.get(str(path))) is not None and now >= due]


def next_deferred_repair(boards: list[tuple[str, Path]]) -> Optional[float]:
    """The earliest due time (``_repair_clock``) of a deferred repair pass among ``boards``, or None."""
    dues = [due for _slug, path in boards if (due := _REPAIR_DEFERRED.get(str(path))) is not None]
    return min(dues) if dues else None
