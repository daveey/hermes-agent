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
# Metacharacters that are literal inside single quotes. Letters stay so a
# quoted shell name can still certify a sink (`| 'bash'`).
_SINGLE_QUOTE_META = frozenset("|&;<>()$`{}")
# Any output process or coprocess denies the speculative-quote narrowing, whatever it runs.
_PROC_SUB_SINK = re.compile(r">\s*>\(|\bcoproc\b")


def mask_single_quoted_metacharacters(text: str) -> str:
    """Blank shell metacharacters inside single-quoted spans of *text*.

    Fail closed (return *text* unchanged) when quote state is unreliable: a
    heredoc opener, an ANSI-C quote, or an unbalanced quote. Double-quoted
    spans are never masked; their substitutions stay live.
    """
    from cron.lifecycle_guard_lines import strip_shell_comments

    if "<<" in text or "$'" in text:
        return text
    stripped = strip_shell_comments(text)
    result: list[str] = []
    single = double = escaped = False
    for char in stripped:
        if single:
            if char == "'":
                single = False
                result.append(char)
            else:
                result.append("_" if char in _SINGLE_QUOTE_META else char)
            continue
        result.append(char)
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            double = not double
        elif char == "'" and not double:
            single = True
    if single or double:
        return text
    return "".join(result)


def quote_aware_shell_sink(command: str) -> bool:
    """Whether *command* feeds output to a shell outside single-quoted literals.

    A backtick or pipe inside single quotes is data, not evidence that a
    harvested quote is executed. Unreliable quote state keeps the quote-blind
    certificate.
    """
    from tools.shell_heredoc import output_reaches_shell

    masked = mask_single_quoted_metacharacters(command)
    return (output_reaches_shell(masked, binding_source=command)
            or _PROC_SUB_SINK.search(masked) is not None)


def quote_scope(cwd, executed, budget) -> tuple:
    """Snapshot the interpretation under which a parent's quotes are walked."""
    return cwd, executed, budget.alias_names, budget.mask_heredocs, budget.integer_context


def iter_recursive_payloads(command, nested_texts, *, cwd, executed, budget, covered_scope):
    """Yield payload, nested-shell, quote coverage, variable provenance, printer line.

    Printed strings/overlapping quote spans are conservative candidates, not
    original shell syntax. Resolving their standalone ``$log`` as a command would
    promote a redirect's data binding into execution. Real nested shell programs
    retain variable discovery; referenced files do too at their read boundary.
    """
    from cron import lifecycle_guard as guard

    budget.integer_context = budget.integer_context or may_set_integer_attributes(
        command, aliases_defined=budget.aliases_defined,
    )
    for payload in nested_texts:
        yield payload, True, None, True, False
    for payload in guard._iter_shell_command_payloads(command):
        yield payload, False, None, True, False
    integer_context = budget.integer_context
    for payload in iter_arithmetic_word_payloads(command, integer_context=integer_context):
        yield payload, False, None, False, False
    # Snapshot after preceding nested-shell walks; shared budget state may have changed.
    scope = quote_scope(cwd, executed, budget)
    for payload, raw_quote in iter_printed_shell_payloads(
            command, include_quoted=covered_scope != scope):
        # Printer lines are decoded data, not syntax. Mark them so a parent
        # without a real sink can drop placeholder splices without treating
        # the line as a raw quote (that would omit its own quote walk).
        yield payload, False, scope if raw_quote else None, False, not raw_quote
    # Quoted eval text can establish attributes needed by the parent's later assignments.
    if budget.integer_context and not integer_context:
        for payload in iter_arithmetic_word_payloads(command, integer_context=True):
            yield payload, False, None, False, False


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
            guard._mask_data_sink_arguments(guard.join_line_continuations(command)),
            binding_source=command):
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
