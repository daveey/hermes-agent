"""The settle window of ``links_diff``. [daveey]

The origin ledger and the replicas are two stores that propagate asynchronously: a card created or linked a moment
ago is in one and not yet in the other (a ledger observation before the next replica pull, a pulled link before the
next ledger projection). On a gateway whose chats spawn cards all the time, a single snapshot therefore always
listed 1-3 brand-new cards, each gone within 20-60 s, so ``clean`` was False in every sample and could not serve as
a parity gate (David-MBP, 6 samples 20 s apart, 2026-11-13, live proof t_01a1d5c12a26ef3f).

:func:`settle` re-reads the diff every ``poll_s`` for up to ``settle_s`` and keeps as a difference only an entry
present in EVERY sample: a persistent divergence (a link one side never gets) is still in the last sample and the
report stays unclean; an entry that cleared, or that showed up only after the first sample, is sync lag and is
reported under ``in_flight`` without making the report unclean. It stops at the first sample in which nothing of the
first sample persists, so a quiet board answers in one read.
"""

from __future__ import annotations

import time
from typing import Any, Callable

# Longer than the slowest lag observed (~60 s) and several sync rounds (``kanban.sync.interval_seconds``, 5 s).
DEFAULT_SETTLE_S = 90.0
DIFF_KEYS = ("only_in_ledger", "only_in_replica")


def default_poll_s() -> float:
    """Two sync intervals (``kanban.sync.interval_seconds``), at least 5 s."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_sync_client import DEFAULT_INTERVAL_S
    try:
        interval = float(sync.sync_config().get("interval_seconds") or DEFAULT_INTERVAL_S)
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL_S
    return max(5.0, 2 * interval)


def _entries(report: dict[str, Any]) -> set[tuple[str, str, tuple]]:
    return {(key, session, tuple(card)) for key in DIFF_KEYS for session, cards in report[key].items()
            for card in cards}


def _grouped(entries: set[tuple[str, str, tuple]], key: str) -> dict[str, list[tuple]]:
    out: dict[str, list[tuple]] = {}
    for k, session, card in entries:
        if k == key:
            out.setdefault(session, []).append(card)
    return {s: sorted(cards) for s, cards in sorted(out.items())}


_sleep: Callable[[float], None] = time.sleep  # module seams: tests drive the window without waiting
_clock: Callable[[], float] = time.monotonic


def settle(snapshot: Callable[[], dict[str, Any]], settle_s: float, poll_s: float) -> dict[str, Any]:
    """``snapshot()`` (one raw ``links_diff`` read) re-read until nothing of the first read persists or ``settle_s``
    has passed. Counts come from the last read; ``only_in_*`` and ``unreadable`` hold what every read listed;
    ``in_flight`` what some read listed and another did not."""
    sleep, clock = _sleep, _clock
    start = clock()
    report = snapshot()
    persistent = _entries(report)
    seen = set(persistent)
    unreadable = set(report["unreadable"])
    samples = 1
    while (persistent or unreadable) and clock() - start < settle_s:
        sleep(max(0.0, min(poll_s, settle_s - (clock() - start))))
        report = snapshot()
        samples += 1
        current = _entries(report)
        seen |= current
        persistent &= current
        unreadable &= set(report["unreadable"])
    in_flight = seen - persistent
    out = dict(report)
    out.update({key: _grouped(persistent, key) for key in DIFF_KEYS})
    out["unreadable"] = sorted(unreadable)
    out["clean"] = not persistent and not unreadable
    out["in_flight"] = {key: _grouped(in_flight, key) for key in DIFF_KEYS}
    out["settle"] = {"window_s": settle_s, "poll_s": poll_s, "samples": samples,
                     "elapsed_s": round(clock() - start, 1)}
    return out
