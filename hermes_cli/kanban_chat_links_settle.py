"""The settle window of ``links_diff``. [daveey]

The origin ledger and the replicas are two stores that propagate asynchronously: a card created or linked a moment
ago is in one and not yet in the other (a ledger observation before the next replica pull, a pulled link before the
next ledger projection). On a gateway whose chats spawn cards all the time, a single snapshot therefore always
listed 1-3 brand-new cards, each gone within 20-60 s, so ``clean`` was False in every sample and could not serve as
a parity gate (David-MBP, 6 samples 20 s apart, 2026-11-13, live proof t_01a1d5c12a26ef3f).

:func:`settle` re-reads the diff every ``poll_s`` for up to ``settle_s`` and keeps as a difference only an entry
present in EVERY sample that could read its board: a persistent divergence (a link one side never gets) is still in
the last sample and the report stays unclean; an entry that cleared, or that showed up only after the first sample,
is sync lag and is reported under ``in_flight`` without making the report unclean. It stops at the first sample in
which nothing persists and every replica was readable, so a quiet board answers in one read.

A sample in which a board's replica is unreadable says nothing about that board: it neither clears nor confirms its
entries, and the report is never clean while the LAST sample has an unreadable replica (PR #2520 review: a plain
intersection let a replica that turned unreadable mid-window drop its differences and end clean).
"""

from __future__ import annotations

import time
from typing import Any, Callable

# Longer than the slowest lag observed (~60 s) and several sync rounds (``kanban.sync.interval_seconds``, 5 s).
DEFAULT_SETTLE_S = 90.0
DIFF_KEYS = ("only_in_ledger", "only_in_replica")

Entry = tuple[str, str, tuple]  # (diff key, session, (gateway, board, task id))
Read = tuple[set[Entry], set[str]]  # one sample: (entries, unreadable boards)


def default_poll_s() -> float:
    """Two sync intervals (``kanban.sync.interval_seconds``), at least 5 s."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_sync_client import DEFAULT_INTERVAL_S
    try:
        interval = float(sync.sync_config().get("interval_seconds") or DEFAULT_INTERVAL_S)
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL_S
    return max(5.0, 2 * interval)


def _entries(report: dict[str, Any]) -> set[Entry]:
    return {(key, session, tuple(card)) for key in DIFF_KEYS for session, cards in report[key].items()
            for card in cards}


def _board(entry: Entry) -> str:
    return entry[2][1]


def _split(reads: list[Read]) -> tuple[set[Entry], set[Entry]]:
    """(persistent, in_flight). Persistent: listed by every sample that could read the entry's board, and at least
    one could. In flight: the rest of what any sample listed (a sample that could not read the board neither clears
    nor confirms the entry)."""
    listed: set[Entry] = set().union(*(entries for entries, _bad in reads))
    persistent = {e for e in listed
                  if any(_board(e) not in bad for _e, bad in reads)
                  and all(e in entries for entries, bad in reads if _board(e) not in bad)}
    return persistent, listed - persistent


def _grouped(entries: set[Entry], key: str) -> dict[str, list[tuple]]:
    out: dict[str, list[tuple]] = {}
    for k, session, card in entries:
        if k == key:
            out.setdefault(session, []).append(card)
    return {s: sorted(cards) for s, cards in sorted(out.items())}


_sleep: Callable[[float], None] = time.sleep  # module seams: tests drive the window without waiting
_clock: Callable[[], float] = time.monotonic


def settle(snapshot: Callable[[], dict[str, Any]], settle_s: float, poll_s: float) -> dict[str, Any]:
    """``snapshot()`` (one raw ``links_diff`` read) re-read until nothing persists and every replica is readable, or
    ``settle_s`` has passed. Counts and ``unreadable`` come from the last read; ``only_in_*`` hold what every read
    able to read the board listed; ``in_flight`` the rest of what some read listed."""
    sleep, clock = _sleep, _clock
    start = clock()
    reads: list[Read] = []
    while True:
        report = snapshot()
        reads.append((_entries(report), set(report["unreadable"])))
        persistent, in_flight = _split(reads)
        unreadable = reads[-1][1]
        if not (persistent or unreadable) or clock() - start >= settle_s:
            break
        sleep(max(0.0, min(poll_s, settle_s - (clock() - start))))
    out = dict(report)
    out.update({key: _grouped(persistent, key) for key in DIFF_KEYS})
    out["unreadable"] = sorted(unreadable)
    out["clean"] = not persistent and not unreadable
    out["in_flight"] = {key: _grouped(in_flight, key) for key in DIFF_KEYS}
    out["settle"] = {"window_s": settle_s, "poll_s": poll_s, "samples": len(reads),
                     "elapsed_s": round(clock() - start, 1)}
    return out
