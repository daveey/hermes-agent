"""Expansion parts of a heredoc delimiter word, read the way bash reads them.

Bash takes a delimiter word through its expansions without performing them. Backticks,
``$((…))`` and ``${…}`` stay as written: ``` <<`echo  E` ``` ends at a line reading
``` `echo  E` ``` and ``<<${x:-a  b}`` at ``${x:-a  b}``, spaces and all. ``$'…'`` and ``$"…"``
are quoting: ``<<$'\\x45'`` ends at ``E``. Stopping such a word at its first space or ``(`` left
the body unterminated, so its lines (a ``}`` among them) read as commands of the script around it.

A ``$(…)`` command substitution is different: bash parses it, and the delimiter is bash's own
reprint of that parse. ``$(echo  E|cat)`` ends at ``$(echo E | cat)``, ``$([[ a ]])`` at
``$([[ -n a ]])``, and a ``case`` or a comment reprints across lines, so it never ends. That
reprint is not modelled here. A word holding ``$(`` outside single quotes is *reprinted*, and the
opener parser treats it as unparsable. The lifecycle guard then tries every later line holding
``$(`` as the terminator (``cron.lifecycle_guard_groups.heredoc_scripts``).
"""

from __future__ import annotations

import re

_ANSI_C_SIMPLE = {
    "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r",
    "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?",
}
_ANSI_C_ESCAPE_RE = re.compile(
    r"\\(?:(?P<simple>[abeEfnrtv\\'\"?])|(?P<octal>[0-7]{1,3})|x(?P<hex>[0-9A-Fa-f]{1,2})"
    r"|u(?P<u4>[0-9A-Fa-f]{1,4})|U(?P<u8>[0-9A-Fa-f]{1,8})|c(?P<control>.))", re.DOTALL)
# A command substitution: ``$(`` that does not open arithmetic ``$((``.
_COMMAND_SUBSTITUTION_RE = re.compile(r"\$\((?!\()")


def reprinted_by_bash(text: str) -> bool:
    """Whether bash ends a heredoc whose delimiter holds expansion *text* at its own reprint of a
    parse instead of at *text*: any ``$(…)`` in it, or a ``$((`` that is not plain arithmetic."""
    return bool(_COMMAND_SUBSTITUTION_RE.search(text)) or (
        text.startswith("$((") and not text.endswith("))"))


def _decode_ansi_c(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        if match["simple"]:
            return _ANSI_C_SIMPLE[match["simple"]]
        if match["control"]:
            return chr(ord(match["control"]) & 0x1F)
        code = (int(match["octal"], 8) if match["octal"] else
                int(match["hex"] or match["u4"] or match["u8"], 16))
        try:
            return chr(code & 0xFF if match["octal"] else code)
        except (ValueError, OverflowError):
            return match.group()
    # Bash truncates this ANSI-C fragment at NUL, not the concatenated shell word.
    # The caller still consumes its entire quoted span and reads subsequent fragments.
    return _ANSI_C_ESCAPE_RE.sub(replace, text).split("\0", 1)[0]


def _quoted_end(command: str, index: int, quote: str, escapes: bool | None = None) -> int | None:
    """Index just past the ``quote`` closing the span opened at ``index`` (one line only).
    Backslash escapes count except in a plain single-quoted span (``escapes`` overrides)."""
    escapes = quote != "'" if escapes is None else escapes
    cursor = index + 1
    while cursor < len(command):
        char = command[cursor]
        if char in "\r\n":
            return None
        if char == quote:
            return cursor + 1
        cursor += 2 if char == "\\" and escapes else 1
    return None


def _balanced_end(command: str, index: int, opener: str, closer: str) -> int | None:
    """Index just past the ``closer`` matching ``command[index]`` (one line only). A nested
    ``$(…)`` or ``${…}`` is skipped whole, so ``${x:-$(echo })}`` closes at its last ``}``."""
    depth = 0
    cursor = index
    while cursor < len(command):
        char = command[cursor]
        if char in "\r\n":
            return None
        if char == "\\":
            cursor += 2
            continue
        if char in "'\"`":
            end = _quoted_end(command, cursor, char)
            if end is None:
                return None
            cursor = end
            continue
        nested = {"(": ")", "{": "}"}.get(command[cursor + 1:cursor + 2])
        if char == "$" and nested is not None:
            end = _balanced_end(command, cursor + 1, command[cursor + 1], nested)
            if end is None:
                return None
            cursor = end
            continue
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return cursor + 1
        cursor += 1
    return None


def delimiter_expansion(command: str, index: int) -> tuple[int, str, bool] | None:
    """``(end, text, quoted)`` for the ``$(…)``, ``$((…))``, ``${…}``, ``$'…'`` or backtick part
    of a delimiter word starting at ``index``; None when it does not close on its line."""
    if command.startswith("`", index):
        end = _quoted_end(command, index, "`")
        return None if end is None else (end, command[index:end], False)
    if command.startswith("$'", index):
        end = _quoted_end(command, index + 1, "'", escapes=True)  # `\'` stays inside $'…'
        if end is None:
            return None
        return end, _decode_ansi_c(command[index + 2:end - 1]), True
    pair = {"(": ")", "{": "}"}.get(command[index + 1:index + 2])
    if not command.startswith("$", index) or pair is None:
        return None
    end = _balanced_end(command, index + 1, command[index + 1], pair)
    return None if end is None else (end, command[index:end], False)
