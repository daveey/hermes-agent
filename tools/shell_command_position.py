"""Command-position helpers shared by lifecycle and arithmetic sink scans."""

from __future__ import annotations

import re
import shlex

from tools.shell_prefix_expansions import mask_prefix_expansions, same_quoted_word

_RESERVED_PREFIX_WORDS = frozenset({
    "if", "then", "else", "elif", "do", "while", "until", "!", "time", "coproc", "in", "{",
    "function",
})
_SHELL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
# Keep quoted separators inside their word; shlex alone loses that distinction.
_REDIRECTION = r"(?:[0-9]+|\{[A-Za-z_]\w*\})?(?:<<<|<<-?|>>|<>|>\||[<>]&|&>>?|[<>])"
_REDIRECTION_RE = re.compile(_REDIRECTION)
_PREFIX_TOKEN = re.compile(_REDIRECTION + r'''|[;\n&|()]|(?:[^\s;\n&|()<>'"\\]+|\\.|'[^']*'|"(?:\\.|[^"\\])*")+''')
_NAME_CONSUMERS = frozenset({"unset", "read", "declare", "local", "typeset", "readonly", "export"})


def peel_reserved_words(segment: list[str]) -> int:
    """Skip leading shell keywords and their operands, never ordinary arguments."""
    index = 0
    while index < len(segment) and segment[index] in _RESERVED_PREFIX_WORDS:
        word = segment[index]
        index += 1
        if index >= len(segment):
            break
        if word == "time":
            if segment[index] == "-p":
                index += 1
            if index < len(segment) and segment[index] == "--":
                index += 1
        elif word == "function" or (word == "coproc" and _SHELL_NAME.fullmatch(segment[index]) and (
            index + 1 == len(segment) or segment[index + 1] == "{"
        )):
            index += 1
    return index


def _builtin_index(words: list[str]) -> int:
    index = peel_reserved_words(words)
    while index < len(words) and _ASSIGNMENT.match(words[index]):
        index += 1
    while index < len(words) and words[index] in ("command", "builtin"):
        wrapper = words[index]
        index += 1
        while index < len(words) and words[index].startswith("-"):
            option = words[index]
            index += 1
            if option == "--":
                break
            if wrapper == "command" and any(flag in option for flag in "vV"):
                return -1  # command -v/-V prints a name; it does not execute it.
    return index


# Command position is resolved from at most this many leading words. A command still
# unresolved after them (all assignments, keywords or wrappers) fails closed, which
# also keeps every classification of a long command O(1) in its earlier words.
_RESOLVE_LIMIT = 64


def _words_verdict(words: list[str]) -> bool:
    """Whether the last of *words* (the open quoted word) is a builtin's target name."""
    head = words[:_RESOLVE_LIMIT]
    index = _builtin_index(head)
    if index < 0:
        return False  # Proven command introspection, not an unknown command.
    if index >= len(head):
        return True
    name, count = words[index], len(words) - index - 1
    if name in _NAME_CONSUMERS:
        return count > 0
    if name == "printf" and count == 1:
        return words[-1].startswith("-v")
    if name in ("printf", "test", "[", "[["):
        return count >= 2 and words[-2] == "-v"
    # Unknown commands may be functions or builtins with arithmetic operands.
    # Only positively identified data consumers may exempt a quoted subscript.
    return name not in ("echo", "cat", "grep")


class QuotedNameClassifier:
    """Classify the quoted words of one simple command, in source order, in linear time.

    Tokenization resumes at the start of the previously classified word (the only
    word whose text could still grow), so each source character is tokenized O(1)
    times however many quoted words the command has; further brackets in the same
    word reuse its verdict after a linear walk of the new span.
    """

    def __init__(self, base: int):
        self.base = base  # source offset where the simple command starts
        self.scanned = 0  # source characters tokenized so far (regression tests read it)
        self._resume = base  # source offset of the last classified word
        self._words: list[str] = []  # words before _resume (truncated in place on resume)
        self._kept = 0
        self._target = False  # whether the word at _resume is a redirection target
        self._checked: int | None = None  # source offset of the last classified bracket
        self._verdict = False

    def classify(self, command: str, start: int) -> bool:
        """Whether the open double-quoted word ending at *start* is a builtin's target."""
        if self._checked is not None and same_quoted_word(command, self._checked, start):
            self._checked = start
            return self._verdict
        try:
            self._verdict = self._tokenize(command, start)
        except (ValueError, RecursionError):
            self._checked = None
            return True  # An unrecognized word must not hide an arithmetic sink.
        return self._verdict

    def _tokenize(self, command: str, start: int) -> bool:
        segment = command[self._resume:start]
        self.scanned += len(segment)
        cleaned_from: list[int] = []
        cleaned: list[str] = []
        index = 0
        while index < len(segment):  # drop line continuations, remembering offsets
            if segment.startswith("\\\n", index):
                index += 2
                continue
            cleaned.append(segment[index])
            cleaned_from.append(index)
            index += 1
        masked_from: list[int] = []
        masked = mask_prefix_expansions("".join(cleaned), masked_from,
                                        comment_at_start=self._resume == self.base)
        words = self._words
        del words[self._kept:]
        redirect_target = self._target
        last = None
        for match in _PREFIX_TOKEN.finditer(masked + '"'):
            raw = match.group()
            last = (match, words, len(words), redirect_target)
            was_target, redirect_target = redirect_target, False
            if was_target:
                continue
            if _REDIRECTION_RE.fullmatch(raw):
                redirect_target = True
            elif raw in (";", "\n", "&", "|", "(", ")"):
                words = []
            else:
                words.append("".join(shlex.split(raw)))
        self._checked = None
        if last is None or last[0].end() != len(masked) + 1 or last[0].start() >= len(masked):
            raise ValueError("open quoted word not found")
        match, kept_words, kept, was_target = last
        self._resume += cleaned_from[masked_from[match.start()]]
        self._words, self._kept, self._target = kept_words, kept, was_target
        self._checked = start
        if was_target:
            return False  # The current word is a redirection destination.
        return _words_verdict(words)


def quoted_name_is_arithmetic(prefix: str) -> bool:
    """Whether the open double-quoted name at *prefix* is a builtin's target.

    The caller stops just before its subscript opener, inside double quotes. Close
    that quote for tokenization; literal separators remain data, not command starts.
    """
    return QuotedNameClassifier(0).classify(prefix, len(prefix))
