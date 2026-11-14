"""Find a command word that spells ``let`` only after quote removal.

``shell_arith_sink`` recognizes a bare ``let`` token, but bash removes quotes before it
looks the command word up, so ``\\let``, ``'let'``, ``l\\et``, ``"le"t``, ``$'\\x6cet'`` and
``$"let"`` all run the ``let`` builtin, whose operands are arithmetic: ``\\let 'a[$(f)]=1'``
runs ``f`` (t_01a1db24f88f8d5b). Expansions (``$x``, ``$(…)``) are data flow and out of
scope here; a substitution as the command word is already a shell sink of its own.
"""

from __future__ import annotations

import re

_WORD_BOUNDARY = frozenset(" \t\n;&|()<>`")
_ANSI_SIMPLE = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n",
                "r": "\r", "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}
_ANSI_NUMERIC_RE = re.compile(
    r"x([0-9A-Fa-f]{1,2})|u([0-9A-Fa-f]{1,4})|U([0-9A-Fa-f]{1,8})|([0-7]{1,3})")
# A quoted piece of a three-letter word is short; longer spans are never `let`.
_SPAN_LIMIT = 40


def hidden_let_end(command: str, token: str, start: int) -> int | None:
    """Where a quoted or escaped word spelling ``let`` ends, when *token* (found at
    *start*: an apostrophe, a double quote, ``$'`` or a backslash escape) belongs to such a
    word; None otherwise.

    Like the bare ``let`` token this ignores command position (``echo \\let …`` counts
    too); that only fails closed. Each token backs up only over the letters right before it,
    and a word that stops being a prefix of ``let`` ends the forward walk, so the scan stays
    linear.
    """
    if token not in ("'", '"', "$'") and token[:1] != "\\":
        return None
    word = start
    while word > 0 and command[word - 1] in "let":
        word -= 1
    if word == start and token == '"' and command[start - 1:start] == "$":
        word -= 1  # `$"let"`: an untranslated locale string is the plain string
    if word and command[word - 1] not in _WORD_BOUNDARY:
        return None
    text, pos = "", word
    while pos < len(command) and command[pos] not in " \t" and "let".startswith(text):
        piece, pos = _unquoted_piece(command, pos)
        if piece is None:
            return None
        text += piece
    if text == "let" and command[pos:pos + 1] in (" ", "\t"):
        return pos
    return None


def _unquoted_piece(command: str, pos: int) -> tuple[str | None, int]:
    """Decode the word piece at *pos* (a letter, an escape or one quoted span). None stops
    the word: an expansion, a metacharacter, an unclosed or overlong quote."""
    char = command[pos]
    if char.isalnum() or char == "_":
        return char, pos + 1
    if char == "\\":
        # A backslash-newline is a line continuation, not part of the word.
        following = command[pos + 1:pos + 2]
        return ("" if following == "\n" else following), pos + 2
    if char == "'":
        close = command.find("'", pos + 1, pos + _SPAN_LIMIT)
        return (None, pos) if close < 0 else (command[pos + 1:close], close + 1)
    if command.startswith("$'", pos):
        return _ansi_c_piece(command, pos + 2)
    if command.startswith('$"', pos):
        return "", pos + 1
    if char == '"':
        return _double_quoted_piece(command, pos + 1)
    return None, pos


def _double_quoted_piece(command: str, pos: int) -> tuple[str | None, int]:
    """Inside ``"…"`` a backslash quotes only ``" \\ $ ` newline``. An expansion stays in
    the text as ``$``/backtick, which no ``let`` holds."""
    text, index, limit = "", pos, min(len(command), pos + _SPAN_LIMIT)
    while index < limit:
        char = command[index]
        if char == '"':
            return text, index + 1
        following = command[index + 1:index + 2]
        if char == "\\" and following in ('"', "\\", "$", "`", "\n"):
            text += "" if following == "\n" else following
            index += 2
            continue
        text += char
        index += 1
    return None, index


def _ansi_c_piece(command: str, pos: int) -> tuple[str | None, int]:
    """Decode a ``$'…'`` body starting at *pos* the way bash does for escapes that can
    spell a letter. Others (``\\cX``, unknown escapes) keep their backslash here, which no
    ``let`` holds either."""
    out, index, limit = [], pos, min(len(command), pos + _SPAN_LIMIT)
    while index < limit:
        char = command[index]
        if char == "'":
            return "".join(out), index + 1
        if char != "\\" or index + 1 >= limit:
            out.append(char)
            index += 1
            continue
        numeric = _ANSI_NUMERIC_RE.match(command, index + 1, limit)
        if numeric:
            hexa = numeric.group(1) or numeric.group(2) or numeric.group(3)
            value = int(hexa, 16) if hexa else int(numeric.group(4), 8) & 0xFF
            out.append(chr(value) if value <= 0x10FFFF else "\ufffd")
            index = numeric.end()
            continue
        escaped = command[index + 1]
        out.append(_ANSI_SIMPLE.get(escaped, "\\" + escaped))
        index += 2
    return None, index
