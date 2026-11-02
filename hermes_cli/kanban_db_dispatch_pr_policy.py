"""Which pull URLs in a card's publication comments arm the ``active_pr`` hold. [daveey]

Input is the card's own or unknown-provenance comments only: comments known to come from
another card's run or a chat are FYI and are separated out by
:func:`kanban_db_dispatch_pr_state._foreign_pr_comment_ids` before this runs.

Among those candidates, a pull that is another live card's ``completion_contract`` is not
this card's work, and neither is a pull that appears only in comments saying it does not
block this card. This card's own ``completion_contract`` always holds.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Optional

# A comment whose purpose is to say the named pull does not block this card.
# "doesn't block the merge" is not that purpose; the object has to be the card.
_NONBLOCKING_RE = re.compile(
    r"\b(?:does(?:n['\u2019]t| not)|do(?:n['\u2019]t| not)|won['\u2019]t|will not)\s+block\s+"
    r"(?:you|this\s+(?:card|task)|the\s+card)\b",
    re.IGNORECASE,
)


def norm_pr(url: str) -> str:
    return url.rstrip("/").lower()


def _says_pull_does_not_block(body: str) -> bool:
    from hermes_cli import kanban_db_dispatch as dispatch

    # A disclaimer in a mixed-URL comment may refer to a different pull from the
    # publication. Ambiguous prose must not release the duplicate-work guard.
    urls = dispatch._RESPAWN_GUARD_PR_URL_RE.findall(body)
    if len(urls) != 1 or re.search(r"\b(?:opened|created|published|pushed)\b", body, re.IGNORECASE):
        return False
    disclaimer = _NONBLOCKING_RE.search(body)
    if disclaimer is None:
        return False
    # Recognize a deliberately narrow informational statement: the URL is the
    # subject, directly or via "<URL> ... . It ...". Unrelated lint/CI subjects,
    # publication statements, extra clauses and ambiguous prose remain guarded.
    prefix = body[:disclaimer.start()].replace(urls[0], "PULL")
    return bool(
        re.fullmatch(r"\s*(?:See\s+)?PULL(?:\s+[^.!?\n]+\.\s+It)?\s+", prefix, re.IGNORECASE)
        and re.fullmatch(r"[.!]?\s*", body[disclaimer.end():])
    )


def _own_contract_key(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """This card's completion_contract when it is a URL, else None."""
    row = conn.execute("SELECT completion_contract FROM tasks WHERE id = ?", (task_id,)).fetchone()
    contract = row[0] if row is not None else None
    if not isinstance(contract, str) or not contract.lower().startswith("http"):
        return None
    return norm_pr(contract)


def _foreign_live_contract_keys(conn: sqlite3.Connection, task_id: str, keys: set[str]) -> set[str]:
    """Pull URLs that are another live card's completion_contract.

    Done and archived cards are not live: a finished contract is not an owner that
    excuses a duplicate-PR mention.
    """
    if not keys:
        return set()
    placeholders = ",".join("?" * len(keys))
    rows = conn.execute(
        "SELECT lower(rtrim(completion_contract, '/')) FROM tasks "
        "WHERE id != ? AND status NOT IN ('done', 'archived') "
        f"AND lower(rtrim(completion_contract, '/')) IN ({placeholders})",
        (task_id, *sorted(keys)),
    ).fetchall()
    return {row[0] for row in rows}


def holding_urls(conn: sqlite3.Connection, task_id: str, mentions: list[tuple[str, str]]) -> set[str]:
    """Normalized pull URLs among candidate ``(url, comment body)`` mentions that hold."""
    by_url: dict[str, list[str]] = {}
    for url, body in mentions:
        by_url.setdefault(norm_pr(url), []).append(body)
    if not by_url:
        return set()
    own = _own_contract_key(conn, task_id)
    foreign = _foreign_live_contract_keys(conn, task_id, set(by_url) - {own})
    return {
        key for key, bodies in by_url.items()
        if key == own or (key not in foreign and not all(map(_says_pull_does_not_block, bodies)))
    }
