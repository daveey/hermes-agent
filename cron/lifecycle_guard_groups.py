"""Which files reach a command's stdin through shell grouping, for the lifecycle guard.

A pipe into a compound command (``cat f | (…)``, ``| { …; }``, ``| if …; fi``, ``| while …;
done``, ``| case … esac``) feeds every command in it, and a group writes on what each of its
commands writes (``{ cat f.py; } | python3``) unless that output is piped elsewhere inside it.
A process substitution (``python3 <(cat f.py)``, ``bash < <(cat f.sh)``) puts what it writes
where the ``<(…)`` stands. Shell words come from the caller's tokenizer; this module only walks
them, so it never imports ``cron.lifecycle_guard``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional

CONTROL_CHARS = frozenset(";&|()")
_CONTROL_OPERATOR = re.compile(r"\|\||&&|\|&|;;|[;&|()]")
# Compound commands a pipe feeds as a whole: the word that opens one -> the word that closes it.
_LEADING_OPENERS = {"{": "}", "if": "fi", "while": "done", "until": "done"}
# Openers the reserved-word peel stops at (they are commands' first words, not prefixes).
_COMMAND_OPENERS = {"for": "done", "select": "done", "case": "esac"}
_CLOSERS = frozenset({"}", "fi", "done", "esac"})
# Stands where a `<(…)` that writes no known file was: NUL keeps it from ever naming a path.
PROCESS_SUBSTITUTION = "\x00<(…)"

Context = tuple[list[str], bool, list[str]]


def split_glued_redirections(line: str) -> str:
    """*line* with a space before each unquoted, unescaped ``<``/``>`` glued to a word that is
    not a bare descriptor number (``python3 -<f.py`` is ``python3 - <f.py``; ``2>x`` stays). A
    quoted ``<`` is text (``bash -c 'python3<f.py'`` keeps one word), and the operator's own
    second character (``<<``, ``>>``, ``<>``) is never split off."""
    out: list[str] = []
    word = ""
    in_single = in_double = escape = False
    for ch in line:
        if escape:
            escape = False
        elif ch == "\\" and not in_single:
            escape = True
        elif ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if ch.isspace() or ch in CONTROL_CHARS:
                out.append(ch)
                word = ""
                continue
            if ch in "<>" and word and not word.isdigit() and word[-1] not in "<>":
                out.append(" ")
                word = ""
        out.append(ch)
        word += ch
    return "".join(out)


@dataclass
class _Group:
    closer: str
    feed: list[str]
    output: list[str] = field(default_factory=list)
    # A process substitution suspends the command it stands in: (words, call_argument, subs).
    outer: Optional[tuple[list[str], bool, list[tuple[int, list[str]]]]] = None


def _opened_groups(segment: list[str], peel_reserved_words: Callable[[list[str]], int]) -> list[str]:
    """Closers of the compound commands *segment* opens (``while { if`` opens three)."""
    index = peel_reserved_words(segment)
    closers = [_LEADING_OPENERS[word] for word in segment[:index] if word in _LEADING_OPENERS]
    if index < len(segment) and segment[index] in _COMMAND_OPENERS:
        closers.append(_COMMAND_OPENERS[segment[index]])
    return closers


def _substituted_variants(segment: list[str], subs: list[tuple[int, list[str]]]) -> list[list[str]]:
    """One reading of *segment* per file a process substitution writes, each in its place (the
    others left as ``PROCESS_SUBSTITUTION``); *segment* itself when none writes a file."""
    variants = [segment[:position] + [name] + segment[position + 1:]
                for position, files in subs for name in files]
    return variants or [segment]


class _LineWalk:
    """State of one line's walk; ``contexts`` yields ``(segment, call_argument, piped_files)``."""

    def __init__(self, cat_operands, opens_call_arguments, peel_reserved_words) -> None:
        self.cat_operands = cat_operands
        self.opens_call_arguments = opens_call_arguments
        self.peel_reserved_words = peel_reserved_words
        self.segment: list[str] = []
        self.subs: list[tuple[int, list[str]]] = []
        self.call_argument = False
        self.feed: list[str] = []
        # What the last finished command (or closed group) wrote, until an operator routes it:
        # into the next command's stdin on `|`, else into the enclosing group's output.
        self.pending: list[str] = []
        self.closed: Optional[list[str]] = None
        self.groups: list[_Group] = []

    def flush(self) -> Iterator[Context]:
        written: list[str] = []
        for variant in _substituted_variants(self.segment, self.subs):
            yield variant, self.call_argument, self.feed
            written += self.cat_operands(variant)
        self.pending = self.closed if self.closed is not None else written
        self.closed = None
        self.groups.extend(_Group(closer, self.feed)
                           for closer in _opened_groups(self.segment, self.peel_reserved_words))
        self.segment, self.subs = [], []

    def route(self) -> None:
        if self.groups:
            self.groups[-1].output.extend(self.pending)
        self.pending = []

    def enclosing_feed(self) -> list[str]:
        return self.groups[-1].feed if self.groups else []

    def resume(self, group: _Group) -> None:
        """Back in the command a closed ``<(…)`` stands in."""
        if group.outer is None:
            return
        self.segment, self.call_argument, self.subs = group.outer
        self.subs.append((len(self.segment), group.output))
        self.segment.append(PROCESS_SUBSTITUTION)
        self.feed = group.feed

    def word(self, token: str) -> None:
        if not self.segment and token in _CLOSERS and self.groups \
                and self.groups[-1].closer == token:
            self.route()
            self.closed = self.groups.pop().output
            self.feed = self.enclosing_feed()
        self.segment.append(token)

    def operator(self, operator: str) -> Iterator[Context]:
        if operator == "(" and self.segment and self.segment[-1] == "<":
            self.segment.pop()
            self.groups.append(_Group(")", self.feed, outer=(
                self.segment, self.call_argument, self.subs)))
            self.segment, self.subs, self.call_argument = [], [], False
            return
        next_is_call = operator == "(" and bool(self.segment) \
            and self.opens_call_arguments(self.segment)
        if self.segment:
            yield from self.flush()
        if operator in ("|", "|&"):
            self.feed, self.pending = self.pending, []
        elif operator == "(":
            self.groups.append(_Group(")", self.feed))
        else:
            self.route()
            if operator == ")" and self.groups and self.groups[-1].closer == ")":
                group = self.groups.pop()
                if group.outer is not None:
                    self.resume(group)
                    return
                self.pending = group.output
            self.feed = self.enclosing_feed()
        self.call_argument = next_is_call

    def finish(self) -> Iterator[Context]:
        while True:
            if self.segment:
                yield from self.flush()
            # An unclosed `<(` still runs the command it stands in.
            index = next((i for i in range(len(self.groups) - 1, -1, -1)
                          if self.groups[i].outer is not None), None)
            if index is None:
                return
            group = self.groups[index]
            del self.groups[index:]
            self.resume(group)


def iter_line_segment_contexts(
    tokens: list[str], *,
    cat_operands: Callable[[Optional[list[str]]], list[str]],
    opens_call_arguments: Callable[[list[str]], bool],
    peel_reserved_words: Callable[[list[str]], int],
) -> Iterator[Context]:
    """``(segment, call_argument, piped_files)`` for one line's shell *tokens* (see
    ``cron.lifecycle_guard._iter_segment_contexts``). *cat_operands* names the files a segment
    writes to its stdout."""
    walk = _LineWalk(cat_operands, opens_call_arguments, peel_reserved_words)
    for token in tokens:
        if token and set(token) <= CONTROL_CHARS:
            for operator in _CONTROL_OPERATOR.findall(token):
                yield from walk.operator(operator)
        else:
            walk.word(token)
    yield from walk.finish()
