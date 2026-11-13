"""Per-board state of the sync client's repair window (``decide``, ``note_round``). [daveey]

Moved out of ``kanban_sync_client`` (at its line cap); the client re-exports every name, and the dicts are shared
objects, so ``ksc._REPAIRED`` and friends stay the same state.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable, Optional

# Per board file: the inputs the last completed repair pass saw (see ``_repair_inputs``).
_REPAIRED: dict[str, tuple] = {}
# Per board file: when that pass finished (``_repair_clock``), and when a deferred pass is due.
_REPAIRED_AT: dict[str, float] = {}
_REPAIR_DEFERRED: dict[str, float] = {}
# Per board file: the live repair revision a deferring round credited to the sync thread's own steps (see ``decide``).
# Cleared by a completed pass and by ``reset_board_watch``.
_REPAIR_CREDIT: dict[str, int] = {}


# A write by another process (a worker heartbeat or claim, the dispatcher) is a repair-revision gap, and each gap
# ran the full pass at once: hub default board 2026-11-13, 5,428 cards, 80 of 86 full passes in 601 s were
# ``local_write`` (20% of gateway CPU samples). Such gaps now share one pass per window; an uncaptured edit is
# found at most this long after the previous pass instead of on the next round. The deferral covers the whole pass,
# so ``repair_partition_links``, ``repair_partition_chat_links`` and ``retire_lost_placeholder_links`` also run up to
# this long after a link written by another process (the remote-parent mirror is never deferred).
LOCAL_WRITE_REPAIR_MIN_S = 30.0


def applies_chain(start: Any, applies: Iterable[tuple], end: Any) -> bool:
    """True when the feed applies' (before, after) revisions account for every live revision step from ``start``
    to ``end``: no gap (a local write between them), no unknown (None) revision."""
    at = start
    for before, after in applies:
        if before is None or after is None or before != at:
            return False
        at = after
    return at is not None and at == end


def decide(key: str, inputs: Optional[tuple], applies: Optional[Iterable[tuple]], now: float, min_s: float) -> str:
    """Why this round's changed repair inputs run the full pass now, or ``deferred``.

    ``deferred`` when the only inputs that changed since the last completed pass are the replica's feed cursor and
    live revision steps the sync thread credited to itself (same replica file, schema, mode, gateway and
    placement), and that pass finished less than ``min_s`` ago; records when the deferred pass is due so the sync
    loop runs it then. ``applies`` is the credited steps' (before, after) revisions (``kanban_sync_own_writes``:
    feed applies, push acknowledgements, the remote-parent mirror, pool reservations); they must chain from the
    credited revision (the last pass's, or the last deferring round's) to this round's without a gap. Any gap is a
    write the scans exist to find (another process, or this thread outside those steps): it repairs at once when
    the last pass is older than ``LOCAL_WRITE_REPAIR_MIN_S``, else that pass is due when the window ends
    (``deferred``, nothing credited). A missing list (None: a rebuild) repairs at once. The reason: ``unknown``
    inputs, ``first`` (no completed pass), ``inputs`` (schema, replica file, mode, gateway or placement),
    ``local_write`` (a gap) or ``window`` (the last pass is older than ``min_s``). [daveey]"""
    last = _REPAIRED.get(key)
    if inputs is None:
        return "unknown"
    if last is None:
        return "first"
    if applies is None:
        return "rebuilt"
    (gen, (revision, schema)), (dev, ino, _cursor), *rest = inputs
    (last_gen, (last_revision, last_schema)), (last_dev, last_ino, _last_cursor), *last_rest = last
    if (gen, schema) != (last_gen, last_schema) or (dev, ino) != (last_dev, last_ino) or rest != last_rest:
        return "inputs"
    if not applies_chain(_REPAIR_CREDIT.get(key, last_revision), applies, revision):
        # Not credited: every later round still sees the gap, so the pass runs when this window ends at the latest.
        due = _REPAIRED_AT.get(key, float("-inf")) + LOCAL_WRITE_REPAIR_MIN_S
        if now >= due:
            return "local_write"
        _REPAIR_DEFERRED[key] = due
        return "deferred"
    due = _REPAIRED_AT.get(key, float("-inf")) + min_s
    if now >= due:
        return "window"
    _REPAIR_DEFERRED[key] = due
    _REPAIR_CREDIT[key] = revision
    return "deferred"


# Per board: how its rounds ended since ``_ROUNDS_SINCE`` (``note_round``): ``unchanged`` (no repair input moved),
# ``deferred``, or the reason the full pass ran (``decide``, plus ``rebuilt``). Logged at INFO every ROUND_LOG_S.
ROUND_LOG_S = 600.0
_ROUNDS: dict[str, dict[str, int]] = {}
_ROUNDS_SINCE: dict[str, float] = {}


def note_round(board: str, outcome: str, now: float) -> None:
    """Count one round's repair outcome for ``board``; every ``ROUND_LOG_S`` log the counts and start over, so a
    live check needs only the gateway log (``repair rounds``), not a ``py-spy --locals`` of the sync thread."""
    counts = _ROUNDS.setdefault(board, {})
    counts[outcome] = counts.get(outcome, 0) + 1
    since = _ROUNDS_SINCE.setdefault(board, now)
    if now - since < ROUND_LOG_S:
        return
    full = sum(n for k, n in counts.items() if k not in ("unchanged", "deferred"))
    reasons = ", ".join(f"{k} {n}" for k, n in sorted(counts.items()) if k not in ("unchanged", "deferred"))
    logging.getLogger("hermes_cli.kanban_sync_client").info(
        "kanban sync: %s repair rounds in %ds: %d full pass (%s), %d deferred, %d unchanged", board, int(now - since),
        full, reasons or "none", counts.get("deferred", 0), counts.get("unchanged", 0))
    _ROUNDS[board] = {}
    _ROUNDS_SINCE[board] = now


def round_counts(board: str) -> dict[str, int]:
    """This board's round outcomes since the last ``repair rounds`` log line (a copy)."""
    return dict(_ROUNDS.get(board, {}))


def reset() -> None:
    """Tests (``reset_board_watch``): forget every pass, deferral, credit and round count."""
    for state in (_REPAIRED, _REPAIRED_AT, _REPAIR_DEFERRED, _REPAIR_CREDIT, _ROUNDS, _ROUNDS_SINCE):
        state.clear()


def deferred_repairs_due(boards: list[tuple[str, Path]], now: float) -> list[tuple[str, Path]]:
    """Boards whose deferred repair pass is due at ``now`` (``_repair_clock`` time)."""
    return [(slug, path) for slug, path in boards
            if (due := _REPAIR_DEFERRED.get(str(path))) is not None and now >= due]


def next_deferred_repair(boards: list[tuple[str, Path]]) -> Optional[float]:
    """The earliest due time (``_repair_clock``) of a deferred repair pass among ``boards``, or None."""
    dues = [due for _slug, path in boards if (due := _REPAIR_DEFERRED.get(str(path))) is not None]
    return min(dues) if dues else None
