"""Carry redirection-only exec file descriptors into later script commands.

Keep lexical redirection metadata intact; quoted operator-shaped arguments are data.
The caller's existing ordered fd resolver handles each command's local redirections.

The table maps fd -> tuple of *possible* targets. A redirection-only ``exec`` that certainly
runs (top level, after ``;``/newline, not piped/backgrounded) replaces the fd; one that may not
run (inside ``if``/``while``/``case``/groups, after ``&&``/``||``, in a pipeline) *adds* its
result to the old targets, so a conditional close/rebind never erases a dangerous earlier open.
A line whose fds have several candidates is emitted once per candidate index (zipped, not a
product), so growth is linear in the candidate count. Never discard reachable targets.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
import re

from cron.lifecycle_guard_words import ShellWord, redirection_parts


_OPENERS = frozenset({"if", "while", "until", "{"})
_CLOSERS = frozenset({"fi", "done", "esac", "}"})
_LEADING_KEYWORDS = frozenset({"if", "then", "else", "elif", "while", "until", "do", "!", "{"})
_HEADERS = frozenset({"for", "case", "select"})  # `for x in …`/`case x in`: no command follows
_Table = dict[str, tuple[str, ...]]


def _redirections(words: list[str], pattern: re.Pattern[str]):
    index = 0
    while index < len(words):
        parts = redirection_parts(words[index], pattern)
        start, index = index, index + 1
        if parts is None:
            continue
        fd, operator, target = parts
        if not target and index < len(words):
            target = words[index]
            index += 1
        yield start, index, (fd, operator, target)


def _apply(table: _Table, fd: str, operator: str, target: str) -> None:
    fd = fd or ("0" if operator.startswith("<") else "1")
    source = target.removesuffix("-")
    value = table.get(source) if operator in ("<&", ">&") else None
    table.pop(fd, None)
    if operator in ("<", "<>") and target:
        table[fd] = (target,)
    elif value is not None:
        table[fd] = value
    if operator in ("<&", ">&") and target.endswith("-"):
        table.pop(source, None)
    if operator in ("&>", "&>>"):
        table.pop("2", None)


def _exec_redirections(words: list[str], pattern: re.Pattern[str]):
    """Redirections of a redirection-only ``exec`` (also ``command``/``builtin`` wrapped, or with
    leading redirections); ``None`` when *words* is not one."""
    found = list(_redirections(words, pattern))
    consumed = {i for start, end, _ in found for i in range(start, end)}
    rest = [word for i, word in enumerate(words) if i not in consumed]
    while rest and rest[0] in ("command", "builtin"):
        wrapper = rest.pop(0)
        while rest and (rest[0] == "--" or (wrapper == "command" and rest[0] == "-p")):
            rest.pop(0)
    return [parts for _, _, parts in found] if rest == ["exec"] else None


def _merge(old: _Table, new: _Table) -> None:
    """Make *old* the union of itself and *new* (the result of a maybe-run ``exec``)."""
    for fd in set(old) | set(new):
        values = tuple(dict.fromkeys(old.get(fd, ()) + new.get(fd, ())))

        if values:
            old[fd] = values
        else:
            old.pop(fd, None)


def _carry_segment(segment: list[str], table: dict[str, str], pattern: re.Pattern[str]
                   ) -> list[str]:
    """Rewrite *segment*'s fd copies using the concrete *table* (fd -> one target)."""
    out: list[str] = []
    local = {fd: (target,) for fd, target in table.items()}
    position = 0
    for start, end, (fd, operator, target) in _redirections(segment, pattern):
        source = target.removesuffix("-")
        value = local.get(source) if operator in ("<&", ">&") else None
        if value is not None:
            out.extend(segment[position:start])
            destination = fd or ("0" if operator.startswith("<") else "1")
            out.append(ShellWord(destination + "<", (destination, "<", "")))
            out.append(ShellWord(value[0], None))
            # A moved source is closed after copying, including same-fd moves.
            if target.endswith("-"):
                out.append(ShellWord(source + "<&-", (source, "<&", "-")))
            position = end
        _apply(local, fd, operator, target)
    out.extend(segment[position:])
    return out


def _command_start(segment: list[str], nesting: list[str], pattern: re.Pattern[str]) -> int:
    """Index past leading compound-command keywords, tracking *nesting* of open compounds."""
    start = 0
    while start < len(segment):
        word = segment[start]
        if word in _CLOSERS:
            if nesting:
                nesting.pop()
            start += 1
        elif word in _LEADING_KEYWORDS:
            if word in _OPENERS:
                nesting.append(word)
            start += 1
        elif word == "time":
            start += 1
            if segment[start:start + 1] == ["-p"]:
                start += 1
            if segment[start:start + 1] == ["--"]:
                start += 1
        elif word in ("function", "coproc"):
            # Definitions and asynchronous bodies cannot certainly mutate the parent.
            nesting.append(word)
            start += 1
            if word == "function" or segment[start + 1:start + 2] == ["{"]:
                start += 1
        elif word in _HEADERS:
            nesting.append(word)
            return len(segment)
        elif (nesting and nesting[-1] == "case" and word.endswith(")")
                and redirection_parts(word, pattern) is None):
            start += 1  # a case pattern `x)`
        else:
            break
    return start


def carry_exec_descriptors(
    lines: Iterable[list[str]], *, pattern: re.Pattern[str],
    peel_reserved_words: Callable[[list[str]], int], control_chars: frozenset[str],
) -> Iterator[list[str]]:
    """Resolve inherited fd copies in script order, without changing pipeline boundaries.

    Redirection-only execs in branches/groups are carried as alternatives (see module doc).
    Each script gets its own table, so heredoc bodies cannot rebind their enclosing script.
    """
    table: _Table = {}
    nesting: list[str] = []  # open compound commands; persists across lines
    for tokens in lines:
        pieces: list[tuple[list[str], str, str]] = []  # (segment, control before, control after)
        segment: list[str] = []
        before = ""
        for token in [*tokens, ";"]:
            if token and set(token) <= control_chars:
                pieces.append((segment, before, token))
                segment, before = [], token
            else:
                segment.append(token)
        variants: list[list[list[str]]] = []
        for segment, before, after in pieces:
            start = _command_start(segment, nesting, pattern)
            command = start + peel_reserved_words(segment[start:])
            redirects = _exec_redirections(segment[command:], pattern)
            if redirects is not None:
                new = dict(table)
                for parts in redirects:
                    _apply(new, *parts)
                if not nesting and before in ("", ";") and after not in ("|", "&", "|&"):
                    table = new
                else:
                    _merge(table, new)
                variants.append([segment])
            else:
                seen: list[list[str]] = []
                for index in range(max((len(v) for v in table.values()), default=1)):
                    concrete = {fd: v[index % len(v)] for fd, v in table.items()}
                    rewritten = _carry_segment(segment, concrete, pattern) if concrete else segment
                    if rewritten not in seen:
                        seen.append(rewritten)
                variants.append(seen)
            if after == "(":
                nesting.append("sub")
            elif after == ")" and nesting and nesting[-1] == "sub":
                nesting.pop()
        for index in range(max((len(v) for v in variants), default=1)):
            out: list[str] = []
            for (_, _, after), options in zip(pieces, variants):
                out.extend(options[index % len(options)])
                out.append(after)
            yield out[:-1]
