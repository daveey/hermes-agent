"""Quote removal for literal builtin name operands (not general data flow)."""
from __future__ import annotations

import re

from tools.shell_command_position import _REDIRECTION_RE, _builtin_index
from tools.shell_prefix_expansions import _expansion_end, _opener

_BREAKS = frozenset(" \t\r\n;&|()<>")
_ANSI_ESCAPE = re.compile(r"\\(?:x[0-9a-fA-F]{1,2}|u[0-9a-fA-F]{1,4}|U[0-9a-fA-F]{1,8}|[0-7]{1,3}|c.|.)", re.DOTALL)
_ESCAPES = dict(zip("abefnrtv\\'\"?", "\a\b\x1b\f\n\r\t\v\\'\"?"))


def _ansi_escape(match: re.Match) -> str:
    value = match.group()[1:]
    if value[0] in "xuU":
        number = int(value[1:], 16)
        return chr(number) if number <= 0x10ffff else "\ufffd"
    if value[0] in "01234567":
        return chr(int(value, 8) % 256)
    if value[0] == "c" and len(value) == 2:
        return chr(ord(value[1].upper()) ^ 64)
    return _ESCAPES.get(value, "\\" + value)


def _word(text: str, pos: int, conditional: bool, cache: dict) -> tuple[str, int]:
    """Remove lexical quotes, leaving real expansions opaque to name classification."""
    pieces = []
    while pos < len(text) and text[pos] not in _BREAKS:
        char = text[pos]
        if char == "\\":
            pair = text[pos:pos + 2]
            pieces.append("" if pair == "\\\n" else pair if conditional else pair[1:])
            pos += len(pair)
        elif char == "'" or text.startswith("$'", pos):
            ansi = char == "$"
            start = pos + (2 if ansi else 1)
            pos = start
            while pos < len(text) and text[pos] != "'":
                pos += 2 if ansi and text[pos] == "\\" else 1
            if pos >= len(text):
                raise ValueError("unterminated literal")
            span = text[start:pos]
            pieces.append(_ANSI_ESCAPE.sub(_ansi_escape, span).split("\0", 1)[0] if ansi else span)
            pos += 1
        elif char == '"' or text.startswith('$"', pos):
            span, pos = _double_word(text, pos + (2 if char == "$" else 1), cache)
            pieces.append(span)
        else:
            opening = _opener(text, pos, "")
            if opening is not None:
                width, closer = opening
                pos = _expansion_end(text, pos + width, closer, cache)
                pieces.append("__expansion__")
            else:
                pieces.append(char)
                pos += 1
    return "".join(pieces), pos


def _double_word(text: str, pos: int, cache: dict) -> tuple[str, int]:
    pieces = []
    while pos < len(text):
        char = text[pos]
        if char == '"':
            return "".join(pieces), pos + 1
        if char == "\\" and text[pos + 1:pos + 2] in ('$', '`', '"', '\\', '\n'):
            pieces.append("" if text[pos + 1] == "\n" else text[pos + 1])
            pos += 2
            continue
        opening = _opener(text, pos, '"')
        if opening is not None:
            width, closer = opening
            pos = _expansion_end(text, pos + width, closer, cache)
            pieces.append("__expansion__")
        else:
            pieces.append(char)
            pos += 1
    raise ValueError("unterminated double quote")


class _Operands:
    """Builtin option values must not be confused with variable destinations."""

    def __init__(self, name: str):
        self.name = name
        self.options = True
        self.pending = ""
        self.disabled = False
        self.previous = ""

    def take(self, word: str) -> str | None:
        name = self.name
        if name in ("test", "[", "[["):
            previous, self.previous = self.previous, word
            return word if previous == "-v" else None
        if self.pending:
            pending, self.pending = self.pending, ""
            return word if pending == "v" else None
        if self.options and word == "--":
            self.options = False
            return None
        if self.options and len(word) > 1 and word[0] in "-+":
            return self._option(word)
        self.options = False
        if name == "printf":
            self.disabled = True
        return word if not self.disabled and name in ("declare", "typeset", "local", "unset", "read") else None

    def _option(self, word: str) -> str | None:
        name = self.name
        if name == "printf":
            if word.startswith("-v"):
                if len(word) > 2:
                    return word[2:]
                self.pending = "v"
            return None
        if word.startswith("-") and (
            (name == "unset" and "f" in word[1:])
            or (name in ("declare", "typeset", "local") and any(c in word[1:] for c in "pfF"))
        ):
            self.disabled = True
        if name == "read":
            for index, flag in enumerate(word[1:], 1):
                if flag in "adnNptui":
                    if index + 1 == len(word):
                        self.pending = flag
                    break
        return None


def _subscript(operand: str) -> str | None:
    match = re.match(r"[A-Za-z_]\w*\[", operand)
    if match is None:
        return None
    depth = 1
    for index in range(match.end(), len(operand)):
        char = operand[index]
        depth += (char == "[") - (char == "]")
        if depth == 0:
            return operand[match.end():index]
    return None


class LiteralNameScanner:
    """Walk each simple command once, caching the verdict of its current word."""

    def __init__(self, base: int, cache: dict):
        self.base = base
        self.cache = cache
        self.pos = base
        self.words: list[str] = []
        self.operands: _Operands | None = None
        self.sink = False
        self.redirect = False

    def check(self, text: str, start: int, *, date_exempt: bool) -> bool:
        from tools.shell_arith_sink import substitution_in_arithmetic

        while self.pos <= start:
            char = text[self.pos]
            if text.startswith("\\\n", self.pos):
                self.pos += 2
                continue
            if char.isspace():
                self.pos += 1
                continue
            redirect = _REDIRECTION_RE.match(text, self.pos)
            if redirect is not None:
                self.redirect = True
                self.pos = redirect.end()
                continue
            if char in ";&|()":
                self.words.clear()
                self.operands = None
                self.redirect = False
                self.pos += 1
                continue
            if char == "#":
                self.pos = text.find("\n", self.pos)
                if self.pos < 0:
                    self.pos = len(text)
                continue
            conditional = self.operands is not None and self.operands.name == "[["
            word, self.pos = _word(text, self.pos, conditional, self.cache)
            self.sink = False
            if self.redirect:
                self.redirect = False
                continue
            if self.operands is None:
                self.words.append(word)
                index = _builtin_index(self.words[:64])
                if 0 <= index < len(self.words):
                    self.operands = _Operands(self.words[index])
                continue
            operand = self.operands.take(word)
            subscript = _subscript(operand) if operand is not None else None
            if subscript is not None:
                self.sink = substitution_in_arithmetic("$((" + subscript + "))", date_exempt=date_exempt)
        return self.sink
