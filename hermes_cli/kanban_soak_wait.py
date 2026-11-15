"""Reject nominal observation windows; measure the evidence already available. [daveey]"""
from __future__ import annotations

import re
import sqlite3

_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                 "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                 "a": 1, "an": 1}
_DURATION = re.compile(
    r"\b(\d+(?:\.\d+)?(?=[\s-]*[a-z])|(?:one|two|three|four|five|six|seven|eight|nine|ten|an?)(?=[\s-]))"
    r"[\s-]*(minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)\b", re.IGNORECASE)
_OBSERVATION = re.compile(
    r"\b(?:window|watch|observation|readback|recount|live check)\b|"
    r"\bafter\s+(?:deploy\w*|merge\w*|landed|went live)\b", re.IGNORECASE)
_SOAK = re.compile(r"\bsoak\b", re.IGNORECASE)
_QUOTA_RESET = re.compile(r"\bquota[\s-]*reset\b|\b(?:quota|pool)\b[^.;\n]{0,60}\breset\b", re.IGNORECASE)
_REQUEST = re.compile(r'^\s*soak_requested_by: David — "[^"\n]*\S[^"\n]*"[ \t]*$', re.MULTILINE)


def observation_wait(text: str) -> bool:
    """A soak, or >=1h with observation wording; quota resets are real clock producers."""
    if _QUOTA_RESET.search(text):
        return False
    if _SOAK.search(text):
        return True
    if not _OBSERVATION.search(text):
        return False
    for amount, unit in _DURATION.findall(text.lower()):
        value = _NUMBER_WORDS[amount] if amount in _NUMBER_WORDS else float(amount)
        hours = value * {"m": 1 / 60, "h": 1, "d": 24, "w": 168}[unit[0]]
        if hours >= 1:
            return True
    return False


def requested(conn: sqlite3.Connection, task_id: str) -> bool:
    """Only a complete opt-in line on this card, not park prose claiming prior approval."""
    rows = conn.execute(
        "SELECT body FROM tasks WHERE id=? UNION ALL SELECT body FROM task_comments WHERE task_id=?",
        (task_id, task_id))
    return any(_REQUEST.search(row[0] or "") for row in rows)


def check(conn: sqlite3.Connection, task_id: str, record: dict, reason: str | None,
          *, short_continue: bool) -> None:
    from hermes_cli.kanban_awaiting import AwaitingError

    if record["kind"] != "clock" or short_continue:
        return
    text = " ".join((reason or "", record.get("why", "")))
    if not observation_wait(text) or requested(conn, task_id):
        return
    raise AwaitingError("soak_wait", (
        'David\'s rule: "i dont want to wait for any soaking, if something is waiting for a soak, '
        'run the check immediately and complete". Run the check now on evidence gathered since the '
        'event and state the real window (e.g. "3.2 h", never "24 h"). A deploy precondition parks '
        "as kind='deploy' on a tested probe (e.g. hub-admin fleet-has-core.sh <sha>). A natural-event "
        "precondition required by acceptance parks on a wake_check for that condition with its producer. "
        'Only an explicit request recorded on the card as soak_requested_by: David — "<his words>" '
        "permits an observation-window clock park."))
