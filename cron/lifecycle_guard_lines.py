"""How the lifecycle guard's command scan splits a command into logical shell lines.

Moved out of ``cron/lifecycle_guard.py`` to keep that module under its FILE_LINES cap.

A ``#`` starts a comment only where bash starts a word: at the line's start or after unquoted,
unescaped whitespace or an operator character (``;&|()<>``). Inside a word (``a#b``, ``x=1#2``,
``$#``, ``${#x}``) it is text, and a quote inside a comment is text too (``echo hi # it's``). A
comment ends at its newline even after a backslash (``# x \\`` does not continue the line).
"""

from __future__ import annotations

import re

# Characters after which an unquoted `#` starts a word, so a comment (bash's metacharacters).
_WORD_BREAKS = frozenset(" \t\r\n;&|()<>")
# A backslash-newline continuation and the indentation after it (what the regex scans collapse).
_CONTINUATION = re.compile(r"\\\r?\n[ \t]*")


def _scan(text: str, *, split: bool, keep_comments: bool) -> list[str]:
    """*text* as one string or (*split*) as logical lines: newlines outside quotes separate them
    (a quoted newline is data). Honors quotes and escapes. Comments are dropped, or with
    *keep_comments* kept without the backslashes they end with (no continuation)."""
    lines: list[str] = []
    current: list[str] = []
    comment: list[str] = []
    in_single = in_double = escape = in_comment = False
    previous = "\n"
    for ch in text:
        if in_comment:
            if ch != "\n":
                comment.append(ch)
                continue
            in_comment = False
            if keep_comments:
                current.append("".join(comment).rstrip("\r").rstrip("\\"))
            comment = []
        if escape:
            escape = False
            if ch == "\n":  # a continuation joins the word: `a\<newline>#b` is `a#b`
                current.append(ch)
                continue
        elif ch == "\\" and not in_single:
            escape = True
        elif ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double and previous in _WORD_BREAKS:
            in_comment = True
            comment = [ch]
            continue
        elif split and ch == "\n" and not in_single and not in_double:
            lines.append("".join(current))
            current = []
            previous = ch
            continue
        current.append(ch)
        if not escape:
            previous = ch
    if in_comment and keep_comments:
        current.append("".join(comment))
    if current or not split:
        lines.append("".join(current))
    return lines


def strip_shell_comments(text: str) -> str:
    """*text* with each shell comment removed (its newline kept)."""
    return _scan(text, split=False, keep_comments=False)[0]


def join_line_continuations(text: str) -> str:
    """*text* with each backslash-newline continuation (and the indentation after it) collapsed
    to a space, as the regex scans read it. A comment's trailing backslash continues nothing, and
    a continuation glued to a ``#`` joins the word (``a\\<newline>#b`` is ``a#b``, no comment)."""
    kept = _scan(text, split=False, keep_comments=True)[0]

    def join(match: re.Match[str]) -> str:
        glued = match.group().endswith("\n") and kept.startswith("#", match.end())
        return "" if glued else " "

    return _CONTINUATION.sub(join, kept)


def _split_logical_lines(text: str) -> list[str]:
    """Split on newlines outside quotes (a quoted newline is data, not a separator); honors
    escapes and drops comments, so a quote in a comment opens nothing."""
    return _scan(text, split=True, keep_comments=False)
