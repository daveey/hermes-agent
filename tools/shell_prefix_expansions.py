"""Hide completed expansion words from command-position tokenization.

Expansion punctuation belongs to its own shell frame, not the enclosing command.
This is lexical masking only: the arithmetic scanner still inspects the original
source. Unsupported or incomplete expansion syntax raises ValueError so its caller
can conservatively classify the target as arithmetic.
"""

from __future__ import annotations

from tools.shell_case_words import CaseWords, opaque_end


def _comment_end(text: str, pos: int, quoted: str) -> int | None:
    if quoted or text[pos] != "#" or (pos and text[pos - 1] not in " \t\n;&|("):
        return None
    newline = text.find("\n", pos)
    return len(text) if newline < 0 else newline


def _expansion_end(text: str, start: int, closer: str, cache: dict | None = None) -> int:
    """Return just past an expansion, respecting nested frames and quote scopes."""
    key = (start, closer)
    if cache is not None and key in cache:
        return cache[key]
    pos, quote = start, ""
    words = CaseWords()
    pending = []
    while pos < len(text):
        char = text[pos]
        opaque = opaque_end(text, pos, words, pending) if not quote else None
        if opaque is not None:
            pos = opaque
            continue
        if char == "\\" and quote != "'":
            if text[pos + 1:pos + 2] != "\n":
                words.fragment("__quoted__")
            pos += 2
            continue
        if quote == "'":
            if char == quote:
                quote = ""
            pos += 1
            continue
        if not quote and char.isspace():
            words.flush()
        if not quote and char in ";&|()\n":
            operator = next((op for op in (";;&", ";;", ";&") if text.startswith(op, pos)), char)
            if words.punctuation(operator):
                if cache is not None:
                    cache[(pos, "case-pattern")] = pos + 1
                pos += 1
                continue
            if len(operator) > 1:
                pos += len(operator)
                continue
        if char == closer and not quote:
            if cache is not None:
                cache[key] = pos + 1
            return pos + 1
        comment_end = _comment_end(text, pos, quote)
        if comment_end is not None:
            pos = comment_end
            continue
        nested = _opener(text, pos, quote)
        if nested is not None:
            width, end = nested
            words.fragment("__expansion__")
            pos = _expansion_end(text, pos + width, end, cache)
            continue
        if char in "\"'" and (not quote or char == quote):
            words.fragment("__quoted__")
            quote = "" if quote else char
        elif not quote and char == "(" and closer == ")":
            pos = _expansion_end(text, pos + 1, ")", cache)
            continue
        elif not quote and not char.isspace() and char not in ";&|()":
            words.fragment(char)
        pos += 1
    raise ValueError("unterminated expansion")


def _opener(text: str, pos: int, quote: str) -> tuple[int, str] | None:
    pair = text[pos:pos + 2]
    if pair in ("$(", "${", "$[") or (not quote and pair in ("<(", ">(")):
        return 2, {"${": "}", "$[": "]"}.get(pair, ")")
    return (1, "`") if text[pos] == "`" else None


_MASK = "__expansion__"


def mask_prefix_expansions(text: str, sources: list[int] | None = None, *,
                           comment_at_start: bool = True) -> str:
    """Replace expansion bodies with one opaque word fragment, retaining quotes.

    When *sources* is given it receives, for each masked character, the index in
    *text* it came from (a mask maps to its expansion's opener). *comment_at_start*
    says whether a ``#`` at index 0 starts a comment, i.e. whether *text* begins a
    command rather than resuming one mid-command at a word start.
    """
    pieces: list[str] = []
    pos, quote = 0, ""
    while pos < len(text):
        char = text[pos]
        if char == "\\" and quote != "'":
            piece = text[pos:pos + 2]
            pieces.append(piece)
            if sources is not None:
                sources.extend(range(pos, pos + len(piece)))
            pos += 2
            continue
        comment_end = None if pos == 0 and not comment_at_start else _comment_end(text, pos, quote)
        if comment_end is not None:
            pos = comment_end
            continue
        nested = _opener(text, pos, quote) if quote != "'" else None
        if nested is not None:
            width, closer = nested
            if sources is not None:
                sources.extend([pos] * len(_MASK))
            pos = _expansion_end(text, pos + width, closer)
            pieces.append(_MASK)
            continue
        if char in "\"'" and (not quote or char == quote):
            quote = "" if quote else char
        pieces.append(char)
        if sources is not None:
            sources.append(pos)
        pos += 1
    return "".join(pieces)


def same_quoted_word(text: str, start: int, end: int) -> bool:
    """Whether *text[start:end]*, opened and closed inside double quotes, stays one word.

    Expansions are skipped whole, so the walk is linear in the span. Anything this
    lexical walk cannot vouch for (ANSI quoting, unterminated expansions) is False.
    """
    pos, quote = start, '"'
    while pos < end:
        char = text[pos]
        if char == "\\" and quote != "'":
            pos += 2
            continue
        if quote == "'":
            quote = "" if char == "'" else quote
            pos += 1
            continue
        if not quote and char in " \t\n\r;&|()<>":
            return False  # an unquoted break: a later word of the command
        if char == "$" and text[pos + 1:pos + 2] in ("'", '"'):
            return False
        nested = _opener(text, pos, quote)
        if nested is not None:
            width, closer = nested
            try:
                pos = _expansion_end(text, pos + width, closer)
            except ValueError:
                return False
            continue
        if char == '"':
            quote = "" if quote else '"'
        elif char == "'" and not quote:
            quote = "'"
        pos += 1
    return pos == end and quote == '"'
