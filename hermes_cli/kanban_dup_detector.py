"""Count cards closed as a duplicate of a sibling from the same creator, and open one card for the pattern. [daveey]

On 11-13 three cards closed as duplicates of cards their own creator had filed earlier (t_01a1d44d9e68cca4,
t_01a1d4917584d038, t_01a1d4e145f389a5), and no one noticed until an owner chat looked: nothing counted them.
The create-time check (:mod:`hermes_cli.kanban_overlap_own`) now refuses those three, but it is a heuristic
(shared title words, or at least 3 rare code identifiers): a duplicate worded apart with no shared identifier
still passes it. This detector is the net behind it.

When a card completes with a summary or result that says it duplicates, or was superseded by, a card id
(``duplicate of t_X``, ``duplicates t_X``, ``Superseded ... card t_X``), and ``t_X`` has the same creator task,
one open card on that board collects the pattern (a denial such as ``Not a duplicate of t_X`` does not count):
- no open detector card: one is created (``ready``, assignee ``default``) naming this pair;
- an open one exists: this pair is added to it as a comment.
After that card is done or archived, the next occurrence opens a new one. Never raises.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Optional

_log = logging.getLogger(__name__)

KEY_PREFIX = "detector:same-creator-duplicate:"
TITLE = "Same-creator duplicate cards: create_overlap let a creator file the same work twice"
_CARD = r"t_[0-9a-f]{8,16}"
# "duplicate of t_X", "duplicates t_X", "DUPLICATE of t_X", "superseded by ... t_X", "Superseded: ... card t_X".
_KEYWORD = re.compile(r"\b(?:duplicat\w*|supersed\w*)\b", re.IGNORECASE)
_AFTER = re.compile(rf"[^.\n]{{0,120}}?\b({_CARD})\b")
# A denial that governs the keyword in its own clause: "Not a duplicate of t_X", "No duplicates found; parent
# t_X", "isn't a duplicate", "non-duplicate", "0 duplicates", "Not a real duplicate", "No other duplicates" (PR
# #2329 review). Up to two words may sit between. Only "non" takes a hyphen, so "No-op as duplicate of t_X" is
# no denial, and a window holding a joining word ("as", "with", "because", ...) negates another word, not the
# keyword ("no changes as duplicate of t_X", "Never started as duplicate of t_X", "no work because duplicate of
# t_X") (PR #2429 reviews, PR #2481 review 5896093750). After not/never, a judging verb before "as" still denies
# ("Not marked as a duplicate of t_X"), and so does "as" right after it ("Closed, not as duplicate of t_X") (PR
# #2535 review 5896481270). An earlier clause ("No code changes; duplicate of t_X") does not deny it.
_WORD = r"[\w'\u2019]+\s+"
_JOIN = r"(?:as|with|because|since|so|but|due|hence|thus)"
_NOT_NOUN = rf"(?:(?!{_JOIN}\b){_WORD})"
_JUDGED = (r"(?:marked|considered|flagged|treated|closed|counted|filed|labell?ed|tagged|reported|listed|deemed|"
           r"regarded|classified|recorded|identified|confirmed|resolved|seen)")
_NEGATED = re.compile(rf"(?:\b(?:not|never|\w+n['\u2019]t)\s+(?:{_NOT_NOUN}{{0,2}}"
                      rf"|(?:{_NOT_NOUN}?{_JUDGED}\s+)?(?:as\s+)?(?:(?:an?|the)\s+)?)"
                      rf"|(?:\b(?:no|zero)|(?<![\w.])0)\s+{_NOT_NOUN}{{0,2}}"
                      r"|\bnon[\s-]+)$", re.IGNORECASE)
# "Checked for duplicates: none", "Duplicates found: 0": a count of none right after the keyword that ends the
# clause ("Duplicate: none other than t_X" names t_X).
_NONE_AFTER = re.compile(r"\s*(?:found\s*)?[:=\-\u2013\u2014]?\s*(?:none|nil|zero|0)(?:\s+found)?(?=\s*(?:[.;:!?,)\n]|$))",
                         re.IGNORECASE)
# An aside that names a keyword but no card ("Closed as duplicate (not superseded) of t_X") is blanked first, so
# its keyword neither stops nor denies the outer one (PR #2481 review).
_ASIDE = re.compile(r"\([^()\n]*\)")
_CLAUSE_END = re.compile(r"[.;:!?,\n]")


def _negated(text: str, start: int) -> bool:
    before = text[max(0, start - 80):start]
    ends = [m.end() for m in _CLAUSE_END.finditer(before)]
    return bool(_NEGATED.search(before[ends[-1] if ends else 0:]))


def sibling_named(text: str) -> list[str]:
    """Card ids the text says this card duplicates or was superseded by, in order; a denied one is skipped.

    A keyword's forward window stops at the next keyword, so "Duplicate check: not a duplicate of t_X" is the
    denied second keyword's, not the first one's (PR #2429 review)."""
    text = _ASIDE.sub(lambda m: m.group(0) if re.search(_CARD, m.group(0)) or not _KEYWORD.search(m.group(0))
                      else " " * len(m.group(0)), text or "")
    found = []
    keywords = list(_KEYWORD.finditer(text))
    for i, kw in enumerate(keywords):
        if _negated(text, kw.start()) or _NONE_AFTER.match(text, kw.end()):
            continue
        stop = keywords[i + 1].start() if i + 1 < len(keywords) else len(text)
        card = _AFTER.match(text, kw.end(), stop)
        if card:
            found.append(card.group(1))
    return list(dict.fromkeys(found))


def _creator(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    row = conn.execute("SELECT json_extract(payload, '$.creator_task_id') FROM task_events "
                       "WHERE task_id = ? AND kind = 'created' ORDER BY id LIMIT 1", (task_id,)).fetchone()
    return row[0] if row and isinstance(row[0], str) and row[0] else None


def same_creator_sibling(conn: sqlite3.Connection, task_id: str, text: str) -> Optional[tuple[str, str]]:
    """``(sibling, creator)`` when ``text`` names a card from this card's own creator, else None."""
    creator = _creator(conn, task_id)
    if not creator:
        return None
    for sibling in sibling_named(text):
        if sibling != task_id and _creator(conn, sibling) == creator:
            return sibling, creator
    return None


def _occurrence(task_id: str, sibling: str, creator: str, text: str) -> str:
    quote = " ".join((text or "").split())[:300]
    return f"- {task_id} closed as a duplicate of {sibling}; both created by {creator}. Its handoff: \"{quote}\""


def _body(line: str) -> str:
    return ("A card completed as a duplicate of, or superseded by, a card its own creator had filed earlier, so "
            "a worker ran on work that was already owned. `kanban_create`'s overlap check compares a creator's "
            "own open cards (hermes_cli/kanban_overlap.py, hermes_cli/kanban_overlap_own.py) and missed this "
            "pair.\n\n"
            "Do: for each pair below, replay the second create through `kanban_overlap._find` and name the rule "
            "that admitted it. Then either narrow that rule with a replay test (same fixture as "
            "tests/tools/test_kanban_create_overlap_same_creator.py: every legitimate fan-out still admitted) or "
            "record why the pair cannot be told apart from fan-out at create time.\n\n"
            "Later occurrences are added here as comments while this card is open.\n\n"
            f"Occurrences:\n{line}\n")


def note_completion(conn: sqlite3.Connection, task_id: str, *texts: Optional[str]) -> Optional[str]:
    """Record a same-creator duplicate completion; returns the detector card id it opened or commented on."""
    from hermes_cli import kanban_db as kb
    try:
        text = "\n".join(t for t in texts if t)
        hit = same_creator_sibling(conn, task_id, text) if text else None
        if hit is None:
            return None
        sibling, creator = hit
        line = _occurrence(task_id, sibling, creator, text)
        row = conn.execute("SELECT id, body FROM tasks WHERE idempotency_key LIKE ? AND status NOT IN ('done', "
                           "'archived') ORDER BY created_at LIMIT 1", (KEY_PREFIX + "%",)).fetchone()
        if row:
            # The first occurrence is the body's line, later ones are comments: either means this card is noted.
            seen = f"\n- {task_id} " in (row[1] or "") or conn.execute(
                "SELECT 1 FROM task_comments WHERE task_id = ? AND body LIKE ?", (row[0], f"- {task_id} %")).fetchone()
            if not seen:
                kb.add_comment(conn, row[0], "dup-detector", line)
            return row[0]
        return kb.create_task(conn, title=TITLE, body=_body(line), assignee="default", created_by="dup-detector",
                              idempotency_key=KEY_PREFIX + task_id)
    except Exception:  # health: allow BLE001 -- an observer must never break a completion
        _log.warning("same-creator duplicate detector failed for %s", task_id, exc_info=True)
        return None


def describe(conn: sqlite3.Connection) -> list[dict]:
    """Open detector cards (for tests and audits)."""
    return [{"id": r[0], "status": r[1], "key": r[2]} for r in conn.execute(
        "SELECT id, status, idempotency_key FROM tasks WHERE idempotency_key LIKE ?", (KEY_PREFIX + "%",))]


__all__ = ["KEY_PREFIX", "TITLE", "describe", "note_completion", "same_creator_sibling", "sibling_named"]
