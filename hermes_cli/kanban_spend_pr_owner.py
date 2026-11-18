"""Proven core publication ownership for spend-budget sparing. [daveey]

A PR mention, completion contract, review receipt or foreign comment is not publication.
Only published_pr run metadata or an owning run's explicit canonical-ship publication
report supplies the URL. Unknown GitHub state/base retains normal budget enforcement.
"""
from __future__ import annotations

import re
import sqlite3

_CORE_PR = re.compile(r"https://github\.com/softmax-universe/daveey-hermes/pull/[1-9][0-9]*")
_PUBLICATION = re.compile(
    r"^(?:(?:canonical ship )?(?:published|opened)(?: via canonical (?:ship|core-wt):)?"
    r"|Recovered[^\n.]*?and published via canonical core-wt:"
    r"|Run[0-9]+ canonical publication advanced [\w-]+:)\s+(?:core\s+)?PR\s*"
    r"(?:#[0-9]+\s*)?[:(\s]*(https://github\.com/[^\s)`>,;]+)", re.IGNORECASE)
_CANONICAL = re.compile(r"\b(?:canonical\s+(?:ship|core-wt|publication)|hermes-core-wt\s+ship)\b", re.IGNORECASE)
EVENT = "spend_budget_spared"


def published_prs(conn: sqlite3.Connection, task_id: str) -> set[str]:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_dispatch_pr_state import _comment_events
    from hermes_cli.kanban_requeued_publisher import _has_run_uid, _writer_run

    urls = set()
    for row in conn.execute("SELECT metadata FROM task_runs WHERE task_id=?", (task_id,)):
        value = kb._json_dict(row[0]).get("published_pr")
        if isinstance(value, str) and _CORE_PR.fullmatch(value):
            urls.add(value)
    events = _comment_events(conn, task_id)
    if events is None:
        return urls
    synced = _has_run_uid(conn)
    by_comment: dict[int, list[dict]] = {}
    for data, _at, cid in events:
        if cid is not None:
            by_comment.setdefault(cid, []).append(data)
    for cid, body in conn.execute("SELECT id, body FROM task_comments WHERE task_id=?", (task_id,)):
        stamps = by_comment.get(cid, [])
        if len(stamps) != 1 or _writer_run(conn, task_id, stamps[0], synced) is None:
            continue
        if not _CANONICAL.search(body):
            continue
        for value in _PUBLICATION.findall(body):
            if _CORE_PR.fullmatch(value):
                urls.add(value)
    return urls


def open_core_pr(conn: sqlite3.Connection, task_id: str) -> str | None:
    from hermes_cli.kanban_db_dispatch_pr_state import pr_details

    return next((url for url in sorted(published_prs(conn, task_id))
                 if pr_details(url) == ("OPEN", "main")), None)


def record_spared(conn, task_id, pr, spent, note):
    """Caller holds the write transaction and revalidated spend, ownership and lane state."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_spend_budget import AUTHOR

    for row in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?", (task_id, EVENT)):
        if kb._json_dict(row[0]).get("since") == spent.since:
            return
    kb._append_event(conn, task_id, EVENT, {"pr": pr, "runs": spent.runs, "tokens": spent.tokens,
                                          "since": spent.since, "reason": note})
    kb.add_comment(conn, task_id, AUTHOR,
                   f"{note}. Spend budget spared: this card published open core PR {pr} (base main). "
                   "Runs and tokens keep counting; normal enforcement resumes once it is merged or closed.",
                   system=True)
