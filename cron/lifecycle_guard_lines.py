"""How the lifecycle guard's command scan splits a command into logical shell lines.

Moved out of ``cron/lifecycle_guard.py`` to keep that module under its FILE_LINES cap.

A ``#`` starts a comment only where bash starts a word: at the line's start or after unquoted,
unescaped whitespace or an operator character (``;&|()<>``). Inside a word (``a#b``, ``x=1#2``,
``$#``, ``${#x}``) it is text, and a quote inside a comment is text too (``echo hi # it's``). A
comment ends at its newline even after a backslash (``# x \\`` does not continue the line).
"""

from __future__ import annotations

import re
import shlex

from cron.lifecycle_guard_printf import decode_c_escapes


def _quoted_end(text: str, start: int, closer: str, *, escapes: bool = True) -> int:
    """Backticks close before their contents are parsed (even inside a comment)."""
    pos = start
    while pos < len(text):
        if text[pos] == closer:
            return pos + 1
        pos += 2 if escapes and text[pos] == "\\" else 1
    return len(text)


def _parameter_span(text: str, start: int, *, shlex_quotes: bool) -> tuple[int, str]:
    """Keep parameter words opaque to the OUTER comment/line scanner.

    Nested expansions and quoted closing braces do not end the parameter. Contents
    remain visible to the guard's separate substitution and referenced-command walks.
    """
    stack = ["}"]
    pos = copied = start
    parts = [text[start - 2:start]]
    while pos < len(text) and stack:
        char = text[pos]
        if char == "`" or text.startswith("$'", pos):
            # Nested backticks need the same inner-comment normalization as root
            # substitutions; otherwise a comment's quote leaks into outer shlex.
            end, span = _expansion(text, pos, in_double=False, shlex_quotes=shlex_quotes)
            parts.extend((text[copied:pos], span))
            pos = copied = end
            continue
        if char == "\\":
            pos += 2
            continue
        if char in "'\"`":
            pos = _quoted_end(text, pos + 1, char, escapes=char != "'")
            continue
        if text.startswith(("${", "$("), pos):
            stack.append("}" if text[pos + 1] == "{" else ")")
            pos += 2
            continue
        if char == stack[-1]:
            stack.pop()
        elif char == "(" and stack[-1] == ")":
            stack.append(")")
        pos += 1
    parts.append(text[copied:pos])
    return pos, "".join(parts)


def _expansion(text: str, pos: int, *, in_double: bool, shlex_quotes: bool) -> tuple[int, str]:
    """Return an opaque span, or an empty span when ordinary scanning should proceed."""
    if text.startswith("${", pos):
        return _parameter_span(text, pos + 2, shlex_quotes=shlex_quotes)
    elif text[pos] == "`":
        end = _quoted_end(text, pos + 1, "`")
        if shlex_quotes and text[end - 1:end] == "`":
            # A quote in the inner comment must not poison POSIX tokenization of
            # the command AFTER this substitution either.
            body = strip_shell_comments(text[pos + 1:end - 1], shlex_quotes=True)
            return end, "`" + body + "`"
    elif not in_double and text.startswith("$'", pos):
        end = _quoted_end(text, pos + 2, "'")
        if shlex_quotes and text[end - 1:end] == "'":
            value = decode_c_escapes(text[pos + 2:end - 1], echo_style=False)
            # Bash truncates this quoted span at NUL, not the rest of the word.
            return end, shlex.quote(value.split("\0", 1)[0])
    else:
        return pos, ""
    return end, text[pos:end]

# Characters after which an unquoted `#` starts a word, so a comment (bash's metacharacters).
_WORD_BREAKS = frozenset(" \t\n;&|()<>")
# A backslash-newline continuation and the indentation after it (what the regex scans collapse).
_CONTINUATION = re.compile(r"\\\r?\n[ \t]*")


def _scan(text: str, *, split: bool, keep_comments: bool, shlex_quotes: bool = False) -> list[str]:
    """*text* as one string or (*split*) as logical lines: newlines outside quotes separate them
    (a quoted newline is data). Honors quotes and escapes. Comments are dropped, or with
    *keep_comments* kept without the backslashes they end with (no continuation)."""
    lines: list[str] = []
    current: list[str] = []
    comment: list[str] = []
    in_single = in_double = escape = in_comment = False
    word_start = True
    end = 0
    for pos, ch in enumerate(text):
        if pos < end:
            continue
        if ch in "$`" and not in_comment and not in_single and not escape:
            end, span = _expansion(text, pos, in_double=in_double, shlex_quotes=shlex_quotes)
            if span:
                current.append(span)
                word_start = False
                continue
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
            current.append(ch)
            word_start = False  # escaped metacharacters are word content
            continue
        elif ch == "\\" and not in_single:
            escape = True
        elif ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double and word_start:
            in_comment = True
            comment = [ch]
            continue
        elif split and ch == "\n" and not in_single and not in_double:
            lines.append("".join(current))
            current = []
            word_start = True
            continue
        current.append(ch)
        if not escape:
            word_start = not in_single and not in_double and ch in _WORD_BREAKS
    if in_comment and keep_comments:
        current.append("".join(comment))
    if current or not split:
        lines.append("".join(current))
    return lines


def strip_shell_comments(text: str, *, shlex_quotes: bool = False) -> str:
    """Remove comments; optionally render ANSI-C words for POSIX shlex."""
    return _scan(text, split=False, keep_comments=False, shlex_quotes=shlex_quotes)[0]


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
