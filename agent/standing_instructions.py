"""Standing instructions: the user's lasting directives ("don't block, don't wait"), kept word for word. [daveey]

A standing instruction governs every later decision in a session or card, so it must survive context
compaction and reach Kanban workers verbatim. Two shapes count:

* an explicit ``standing: <exact words>`` line (written by agents on cards, by the session-mentions relay,
  and by the compaction summary itself, so a later compaction re-reads it);
* a "don't wait"-class directive inside a REAL user message (or a relayed one), taken as the quoted span
  or sentence that holds it, unchanged.

Pure text helpers, no I/O: the context compressor, ``build_worker_context`` and the kanban_block guard
share them so all three agree on what is standing.
"""
from __future__ import annotations

import re
from typing import Callable, Iterable

STANDING_HEADING = "## Standing instructions (still in force)"
_MAX_LINES = 20
_MAX_CHARS = 400

# An explicit marker line: "standing: ...", optionally bulleted/quoted.
_STANDING_LINE = re.compile(r"^[\s>*\-•]*standing:\s*(?P<words>\S.*?)\s*$", re.IGNORECASE | re.MULTILINE)
# "don't block / dont wait / do not pause / never stop to ask / no check-ins" etc.
DONT_WAIT = re.compile(
    r"\b(?:do\s*not|don['’]?t|dont|never)\s+(?:stop\s+to\s+)?(?:block|wait|pause|ask|stop)\b"
    r"|\bno\s+(?:check[\s-]?ins?|waiting|pausing)\b",
    re.IGNORECASE,
)
_QUOTED = re.compile(r"'([^'\n]{4,400})'|\"([^\"\n]{4,400})\"|“([^”\n]{4,400})”|‘([^’\n]{4,400})’")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")


def _clean(words: str) -> str:
    return " ".join(words.split())[:_MAX_CHARS]


def explicit_lines(text: str) -> list[str]:
    """``standing:`` lines in any text, words unchanged (whitespace collapsed)."""
    if not isinstance(text, str) or "standing:" not in text.lower():
        return []
    return [_clean(m.group("words")) for m in _STANDING_LINE.finditer(text)]


def directive_spans(text: str) -> list[str]:
    """"Don't wait"-class directives in user-authored text: the quote or sentence holding each, unchanged."""
    if not isinstance(text, str) or not DONT_WAIT.search(text):
        return []
    out: list[str] = []
    taken: list[tuple[int, int]] = []
    for q in _QUOTED.finditer(text):
        inner = next(g for g in q.groups() if g is not None)
        if DONT_WAIT.search(inner):
            out.append(_clean(inner))
            taken.append(q.span())
    pos = 0
    for part in _SENTENCE_END.split(text):
        start = text.find(part, pos)
        pos = start + len(part)
        if not part.strip() or any(a <= start < b or a < pos <= b for a, b in taken):
            continue
        if DONT_WAIT.search(part) and not _STANDING_LINE.match(part):
            out.append(_clean(part))
    return out


def extract(text: str, *, user_authored: bool) -> list[str]:
    """Standing words in one message: explicit lines always; directives only from user-authored text."""
    found = explicit_lines(text)
    if user_authored:
        found += directive_spans(text)
    return found


def dedupe(lines: Iterable[str], limit: int = _MAX_LINES) -> list[str]:
    """Keep first-seen order, drop repeats (case-insensitive) and keep the newest ``limit``."""
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        key = line.casefold().strip(" .")
        if line and key not in seen:
            seen.add(key)
            out.append(line)
    return out[-limit:]


def restricted_explicit_lines(text: str, keep: Callable[[str, str], bool]) -> list[tuple[str, str]]:
    """Opt-in direct promotion: ``(raw_line, words)`` whose every representation ``keep`` accepts.

    Unlike :func:`explicit_lines`, each ``str.splitlines()`` physical line is matched separately, so no
    candidate crosses LF/CRLF/CR/VT/FF/FS/GS/RS/NEL/U+2028/U+2029. ``keep(raw_line, candidate)`` sees
    the untruncated words, the collapsed words, the 400-character words and the rendered ``standing:``
    line; one refusal discards the candidate before any dedupe/limit. Source text is never altered.
    """
    if not isinstance(text, str) or "standing:" not in text.lower():
        return []
    out: list[tuple[str, str]] = []
    for raw in text.splitlines():
        m = _STANDING_LINE.match(raw)
        if not m:
            continue
        words = m.group("words")
        collapsed = " ".join(words.split())
        final = collapsed[:_MAX_CHARS]
        if all(keep(raw, candidate) for candidate in (words, collapsed, final, f"standing: {final}")):
            out.append((raw, final))
    return out


def dedupe_candidates(pairs: Iterable[tuple[str, str]], limit: int = _MAX_LINES) -> list[tuple[str, str]]:
    """:func:`dedupe` over already-accepted ``(raw_line, words)``; keys never change emitted words."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for raw, words in pairs:
        key = words.casefold().strip(" .")
        if words and key not in seen:
            seen.add(key)
            out.append((raw, words))
    return out[-limit:]


def says_dont_wait(lines: Iterable[str]) -> list[str]:
    """The standing lines that forbid waiting/blocking on the user."""
    return [line for line in lines if DONT_WAIT.search(line)]


_SECTION_NOTE = ("(The user's own standing instructions, word for word. Unlike the rest of this summary they are "
                 "ACTIVE: they still govern every decision, including whether to block, pause or wait on the user.)")
_SECTION_RE = re.compile(r"\n*" + re.escape(STANDING_HEADING) + r"\n(?:[ \t]*standing:.*\n?)*(?:[ \t]*\(The user's own "
                         r"standing instructions[^)]*\))?")


def strip_section(text: str) -> str:
    """Remove a rendered section (lines are re-collected first), keeping whatever follows it."""
    return _SECTION_RE.sub("", text)


def render_section(lines: Iterable[str]) -> str:
    """Summary/context section: one ``standing:`` line each, so the next compaction re-reads them."""
    lines = dedupe(lines)
    if not lines:
        return ""
    body = "\n".join(f"standing: {line}" for line in lines)
    return f"\n\n{STANDING_HEADING}\n{body}\n{_SECTION_NOTE}"
