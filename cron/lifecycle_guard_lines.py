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


def _double_span(text: str, start: int, *, shlex_quotes: bool) -> tuple[int, str]:
    """A substitution's quotes belong to its own frame, not the enclosing word."""
    pos = copied = start
    parts = ['"']
    while pos < len(text):
        char = text[pos]
        if char == "\\":
            pos += 2
            continue
        if char == '"':
            pos += 1
            break
        if char in "$`":
            end, span = _expansion(text, pos, in_double=True, shlex_quotes=shlex_quotes)
            if span:
                if shlex_quotes:
                    span = span.replace("\\", "\\\\").replace('"', '\\"')
                parts.extend((text[copied:pos], span))
                pos = copied = end
                continue
        pos += 1
    parts.append(text[copied:pos])
    return pos, "".join(parts)


def _command_span(text: str, start: int, *, shlex_quotes: bool,
                  breaks: list[int] | None = None) -> tuple[int, str]:
    """Find a substitution's closing parenthesis in its own shell comment frame.

    With *breaks*, record (relative to the returned span) each newline that separates commands
    in this frame or a nested parameter/command frame, so a logical-line split still sees them."""
    pos = copied = start
    depth = 1
    word_start = True
    parts = ["$("]
    # Open `case` statements as [depth, phase]. In a pattern list a bare `)` ends the pattern,
    # not the substitution (`$(case x in x) true;; esac)`), and no word but a leading `esac`
    # is reserved there (`(case)` is a pattern, not a nested `case`). Phases: "subject" and
    # "in" before the clauses, "pattern" at a clause start, "pattern-word" inside a pattern
    # list (after `(`, `|` or a word), "body" in a clause's command list.
    cases: list[list] = []
    at_command = True
    while pos < len(text):
        char = text[pos]
        if char == "#" and word_start:
            end = text.find("\n", pos)
            end = len(text) if end < 0 else end
            parts.append(text[copied:pos])
            if not shlex_quotes:
                # A comment's trailing backslash continues nothing (the newline still ends
                # it), so it must not survive into a later continuation join.
                parts.append(text[pos:end].rstrip("\r").rstrip("\\"))
            pos = copied = end
            continue
        if word_start and char not in _WORD_BREAKS:
            word = _plain_word(text, pos)
            top = cases[-1] if cases and cases[-1][0] == depth else [depth, ""]
            phase = top[1]
            if phase == "subject":
                top[1] = "in"
            elif phase == "in":
                if word == "in":
                    top[1] = "pattern"
            elif phase == "pattern" and word == "esac":
                cases.pop()
            elif phase in ("pattern", "pattern-word"):
                top[1] = "pattern-word"
            elif word == "case" and at_command:
                cases.append([depth, "subject"])
            elif word == "esac" and phase == "body" and at_command:
                cases.pop()
            at_command = phase in ("", "body") and word in _COMMAND_PREFIX_WORDS
        if char == "\\":
            if text[pos + 1:pos + 2] != "\n":
                word_start = False
            pos += 2
            continue
        if char == "'":
            pos = _quoted_end(text, pos + 1, char, escapes=False)
            word_start = False
            continue
        nested: list[int] | None = None
        if char == '"':
            end, span = _double_span(text, pos + 1, shlex_quotes=shlex_quotes)
        elif char in "$`":
            nested = [] if breaks is not None else None
            end, span = _expansion(text, pos, in_double=False, shlex_quotes=shlex_quotes,
                                   breaks=nested)
        else:
            end, span = pos, ""
        if span:
            _shift_breaks(breaks, nested, _out_len(parts) + pos - copied)
            parts.extend((text[copied:pos], span))
            pos = copied = end
            word_start = False
            continue
        top = cases[-1] if cases and cases[-1][0] == depth else [depth, ""]
        phase = top[1]
        if char == ";" and phase == "body" and text[pos + 1:pos + 2] in (";", "&"):
            # `;;`, `;&` or `;;&` ends a clause: a pattern list (or `esac`) follows.
            pos += 3 if text.startswith(";;&", pos) else 2
            top[1] = "pattern"
            at_command = word_start = True
            continue
        if char == "\n" and breaks is not None:
            breaks.append(_out_len(parts) + pos - copied)
        elif char == "(" and phase == "pattern":
            top[1] = "pattern-word"  # a pattern list's optional leading parenthesis
        elif char == "(":
            depth += 1
        elif char == ")":
            if phase in ("pattern", "pattern-word"):
                top[1] = "body"
                at_command = True  # a case pattern ends; its command list follows
            else:
                depth -= 1
                if not depth:
                    pos += 1
                    break
        if char in _COMMAND_BREAKS:
            at_command = True
        word_start = char in _WORD_BREAKS
        pos += 1
    parts.append(text[copied:pos])
    return pos, "".join(parts)


def _parameter_span(text: str, start: int, *, shlex_quotes: bool,
                    breaks: list[int] | None = None) -> tuple[int, str]:
    """Keep parameter words opaque to the OUTER comment/line scanner.

    Nested expansions and quoted closing braces do not end the parameter. Contents
    remain visible to the guard's separate substitution and referenced-command walks.
    """
    stack = ["}"]
    pos = copied = start
    parts = [text[start - 2:start]]
    while pos < len(text) and stack:
        char = text[pos]
        if char == "`" or text.startswith("$'", pos) or (
            text.startswith("$(", pos) and not text.startswith("$((", pos)
        ):
            # Nested backticks need the same inner-comment normalization as root
            # substitutions; otherwise a comment's quote leaks into outer shlex.
            nested = [] if breaks is not None else None
            end, span = _expansion(text, pos, in_double=False, shlex_quotes=shlex_quotes,
                                   breaks=nested)
            _shift_breaks(breaks, nested, _out_len(parts) + pos - copied)
            parts.extend((text[copied:pos], span))
            pos = copied = end
            continue
        if char == "\\":
            pos += 2
            continue
        if char == '"':
            end, span = _double_span(text, pos + 1, shlex_quotes=shlex_quotes)
            parts.extend((text[copied:pos], span))
            pos = copied = end
            continue
        if char == "'":
            pos = _quoted_end(text, pos + 1, char, escapes=False)
            continue
        if char == "\n" and breaks is not None:
            # Fail closed as an unframed scan does: should a nested frame end early (a
            # construct its walker misreads), the newline after it still ends the line.
            breaks.append(_out_len(parts) + pos - copied)
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


def _out_len(parts: list[str]) -> int:
    return sum(map(len, parts))


def _shift_breaks(breaks: list[int] | None, nested: list[int] | None, offset: int) -> None:
    if breaks is not None and nested:
        breaks.extend(offset + brk for brk in nested)


def _expansion(text: str, pos: int, *, in_double: bool, shlex_quotes: bool,
               breaks: list[int] | None = None) -> tuple[int, str]:
    """Return an opaque span, or an empty span when ordinary scanning should proceed.

    *breaks* collects command-separating newlines of nested ``$(...)`` frames (see
    ``_command_span``); backtick bodies and double-quoted frames do not report them."""
    if text.startswith("${", pos):
        return _parameter_span(text, pos + 2, shlex_quotes=shlex_quotes, breaks=breaks)
    elif text.startswith("$(", pos) and not text.startswith("$((", pos):
        return _command_span(text, pos + 2, shlex_quotes=shlex_quotes, breaks=breaks)
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
# Characters after which the next word is in command position (a reserved word is recognized).
_COMMAND_BREAKS = frozenset(";&|(\n")
# Reserved words after which the next word is still in command position.
_COMMAND_PREFIX_WORDS = frozenset({
    "if", "then", "else", "elif", "do", "while", "until", "!", "{", "time",
})
_PLAIN_WORD = re.compile(r"!|\{|[A-Za-z_][A-Za-z0-9_]*")


def _plain_word(text: str, pos: int) -> str | None:
    """The unquoted word starting at *pos* when it is a bare name (a candidate reserved word)."""
    match = _PLAIN_WORD.match(text, pos)
    if match is None or (match.end() < len(text) and text[match.end()] not in _WORD_BREAKS):
        return None
    return match.group()

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
        if (ch in "$`" and not in_comment and not in_single and not escape
                and not text.startswith("$(", pos)):
            # Root $() traversal stays visible to logical-line consumers; only
            # nested opaque words need the substitution framed as a whole.
            breaks: list[int] | None = [] if split and not in_double else None
            end, span = _expansion(text, pos, in_double=in_double, shlex_quotes=shlex_quotes,
                                   breaks=breaks)
            if span:
                if shlex_quotes and in_double:
                    # shlex has no nested shell frames: keep the normalized
                    # substitution's quotes inside the enclosing quoted word.
                    span = span.replace("\\", "\\\\").replace('"', '\\"')
                # A newline separating commands in a nested $() still ends the logical
                # line (as an unframed scan does), so the command after it is read.
                prev = 0
                for brk in breaks or ():
                    current.append(span[prev:brk])
                    lines.append("".join(current))
                    current = []
                    prev = brk + 1
                current.append(span[prev:])
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
