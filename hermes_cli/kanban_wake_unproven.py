"""One early notice when a scheduled wait's probe keeps saying "not yet" under a far backstop. [daveey]

The 48 h ``wake_check_stale`` wake (``kanban_wake_episode``) is the late, card-waking reconsideration. A probe
whose premise can never hold (a PID of a service that restarts, a file nothing writes) leaves the card
``scheduled`` for days with nobody told. This adds only detection: after ``wake_check_notice_hours`` (default
6 h) of clean exit-1 checks within the current park, while the park's backstop (``wake_at``) is more than
24 h away, the card's origin chat gets ONE passive ``wake_probe_unproven_notice`` naming the probe and its
last output. Probe semantics, backstops and admission are untouched; the card stays scheduled.

One notice per park episode: a later park event (re-park) opens a new episode. Clock-only waits run no probe,
so they never reach this.
"""
from __future__ import annotations

import json
import math
import sqlite3
from typing import Optional

NOTICE_KIND = "wake_probe_unproven_notice"
DEFAULT_NOTICE_HOURS = 6
MIN_CHECKS = 3
FAR_BACKSTOP_SECONDS = 24 * 3600


def notice_seconds() -> float:
    from hermes_cli.config import load_config_readonly
    from hermes_cli.config_backend import ConfigBackendUnavailable

    try:
        section = load_config_readonly().get("kanban")
    except (ConfigBackendUnavailable, OSError, ValueError):
        section = None
    value = section.get("wake_check_notice_hours") if isinstance(section, dict) else DEFAULT_NOTICE_HOURS
    try:
        hours = float(value)
    except (TypeError, ValueError):
        hours = DEFAULT_NOTICE_HOURS
    if not math.isfinite(hours) or hours <= 0:
        hours = DEFAULT_NOTICE_HOURS
    return hours * 3600


def _latest_park(conn: sqlite3.Connection, task_id: str) -> Optional[tuple[int, int, int]]:
    """``(event_id, parked_at, wake_at)`` of the card's newest park, or None."""
    from hermes_cli import kanban_timed_park as ktp

    row = conn.execute("SELECT id, payload, created_at FROM task_events WHERE task_id = ? "
                       "AND kind IN ('scheduled', 'continued') ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[1] or "{}")
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if payload.get("clock_only"):
        return None  # a pure time wait has no probe to doubt
    return int(row[0]), int(row[2]), int(ktp.implied_wake(payload, int(row[2])))


def maybe_notice(conn: sqlite3.Connection, task_id: str, cmd: str, out: str, *, now: int, first_at: int,
                 checks: int, threshold: Optional[float] = None) -> bool:
    """Append the notice when due; call inside the probe-result write txn after a counted clean exit 1."""
    from hermes_cli import kanban_db as kb

    if checks < MIN_CHECKS:
        return False
    park = _latest_park(conn, task_id)
    if park is None:
        return False
    park_id, parked_at, wake_at = park
    if wake_at - now <= FAR_BACKSTOP_SECONDS:
        return False
    limit = notice_seconds() if threshold is None else threshold
    since = max(first_at, parked_at)  # a re-park opens a new episode and restarts the age
    if now - since < limit:
        return False
    if conn.execute("SELECT 1 FROM task_events WHERE task_id = ? AND kind = ? AND id > ?",
                    (task_id, NOTICE_KIND, park_id)).fetchone():
        return False
    kb._append_event(conn, task_id, NOTICE_KIND, {
        "wake_check": cmd, "output": (out or "")[-500:], "checks": checks, "hours": round((now - since) / 3600, 1),
        "wake_at": wake_at})
    return True


def note_check(conn: sqlite3.Connection, task_id: str, cmd: str, out: str, now: int) -> bool:
    """After a counted clean exit 1: notice from the card's wake episode, when there is one."""
    ep = conn.execute("SELECT first_at, checks FROM task_wake_episodes WHERE task_id = ?", (task_id,)).fetchone()
    return bool(ep) and maybe_notice(conn, task_id, cmd, out, now=now, first_at=ep[0], checks=ep[1])


def notice_text(payload: dict) -> str:
    payload = payload if isinstance(payload, dict) else {}
    cmd = str(payload.get("wake_check") or "")[:300]
    out = str(payload.get("output") or "").strip()[-300:] or "(no output)"
    hours = payload.get("hours")
    span = f"{hours:g} h" if isinstance(hours, (int, float)) else "hours"
    return (f"its wake probe has not passed in {span} ({payload.get('checks', '?')} checks) and the wait's "
            f"backstop is more than 24 h away. Check that the condition can occur. Probe: `{cmd}`. "
            f"Last output: {out}")
