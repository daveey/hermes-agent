"""Printed shell payloads and coverage of unchanged quoted substrings.

A sink expands *every* adjacent pair of each quote kind, including overlapping
pairs. Every pair inside an unchanged raw span is also a pair in its parent.
Re-expanding those pairs recursively adds no coverage, but can exhaust the walk
budget. Decoded/printer/command payloads are new text and retain full expansion.
"""
from __future__ import annotations

import re
from collections.abc import Iterator

from cron.lifecycle_guard_arith_words import iter_arithmetic_word_payloads, may_set_integer_attributes

_QUOTED_LITERAL_RES = (re.compile(r"(?='([^']+)')"), re.compile(r'(?="([^"]+)")'))
_ARGUMENT_PRINTERS = frozenset({"echo", "printf"})


def quote_scope(cwd, executed, budget) -> tuple:
    """Snapshot the interpretation under which a parent's quotes are walked."""
    return cwd, executed, budget.alias_names, budget.mask_heredocs, budget.integer_context


def iter_recursive_payloads(command, nested_texts, *, cwd, executed, budget, covered_scope):
    from cron import lifecycle_guard as guard

    budget.integer_context = budget.integer_context or may_set_integer_attributes(command)
    for payload in nested_texts:
        yield payload, True, None
    for payload in guard._iter_shell_command_payloads(command):
        yield payload, False, None
    integer_context = budget.integer_context
    for payload in iter_arithmetic_word_payloads(command, integer_context=integer_context):
        yield payload, False, None
    # Snapshot after preceding nested-shell walks; shared budget state may have changed.
    scope = quote_scope(cwd, executed, budget)
    for payload, raw_quote in iter_printed_shell_payloads(
            command, include_quoted=covered_scope != scope):
        yield payload, False, scope if raw_quote else None
    # Quoted eval text can establish attributes needed by the parent's later assignments.
    if budget.integer_context and not integer_context:
        for payload in iter_arithmetic_word_payloads(command, integer_context=True):
            yield payload, False, None


def iter_printed_shell_payloads(
    command: str, *, include_quoted: bool = True, shell_sink: bool = False,
) -> Iterator[tuple[str, bool]]:
    """Yield (payload, raw_quote) when output may execute as shell code.

    A raw quote's interior quote pairs are already enumerated by this call; its
    consumer may omit ONLY their enumeration in the same interpretation scope.
    Keep both quote kinds and overlapping pairs: apostrophes inside the other
    kind must not hide later literals. Unknown producers remain conservative.
    *shell_sink*: the caller already knows the output reaches a shell.
    """
    from cron import lifecycle_guard as guard
    from tools.shell_heredoc import output_reaches_shell

    if not shell_sink and not output_reaches_shell(command):
        return
    # The sink regex is quote-blind: `|\[bash` inside a grep/rg/sqlite3 pattern reads as a pipe
    # into bash. Re-check with data-sink arguments masked (fail-closed: lines piping into a shell
    # or arguments carrying `$(`, backticks, `<(`… are never masked), so a real sink survives
    # (self-heal lifecycleguard-f82a8e8dd311).
    if not shell_sink and not output_reaches_shell(
            guard._mask_data_sink_arguments(guard.join_line_continuations(command))):
        return
    seen: set[str] = set()

    def unseen(text: str, raw_quote: bool) -> Iterator[tuple[str, bool]]:
        for variant in guard._with_escapes_decoded(text):
            if variant not in seen:
                seen.add(variant)
                yield variant, raw_quote and variant == text

    if include_quoted:
        for pattern in _QUOTED_LITERAL_RES:
            for match in pattern.finditer(command):
                yield from unseen(match[1], True)
    if not any(name in command for name in _ARGUMENT_PRINTERS):
        return
    for segment in guard._iter_command_segments(command):
        segment = segment[guard._peel_reserved_words(segment):]
        index = guard._executed_command_index(segment)
        if index is None:
            continue
        name = guard._executable_name(segment[index])
        if name in _ARGUMENT_PRINTERS:
            for line in guard._printed_argument_lines(name, segment[index + 1:]):
                yield from unseen(line, False)
