"""Durable consecutive pass-then-fail wake confirmations. [daveey]"""
from __future__ import annotations

import sqlite3
from typing import Any

EVENT = "wake_check_flapping"
FAILED_EVENT = "wake_confirm_failed"
AFTER = 3
NOTE = (
    "This card's wake_check passed and then failed its immediate confirm re-run 3 times in a row. "
    "A passing probe is run twice and the card wakes only if both runs pass, so a probe that compares "
    "against the record its own first run just wrote can never wake. Make it give the same answer "
    "when re-run seconds later."
)


def note_result(conn: sqlite3.Connection, task_id: str, cmd: str, rc: Any,
                tail: str, diagnostics: dict | None) -> str | None:
    """Caller holds the write txn and has validated cmd, generation and scheduled status.

    The counter belongs to the execution row: set_check deletes it on every re-park,
    and _wake deletes it on every wake. The dispatcher's per-attempt generation claim
    must NOT reset it, but still fences stale results before this helper is called.
    Any result other than a failed confirmation breaks the consecutive streak.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_wake_episode as episode
    from hermes_cli.kanban_wake_check import _tail, _wake

    if rc == 0 or (diagnostics and diagnostics.get("first_rc") == 0):
        episode.reset(conn, task_id)
        if rc != 0:
            kb._append_event(conn, task_id, "wake_episode_reset")
    failed = bool(diagnostics and diagnostics.get("first_rc") == 0 and rc != 0)
    conn.execute("UPDATE task_wake_checks SET confirm_fails = "
                 + ("confirm_fails + 1" if failed else "0") + " WHERE task_id = ?", (task_id,))
    if not failed:
        return None
    count = conn.execute("SELECT confirm_fails FROM task_wake_checks WHERE task_id = ?", (task_id,)).fetchone()[0]
    payload = {"wake_check": cmd, "first_rc": diagnostics["first_rc"],
               "first_output": _tail(diagnostics.get("first_output", "")),
               "confirm_rc": rc, "confirm_output": tail, "confirm_fails": count}
    kb._append_event(conn, task_id, FAILED_EVENT, payload)
    if count < AFTER:
        return None
    woke = _wake(conn, task_id, EVENT, {**payload, "note": NOTE})
    if woke:
        kb.add_comment(conn, task_id, "kanban-dispatcher", NOTE)
    return woke
