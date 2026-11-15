"""Carry redirection syntax through quote removal without changing argv text."""
from __future__ import annotations

from collections.abc import Callable
import re

from cron.lifecycle_guard_groups import split_glued_redirections

# Optional fd or array-element fd variable, operator, and glued target.
REDIRECTION = re.compile(
    r"^(\d*|\{[A-Za-z_][A-Za-z0-9_]*(?:\[.+?\])?\})(<<<|<<-|<<|<>|<&|>&|>>|>\||&>>|&>|<|>)(.*)$", re.DOTALL)


class ShellWord(str):
    """A decoded word plus the redirection parsed before quoted punctuation is decoded."""

    redirection: tuple[str, str, str] | None
    quoted: bool

    def __new__(cls, text: str, redirection: tuple[str, str, str] | None, *, quoted: bool = False):
        word = super().__new__(cls, text)
        word.redirection = redirection
        word.quoted = quoted
        return word


def redirection_parts(word: str, pattern: re.Pattern[str]) -> tuple[str, str, str] | None:
    if isinstance(word, ShellWord):
        return word.redirection
    match = pattern.match(word)  # Legacy callers supply already lexical redirection words.
    return (match[1], match[2], match[3]) if match else None


def tokenize_redirections(line: str, tokenize: Callable[[str], list[str]],
                          pattern: re.Pattern[str]) -> list[str]:
    # Encode the escape character itself too, even outside quotes. A one-pass
    # decode cannot confuse literal fragments joined by quote removal with syntax.
    marker = "\ue000"
    alphabet = "<>&|" + marker
    replacements = {ch: f"{marker}{index}{marker}" for index, ch in enumerate(alphabet)}
    protected: list[str] = []
    single = double = escaped = False
    quote_marker = marker + "5" + marker
    for ch in line:
        literal = single or double or escaped
        # Mark the boundary, including empty quoted pieces. The escape marker
        # itself is encoded below, so literal input cannot synthesize this tag.
        if not escaped and (ch == "\\" and not single
                            or ch == "'" and not double or ch == '"' and not single):
            protected.append(quote_marker)
        if escaped:
            escaped = False
        elif ch == "\\" and not single:
            escaped = True
        elif ch == "'" and not double:
            single = not single
        elif ch == '"' and not single:
            double = not double
        protected.append(replacements.get(ch, ch) if literal or ch == marker else ch)

    def restore(text: str) -> str:
        return re.sub(marker + r"([0-4])" + marker, lambda m: alphabet[int(m[1])], text)

    words: list[str] = []
    for token in tokenize(split_glued_redirections("".join(protected))):
        tags = list(re.finditer(marker + r"([0-5])" + marker, token))
        quoted = any(match[1] == "5" for match in tags)
        token = re.sub(marker + r"([0-5])" + marker,
                       lambda m: "" if m[1] == "5" else m[0], token)
        match = pattern.match(token)
        parts = (restore(match[1]), restore(match[2]), restore(match[3])) if match else None
        words.append(ShellWord(restore(token), parts, quoted=quoted))
    return words
