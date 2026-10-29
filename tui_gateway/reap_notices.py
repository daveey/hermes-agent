"""Tell a parent chat when the startup orphan reap closed its subagent mid-work. [daveey]

A delegate child row that a backend restart killed while it was mid-tool or waiting on its next model call ends
``startup_orphan_reap`` with its last active message a tool or user row. Its parent never got a result (13 such
children in 14 days on the MacBook, session-state audit 2026-10-29). For each one the reap appends ONE notice to
the parent's live tip ("subagent <title> was stopped by a restart before finishing") and refreshes the parent's
outcome, which then reads ``unresolved{subagent_killed}`` until a turn answers it (the notice guarantee wakes it).
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)

_CHILDREN_SQL = """
SELECT s.id, s.title, s.parent_session_id,
       (SELECT role FROM messages WHERE session_id = s.id AND active = 1 AND role IN ('user', 'assistant', 'tool')
         ORDER BY id DESC LIMIT 1) AS last_role
  FROM sessions s
 WHERE s.id IN ({marks}) AND s.parent_session_id IS NOT NULL
   AND (s.source = 'subagent' OR {delegate} IS NOT NULL)
"""


def killed_subagents(db: Any, swept: Iterable[str]) -> list[dict]:
    """The reaped rows that are delegate children stopped mid-work: ``[{id, title, parent}]``."""
    from hermes_state_common import _sql_json_extract
    ids = [s for s in dict.fromkeys(swept) if s]
    if not ids:
        return []
    sql = _CHILDREN_SQL.format(marks=",".join("?" * len(ids)),
                               delegate=_sql_json_extract("s.model_config", "$._delegate_from"))
    return [{"id": str(r["id"]), "title": r["title"] or "subagent", "parent": str(r["parent_session_id"])}
            for r in db._read_all(sql, tuple(ids)) if r["last_role"] in ("tool", "user")]


def notify_parents_of_killed_subagents(db: Any, swept: Iterable[str]) -> list[str]:
    """Append one notice per killed child to its parent's tip and refresh that parent. Returns the parents
    notified. Never raises (a parent mid-turn refuses the row; that is logged and skipped)."""
    from hermes_state_outcome import refresh_outcomes_quietly
    notified: list[str] = []
    try:
        children = killed_subagents(db, swept)
    except Exception:
        logger.warning("killed-subagent lookup after the orphan reap failed", exc_info=True)
        return notified
    for child in children:
        tip = db.get_compression_tip(child["parent"]) or child["parent"]
        text = (f"⚠ subagent {child['title']} was stopped by a restart before finishing (child session "
                f"{child['id']}); its result never came back. Check what it got done and redo or re-delegate the rest.")
        metadata = {"notice_id": f"subagent-killed:{child['id']}", "display_text": text,
                    "subagent_killed": child["id"]}
        try:
            db.append_kanban_notice(tip, text, metadata)
        except Exception:
            logger.warning("could not tell parent %s that subagent %s was reaped", tip, child["id"], exc_info=True)
            continue
        notified.append(tip)
    refresh_outcomes_quietly(notified, "reap", db=db)
    if notified:
        logger.info("orphan reap: told %d parent chat(s) their subagent was stopped mid-work: %s",
                    len(notified), ", ".join(notified))
    return notified
