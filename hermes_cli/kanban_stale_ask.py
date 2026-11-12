"""Refresh a queued block notice's ask against the card's state at delivery time. [daveey]

A Kanban wake is rendered when the card blocks, then waits in the chat's FIFO until the running
turn ends, which can be hours. By then the card may be unblocked or done, yet the frozen text still
said "Next action for the user: ...". ``refresh_stale_ask`` re-reads the card (read-only) when the
text is delivered: a card that moved past the notice's block keeps the notice, with the
"Next action for the user" / "Why only the user" lines (and the user-terminal hint) replaced by one
``Since then: ...`` line. A card still blocked on the same block leaves the text byte-identical, and
an unreadable board or a remote card (``@task:<board>@<gateway>/...``) leaves it unchanged too.
"""
from __future__ import annotations

import contextlib
import json
import logging
import re
import sqlite3
from datetime import datetime
from typing import Optional

ASK_MARK = "Next action for the user: "
TERMINAL_HINT = ("\nUse the owner Desktop session's user-terminal flow; opening this task or unblocking it does not "
                 "execute a command or grant terminal authority.")
# Every free-text field is a tempered token: it may span lines (a human_request field can) but never
# another notice's actor line or ask mark, so a stray "Next action for the user:" in prose or a second
# joined notice cannot pull the text between them into one match. The ask mark must also sit at the
# start of the line right under block_notice()'s own "blocked — actor:" line.
_FIELD = r"(?:(?!Next action for the user: |\nblocked — actor: ).)*?"
_ASK = re.compile(
    r"(?P<head>blocked — actor: [^\n]*\n)Next action for the user: (?P<action>" + _FIELD + r")\n"
    r"Why only the user: " + _FIELD + r"\n"
    r"(?P<rest>Scope: " + _FIELD + r"\nOpen task: @task:(?P<board>[^/@\s]+)(?:@(?P<gw>[^/\s]+))?/(?P<tid>[A-Za-z0-9_-]+))"
    r"(?P<hint>" + re.escape(TERMINAL_HINT) + r")?",
    re.DOTALL)
logger = logging.getLogger(__name__)
_PT = "America/Los_Angeles"
# Transitions worth naming; claims, spawns, heartbeats and comments are noise here.
_LABELS = {"unblocked": "unblocked", "blocked": "blocked again", "scheduled": "scheduled",
           "review_requested": "sent to review", "completed": "completed", "archived": "archived",
           "request_withdrawn": "ask withdrawn", "gave_up": "gave up"}
_LAST_STATUS = {"completed": "done", "archived": "archived", "request_withdrawn": "archived",
                "blocked": "blocked", "scheduled": "scheduled", "review_requested": "review"}


def _norm(text) -> str:
    return " ".join(str(text or "").split())


def _payload(raw) -> dict:
    with contextlib.suppress(TypeError, ValueError):
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    return {}


def _read_card(board: str, task_id: str):
    """``(status, [(id, kind, created_at, payload)])`` from a read-only board connection, else ``None``."""
    from hermes_cli.kanban_db import kanban_db_path
    path = kanban_db_path(board=board)
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    try:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return None
        events = conn.execute("SELECT id, kind, created_at, payload FROM task_events WHERE task_id=? ORDER BY id",
                              (task_id,)).fetchall()
        return row[0], [(i, k, c, _payload(p)) for i, k, c, p in events]
    finally:
        conn.close()


def _anchor(events, action: str) -> Optional[int]:
    """Index of the newest ``blocked`` event carrying this notice's ask, else of the newest block."""
    blocks = [i for i, ev in enumerate(events) if ev[1] == "blocked"]
    for i in reversed(blocks):
        request = events[i][3].get("human_request")
        if isinstance(request, dict) and _norm(request.get("action")) == action:
            return i
    return blocks[-1] if blocks else None


def _clock(ts: int, base_day) -> tuple[str, str]:
    from zoneinfo import ZoneInfo
    when = datetime.fromtimestamp(int(ts), ZoneInfo(_PT))
    day = "" if when.date() == base_day else when.strftime("%b %-d ")
    return f"{day}{when:%H:%M}", when.strftime("%Z")


def since_line(status: str, events, anchor: int) -> Optional[str]:
    """The replacement line, or ``None`` while the card still waits on the same block."""
    from zoneinfo import ZoneInfo
    later = [ev for ev in events[anchor + 1:] if ev[1] in _LABELS and not ev[3].get("metadata_only")]
    if status in {"blocked", "triage"} and not later:
        return None  # still the same block: the ask stands
    base_day = datetime.fromtimestamp(int(events[anchor][2]), ZoneInfo(_PT)).date()
    stamped = [(_LABELS[kind], *_clock(created_at, base_day)) for _id, kind, created_at, _p in later]
    zones = {zone for _l, _c, zone in stamped}
    if len(zones) > 1:  # the window crossed a DST change: label each time
        text = ", ".join(f"{label} {clock} {zone}" for label, clock, zone in stamped)
    else:
        text = ", ".join(f"{label} {clock}" for label, clock, _z in stamped) + (f" {zones.pop()}" if zones else "")
    if not later or _LAST_STATUS.get(later[-1][1]) != status:
        text = f"{text}; now {status}" if text else f"now {status}"
    if status in {"blocked", "triage"}:
        newest = next((ev for ev in reversed(events) if ev[1] == "blocked"), None)
        if newest is not None and isinstance(newest[3].get("human_request"), dict):
            return f"Since then: {text}. This ask was replaced by a newer one; answer that, not this."
        return f"Since then: {text}. This ask no longer applies; the card is blocked on something else."
    return f"Since then: {text}. No action needed."


def refresh_stale_ask(text):
    """``text`` with each stale ask swapped for a ``Since then:`` line; unchanged on any doubt."""
    if not isinstance(text, str) or ASK_MARK not in text:
        return text

    def swap(match):
        if match["gw"]:
            return match[0]  # another gateway's card: this host cannot read its current state
        try:
            card = _read_card(match["board"], match["tid"])
        except (sqlite3.Error, OSError, ValueError):
            return match[0]
        if card is None:
            return match[0]
        status, events = card
        anchor = _anchor(events, _norm(match["action"]))
        line = since_line(status, events, anchor) if anchor is not None else None
        return match[0] if line is None else f"{match['head']}{line}\n{match['rest']}"

    return _ASK.sub(swap, text)


def _is_producer_row(payload) -> bool:
    """A producer wake (Kanban notifier, automation), never text a person typed or pasted."""
    native = payload.get("native_text_v1")
    return bool(payload.get("local_automation_v1")) or (isinstance(native, dict) and bool(native.get("automation")))


def refresh_payload(payload) -> None:
    """Refresh a claimed producer wake's text in place, before it becomes a turn or a note.

    Human-typed rows are never touched. Any failure leaves the text as it was: a refresh must never
    cost the wake itself."""
    try:
        if (isinstance(payload, dict) and isinstance(payload.get("text"), str) and ASK_MARK in payload["text"]
                and _is_producer_row(payload)):
            payload["text"] = refresh_stale_ask(payload["text"])
    except Exception:  # unchanged on any doubt
        logger.warning("stale block-ask refresh failed; delivering the wake unchanged", exc_info=True)
