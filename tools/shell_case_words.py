"""Reserved-word state for command-substitution boundary scanning.

Only lexical, unquoted words are reserved words. Nested expansions are opaque
word fragments; their own boundary walk has its own state.
"""
from __future__ import annotations

import re

_ASSIGNMENT = re.compile(r"[A-Za-z_]\w*(?:\+)?=")


class CaseWords:
    def __init__(self):
        self.cases: list[str] = []
        self.command = True
        self.prefix = ""
        self.word: list[str] = []
        self.pattern_depth = 0

    def fragment(self, value: str) -> None:
        self.word.append(value)

    def flush(self) -> None:
        if not self.word:
            return
        word = "".join(self.word)
        self.word.clear()
        phase = self.cases[-1] if self.cases else ""
        if phase == "subject":
            self.cases[-1] = "in"
        elif phase == "in":
            if word != "in":
                raise ValueError("case missing in")
            self.cases[-1] = "pattern-start"
        elif phase in ("pattern-start", "pattern"):
            if word == "esac" and phase == "pattern-start":
                self.cases.pop()
                self.command = False
            else:
                self.cases[-1] = "pattern"
        elif self.command and word == "case":
            self.prefix = ""
            self.cases.append("subject")
        elif self.command and word == "esac" and self.cases:
            self.cases.pop()
            self.command = False
        elif self.command:
            if word in ("time", "coproc", "function"):
                self.prefix = word
            elif self.prefix == "time" and word in ("-p", "--"):
                return
            elif self.prefix in ("coproc", "function"):
                self.prefix = ""
            else:
                self.prefix = ""
                self.command = bool(_ASSIGNMENT.match(word)) or word in {
                    "if", "then", "elif", "else", "while", "until", "do", "!", "{",
                }

    def punctuation(self, token: str) -> bool:
        """Consume case syntax; return whether a parenthesis is a pattern token."""
        phase = self.cases[-1] if self.cases else ""
        extended = token == "(" and self.word and self.word[-1] in "@!?+*"
        if phase in ("pattern-start", "pattern") and (extended or self.pattern_depth):
            self.pattern_depth += (token == "(") - (token == ")")
            self.fragment(token)
            return token in ("(", ")")
        self.flush()
        phase = self.cases[-1] if self.cases else ""
        if phase in ("pattern-start", "pattern"):
            if token == ")":
                self.cases[-1] = "body"
                self.command = True
            elif token in ("(", "|"):
                self.cases[-1] = "pattern"
            return token in ("(", ")")
        if token in (";;", ";&", ";;&") and phase == "body":
            self.cases[-1] = "pattern-start"
            self.command = True
        elif token in (";", "&", "|", "\n", "("):
            self.command = True
        return False


def opaque_end(text: str, pos: int, words: CaseWords, pending: list) -> int | None:
    """Skip ANSI literals and heredoc bodies without interpreting reserved words."""
    from tools.shell_heredoc import _find_heredoc_close, _parse_heredoc_operator

    if text.startswith("$'", pos):
        words.fragment("__quoted__")
        pos += 2
        while pos < len(text) and text[pos] != "'":
            pos += 2 if text[pos] == "\\" else 1
        if pos >= len(text):
            raise ValueError("unterminated ANSI quote")
        return pos + 1
    if text.startswith("<<", pos) and not text.startswith("<<<", pos):
        spec = _parse_heredoc_operator(text, pos)
        if spec is not None:
            words.flush()
            pending.append(spec)
            return spec[0]
    if text[pos] == "\n" and pending:
        words.punctuation("\n")
        pos += 1
        for _, delimiter, strip_tabs, _ in pending:
            close = _find_heredoc_close(text, pos, delimiter, strip_tabs)
            if close is None:
                raise ValueError("unterminated heredoc")
            pos = close[1]
        pending.clear()
        return pos
    return None
