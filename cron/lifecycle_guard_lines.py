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


class _CaseClauses:
    """Open ``case`` statements of one ``$(...)`` frame, as ``[depth, phase]``.

    In a pattern list a bare ``)`` ends the pattern, not the substitution
    (``$(case x in x) true;; esac)``), and no word but a leading ``esac`` is reserved there
    (``(case)`` is a pattern, not a nested ``case``). Phases: "subject" and "in" before the
    clauses, "pattern" at a clause start, "pattern-word" inside a pattern list (after ``(``,
    ``|`` or a word), "body" in a clause's command list.
    """

    def __init__(self) -> None:
        self._open: list[list] = []

    def _top(self, depth: int) -> list:
        return self._open[-1] if self._open and self._open[-1][0] == depth else [depth, ""]

    def word(self, word: str | None, depth: int, *, at_command: bool) -> bool:
        """Advance on a word at *depth*; return whether the next word is in command position."""
        top = self._top(depth)
        phase = top[1]
        if phase == "subject":
            top[1] = "in"
        elif phase == "in":
            if word == "in":
                top[1] = "pattern"
        elif phase == "pattern" and word == "esac":
            self._open.pop()
        elif phase in ("pattern", "pattern-word"):
            top[1] = "pattern-word"
        elif word == "case" and at_command:
            self._open.append([depth, "subject"])
        elif word == "esac" and phase == "body" and at_command:
            self._open.pop()
        return phase in ("", "body") and word in _COMMAND_PREFIX_WORDS

    def clause_end(self, text: str, pos: int, depth: int) -> int:
        """Length of a ``;;``, ``;&`` or ``;;&`` ending a clause at *pos*, else 0.

        After it a pattern list (or ``esac``) follows."""
        top = self._top(depth)
        if text[pos] != ";" or top[1] != "body" or text[pos + 1:pos + 2] not in (";", "&"):
            return 0
        top[1] = "pattern"
        return 3 if text.startswith(";;&", pos) else 2

    def pattern_paren(self, char: str, depth: int) -> bool:
        """Consume a pattern list's optional leading ``(`` or its closing ``)``."""
        top = self._top(depth)
        if char == "(" and top[1] == "pattern":
            top[1] = "pattern-word"
        elif char == ")" and top[1] in ("pattern", "pattern-word"):
            top[1] = "body"
        else:
            return False
        return True


def _heredoc_delimiter(text: str, pos: int) -> tuple[str, bool] | None:
    """The delimiter of a ``<<`` / ``<<-`` operator at *pos* (quotes removed), else None."""
    if not text.startswith("<<", pos) or text.startswith("<<<", pos):
        return None
    pos += 2
    strip_tabs = text.startswith("-", pos)
    pos += strip_tabs
    while pos < len(text) and text[pos] in " \t":
        pos += 1
    word: list[str] = []
    while pos < len(text) and text[pos] not in _WORD_BREAKS:
        char = text[pos]
        if char in "'\"":
            end = _quoted_end(text, pos + 1, char, escapes=char == '"')
            word.append(text[pos + 1:end - 1])
            pos = end
            continue
        if char == "\\":
            word.append(text[pos + 1:pos + 2])
            pos += 2
            continue
        word.append(char)
        pos += 1
    return ("".join(word), strip_tabs) if word else None


def _heredoc_bodies_end(text: str, pos: int, delimiters: list[tuple[str, bool]]) -> int:
    """End of the here-document bodies starting at *pos* (just after their command's newline).

    Inside ``$(...)`` bash also ends a body at a line that starts with the delimiter and has a
    ``)`` after it (``E)``, ``E )``); the rest of that line is shell again, so stop before it."""
    for delimiter, strip_tabs in delimiters:
        while pos < len(text):
            end = text.find("\n", pos)
            end = len(text) if end < 0 else end
            line = text[pos:end].lstrip("\t") if strip_tabs else text[pos:end]
            start, pos = end - len(line), end + 1
            if line == delimiter:
                break
            if line.startswith(delimiter) and ")" in line[len(delimiter):]:
                return start + len(delimiter)
        else:
            return -1  # unterminated: not skipped, so a misread operator hides nothing
    return min(pos, len(text))


class _HereDocs:
    """Here-document operators of one ``$(...)`` frame. A body is data to bash: no paren, quote,
    comment or ``case`` word in it moves the frame. Arithmetic (``$((1<<2))``) opens none."""

    def __init__(self) -> None:
        self._pending: list[tuple[str, bool]] = []
        self._arith = -1
        self._bracket_end = -1
        self.unbounded = False
        self.skipped = False

    def bodies_end(self, text: str, pos: int, depth: int, *, word_start: bool) -> int:
        """Advance on the character at *pos*; when it is the newline before complete bodies,
        return the position after them, else *pos*."""
        char = text[pos]
        if char == "[" and _arithmetic_bracket(text, pos):
            self._bracket_end = text.find("]", pos + 1)
        if pos <= self._bracket_end:
            return pos
        if char == "(" and self._arith < 0 and text.startswith("((", pos):
            self._arith = depth
        elif char == ")" and depth - 1 == self._arith:
            self._arith = -1
        elif char == "<" and self._arith < 0 and text[pos - 1] != "<":
            delimiter = _heredoc_delimiter(text, pos)
            if delimiter is not None:
                self._pending.append(delimiter)
        elif char == "\n" and self._pending:
            end = _heredoc_bodies_end(text, pos + 1, self._pending)
            self._pending = []
            self.unbounded |= end < 0
            self.skipped |= end > pos
            return max(end, pos)
        return pos


def _arithmetic_bracket(text: str, pos: int) -> bool:
    """Legacy arithmetic expansion or an assignment subscript, not literal ``[``."""
    if text[pos - 1:pos] == "$":
        return True
    end = text.find("]", pos + 1)
    return end >= 0 and text.startswith(("=", "+="), end + 1) and bool(
        re.search(r"(?:^|[\s;|(])[A-Za-z_][A-Za-z_0-9]*$", text[:pos]))


def _newline_breaks(breaks: list[int] | None, text: str, start: int, end: int,
                    offset: int) -> None:
    if breaks is not None:
        breaks.extend(offset + i - start for i in range(start, end) if text[i] == "\n")


def _command_span(text: str, start: int, *, shlex_quotes: bool,
                  breaks: list[int] | None = None) -> tuple[int, str]:
    """Find a substitution's closing parenthesis in its own shell comment frame.

    With *breaks*, record (relative to the returned span) each newline that separates commands
    in this frame or a nested parameter/command frame, so a logical-line split still sees them."""
    initial_breaks = len(breaks) if breaks is not None else 0
    end, span, retry = _walk_command_span(text, start, shlex_quotes, breaks, fallback=False)
    if retry:
        # A misread shift can skip the real closer. Discard speculative breaks and
        # retry without body skipping or case patterns rather than hide outer commands.
        if breaks is not None:
            del breaks[initial_breaks:]
        end, span, _ = _walk_command_span(text, start, shlex_quotes, breaks, fallback=True)
    return end, span


def _walk_command_span(text: str, start: int, shlex_quotes: bool,
                       breaks: list[int] | None, *, fallback: bool) -> tuple[int, str, bool]:
    """Walk one frame; request a conservative retry if skipped data hid its closer."""
    pos = copied = start
    depth = 1
    word_start = True
    parts = ["$("]
    cases = _CaseClauses()
    docs = _HereDocs()
    at_command = True
    while pos < len(text):
        char = text[pos]
        end = pos if fallback else docs.bodies_end(text, pos, depth, word_start=word_start)
        if docs.unbounded:
            # Unknown delimiter: body words cannot safely open case clauses.
            cases = _CaseClauses()
        if end > pos:
            _newline_breaks(breaks, text, pos, end, _out_len(parts) + pos - copied)
            pos = end
            at_command = word_start = True
            continue
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
        if word_start and char not in _WORD_BREAKS and not (fallback or docs.unbounded):
            at_command = cases.word(_plain_word(text, pos), depth, at_command=at_command)
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
        skip = cases.clause_end(text, pos, depth)
        if skip:
            pos += skip
            at_command = word_start = True
            continue
        if char == "\n" and breaks is not None:
            breaks.append(_out_len(parts) + pos - copied)
        elif char in "()" and cases.pattern_paren(char, depth):
            at_command = True  # after a pattern's `)` its command list follows
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if not depth:
                pos += 1
                break
        if char in _COMMAND_BREAKS:
            at_command = True
        word_start = char in _WORD_BREAKS
        pos += 1
    parts.append(text[copied:pos])
    return pos, "".join(parts), bool(depth and docs.skipped)


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
