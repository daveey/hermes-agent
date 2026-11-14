"""Hide completed expansion words from command-position tokenization.

Expansion punctuation belongs to its own shell frame, not the enclosing command.
This is lexical masking only: the arithmetic scanner still inspects the original
source. Unsupported or incomplete expansion syntax raises ValueError so its caller
can conservatively classify the target as arithmetic.
"""

from __future__ import annotations

import re

_CASE_WORD = re.compile(r"(?<![\w])case(?![\w])")


def _comment_end(text: str, pos: int, quoted: str) -> int | None:
    if quoted or text[pos] != "#" or (pos and text[pos - 1] not in " \t\n;&|("):
        return None
    newline = text.find("\n", pos)
    return len(text) if newline < 0 else newline


def _expansion_end(text: str, start: int, closer: str) -> int:
    """Return just past an expansion, respecting nested frames and quote scopes."""
    pos, quote = start, ""
    while pos < len(text):
        char = text[pos]
        if char == "\\" and quote != "'":
            pos += 2
            continue
        if quote == "'":
            if char == quote:
                quote = ""
            pos += 1
            continue
        if char == closer and not quote:
            return pos + 1
        if not quote and _CASE_WORD.match(text, pos):
            # A case-pattern ')' is not a frame closer; fail closed on grammar
            # this lexical masker cannot classify rather than lose the builtin.
            raise ValueError("case grammar in expansion")
        comment_end = _comment_end(text, pos, quote)
        if comment_end is not None:
            pos = comment_end
            continue
        nested = _opener(text, pos, quote)
        if nested is not None:
            width, end = nested
            pos = _expansion_end(text, pos + width, end)
            continue
        if char in "\"'" and (not quote or char == quote):
            quote = "" if quote else char
        elif not quote and char == "(" and closer == ")":
            pos = _expansion_end(text, pos + 1, ")")
            continue
        pos += 1
    raise ValueError("unterminated expansion")


def _opener(text: str, pos: int, quote: str) -> tuple[int, str] | None:
    pair = text[pos:pos + 2]
    if pair in ("$(", "${", "$[") or (not quote and pair in ("<(", ">(")):
        return 2, {"${": "}", "$[": "]"}.get(pair, ")")
    return (1, "`") if text[pos] == "`" else None


def mask_prefix_expansions(text: str) -> str:
    """Replace expansion bodies with one opaque word fragment, retaining quotes."""
    pieces: list[str] = []
    pos, quote = 0, ""
    while pos < len(text):
        char = text[pos]
        if char == "\\" and quote != "'":
            pieces.append(text[pos:pos + 2])
            pos += 2
            continue
        comment_end = _comment_end(text, pos, quote)
        if comment_end is not None:
            pos = comment_end
            continue
        nested = _opener(text, pos, quote) if quote != "'" else None
        if nested is not None:
            width, closer = nested
            pos = _expansion_end(text, pos + width, closer)
            pieces.append("__expansion__")
            continue
        if char in "\"'" and (not quote or char == quote):
            quote = "" if quote else char
        pieces.append(char)
        pos += 1
    return "".join(pieces)
