"""Exact-store recovery of retained unknown-card outcomes on a links-health transition. [daveey]

Runs only from the normal outcome executor after a health generation is pending, never from the
source callback. It reads the origin home's own ``state.db`` (no profile fallback, so equal chat ids
in a foreign profile are never aliased) and refreshes outcomes whose stored status says the linked
cards could not be read. Sessions without any remote card row are covered the same way.
"""
from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger(__name__)

# Stored-envelope markers of an unreadable card source (reason / reason_codes / coverage).
_MARKERS = ("source_unknown", "linked_status_unknown", "card_read_failed", "card_status_unavailable")


@contextlib.contextmanager
def origin_store(origin: tuple) -> Iterator[Any]:
    """Writable SessionDB for exactly the origin home, or None when it has no store."""
    path = Path(origin[0]) / "state.db"
    if not path.exists():
        yield None
        return
    from hermes_state_registry import acquire
    db = acquire(path)
    try:
        yield db
    finally:
        with contextlib.suppress(Exception):
            db.close()


def unknown_outcome_ids(db: Any) -> list[str]:
    where = " OR ".join("outcome_json LIKE ?" for _ in _MARKERS)
    rows = db._read_all(f"SELECT id FROM sessions WHERE outcome_json IS NOT NULL AND ({where})",
                        tuple(f"%{m}%" for m in _MARKERS))
    return [r["id"] for r in rows]


def recover(origin: tuple, skip: set[str], opener: Optional[Callable] = None) -> tuple[list[tuple[str, str]], bool]:
    """Refresh stored unknown-card outcomes in the origin store. Returns ([(chat, tip)], complete)."""
    from hermes_state_outcome import refresh_outcomes_quietly
    from tui_gateway.notice_fanout import chat_skip_reason
    done: list[tuple[str, str]] = []
    complete = True
    try:
        with (opener or origin_store)(origin) as db:
            if db is None:
                return done, True
            for chat in unknown_outcome_ids(db):
                if chat in skip:
                    continue
                if chat_skip_reason(db, chat) is not None:
                    continue
                tip = db.get_compression_tip(chat) or chat
                if refresh_outcomes_quietly([tip], "card_change", db=db):
                    done.append((chat, tip))
                else:
                    complete = False
    except Exception:  # health: allow BLE001 -- an unreadable store keeps the generation pending
        logger.warning("links-health outcome recovery failed", exc_info=True)
        complete = False
    return done, complete


def recover_into(out: Any, origin: tuple, generation: Optional[int], skip: set[str], opener: Optional[Callable] = None) -> bool:
    """Executor-side step: only while a health generation is pending. True when nothing is left to retry."""
    if generation is None:
        return True
    done, complete = recover(origin, skip, opener)
    for chat, tip in done:
        out.append({"board": None, "task_id": None, "chat_id": chat, "session_id": tip,
                    "status": None, "skipped": None, "source": "remote"})
    if not complete:
        out.retry()
    return complete
