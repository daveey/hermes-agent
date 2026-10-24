"""Run-scoped budget exhaustion belongs to agent triage, not a human. [daveey]"""
from __future__ import annotations

import re


# Deliberately a full match: an unrelated ask in the same reason must stay visible.
_ACTION = (r"(?:reset(?:/requeue)?|raise|increase|extend) (?:the )?continuation (?:budget|limit)"
           r"|split (?:the |this )?(?:work|task|card)(?: into (?:smaller )?(?:tasks|cards))?"
           r"|route (?:the )?remaining (?:work|integration) as bounded specialist work")
_BUDGET_ONLY = re.compile(
    r"(?:continuation_exhausted:\s*)?(?:please |ask (?:a |the )?human to )?"
    rf"(?:{_ACTION})(?:\s+(?:or|and)\s+(?:{_ACTION}))*"
    r"(?:,? then unblock (?:this|the) card)?[.!?]?", re.IGNORECASE,
)
# These are handoff context, not requests for a product/security decision. Unknown
# sentences stay human; do not turn this into a keyword match over arbitrary prose.
_CONTEXT = re.compile(
    r"(?:review the preserved implementation/checkpoint \S+ for this card; "
    r"the dispatcher refused continuation after its \d+-run limit"
    r"|latest unshipped commit\s*[a-z0-9]+ passes\s*\d+ [a-z ]*tests"
    r"(?: and\s*\d+ [a-z ]*tests)?; [a-z ,/-]+ still remains?"
    r"|this is not delivered; keep the originating tracker unfinished)[.!]?", re.IGNORECASE,
)


def _budget_only(reason: str | None) -> bool:
    sentences = re.split(r"(?<=[.!?])\s+", " ".join((reason or "").split()))
    found = False
    for sentence in sentences:
        sentence = re.sub(r"^\d+[.)]\s*", "", sentence)
        if _BUDGET_ONLY.fullmatch(sentence):
            found = True
        elif not _CONTEXT.fullmatch(sentence):
            return False
    return found


def is_exhausted_budget_block(conn, task_id: str, kind: str | None, reason: str | None) -> bool:
    """Require both the current run's refusal and an exclusively budget-management ask.

    Unknown prose is never suppressed: human decisions and capability requests remain
    human, even when a worker mentions continuation exhaustion in their explanation.
    """
    if kind not in ("needs_input", "transient") or not _budget_only(reason):
        return False
    return conn.execute(
        "SELECT 1 FROM tasks t JOIN task_events e ON e.task_id = t.id "
        "AND e.run_id = t.current_run_id WHERE t.id = ? AND t.status = 'running' "
        "AND e.kind = 'continuation_exhausted' LIMIT 1", (task_id,),
    ).fetchone() is not None
