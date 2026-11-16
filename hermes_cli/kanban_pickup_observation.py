"""Material pickup evidence, excluding the detector's changing observation clocks."""
from __future__ import annotations

import re

_STAMP = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"


def evidence(body: str) -> str:
    """Keep reasons, targets, counts and registry state; discard only rendered tick fields."""
    body = re.sub(r"(, ready )\d+( min,)", r"\1<elapsed>\2", body)
    body = re.sub(r"(pinned to machine:[^,\n]+, for )\d+( min \(threshold )", r"\1<elapsed>\2", body)
    body = re.sub(r", last " + _STAMP + r", \d+ pass\(es\)", ", last <tick>, <ticks> pass(es)", body)
    body = re.sub(r"(Registry entry for [^\n]+? at )" + _STAMP, r"\1<tick>", body)
    return body


def append_observation(conn, task_id: str, author: str, body: str) -> None:
    """Call under the incident write transaction; compare the last delivered observation, not all history."""
    from hermes_cli.kanban_db_comment_ids import add_comment

    note = f"Watcher firing:\n\n{body}"
    prior = conn.execute(
        "SELECT body FROM task_comments WHERE task_id=? AND author=? "
        "AND instr(body, ?) = 1 ORDER BY id DESC LIMIT 1",
        (task_id, author, "Watcher firing:\n\n"),
    ).fetchone()
    if prior is None or evidence(prior[0]) != evidence(note):
        add_comment(conn, task_id, author, note)
