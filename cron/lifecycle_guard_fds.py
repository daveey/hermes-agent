"""Carry redirection-only exec file descriptors into later script commands.

Keep lexical redirection metadata intact; quoted operator-shaped arguments are data.
The caller's existing ordered fd resolver handles each command's local redirections.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
import re

from cron.lifecycle_guard_words import ShellWord, redirection_parts


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


def _apply(table: dict[str, str], fd: str, operator: str, target: str) -> None:
    fd = fd or ("0" if operator.startswith("<") else "1")
    source = target.removesuffix("-")
    value = table.get(source) if operator in ("<&", ">&") else None
    table.pop(fd, None)
    if operator in ("<", "<>") and target:
        table[fd] = target
    elif value is not None:
        table[fd] = value
    if operator in ("<&", ">&") and target.endswith("-"):
        table.pop(source, None)
    if operator in ("&>", "&>>"):
        table.pop("2", None)


def _carry_segment(segment: list[str], table: dict[str, str], command: int,
                   pattern: re.Pattern[str]) -> list[str]:
    found = list(_redirections(segment[command + 1:], pattern))
    if segment[command:command + 1] == ["exec"] and sum(
            end - start for start, end, _ in found) == len(segment) - command - 1:
        for _, _, parts in found:
            _apply(table, *parts)
        return segment
    out: list[str] = []
    local = dict(table)
    position = 0
    for start, end, (fd, operator, target) in _redirections(segment, pattern):
        source = target.removesuffix("-")
        value = local.get(source) if operator in ("<&", ">&") else None
        if value is not None:
            out.extend(segment[position:start])
            destination = fd or ("0" if operator.startswith("<") else "1")
            out.append(ShellWord(destination + "<", (destination, "<", "")))
            out.append(ShellWord(value, None))
            # A moved source is closed after copying, including same-fd moves.
            if target.endswith("-"):
                out.append(ShellWord(source + "<&-", (source, "<&", "-")))
            position = end
        _apply(local, fd, operator, target)
    out.extend(segment[position:])
    return out


def carry_exec_descriptors(
    lines: Iterable[list[str]], *, pattern: re.Pattern[str],
    peel_reserved_words: Callable[[list[str]], int], control_chars: frozenset[str],
) -> Iterator[list[str]]:
    """Resolve inherited fd copies in script order, without changing pipeline boundaries.

    Redirection-only execs in branches/groups are conservatively carried too. Each
    script gets its own table, so heredoc bodies cannot rebind their enclosing script.
    """
    table: dict[str, str] = {}
    for tokens in lines:
        if not table and "exec" not in tokens:
            yield tokens
            continue
        out: list[str] = []
        segment: list[str] = []
        for token in [*tokens, ";"]:
            if not (token and set(token) <= control_chars):
                segment.append(token)
                continue
            out.extend(_carry_segment(segment, table, peel_reserved_words(segment), pattern))
            out.append(token)
            segment = []
        yield out[:-1]
