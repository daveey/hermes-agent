"""Subscript substitutions in arithmetic WORDS, recovered after quote removal.

``let`` operands, ``[[ … -eq … ]]`` operands and integer declarations are shell
words first: bash removes quotes and backslashes, THEN evaluates the result as
arithmetic, where an array subscript runs its command substitution. So
``let x=a[\\$\\(S\\)]``, ``let x=a['$'\\(S\\)]`` and ``let x=a[\\`S\\`]`` all run ``S``
(bash 5.2.21) although no substitution is visible in the raw text the reference
walk tokenizes (t_01a1db0af44d1601). Each such dequoted operand that holds a
subscript and a substitution opener is handed back to the walk as shell text, with
backtick spans spelled ``$( … )`` so the walk follows them like any substitution.

Deliberately coarse, failing towards scanning: any ``[`` plus opener counts, and an
operand of an integer operator counts wherever the operator sits (``[ … ]``/``test``
included). A payload only makes the walk READ the referenced file; it refuses only
when that file holds a lifecycle command (or cannot be scanned).
"""
from __future__ import annotations

import re
from collections.abc import Iterator

_INT_OPERATORS = frozenset({"-eq", "-ne", "-lt", "-le", "-gt", "-ge"})
_INTEGER_DECLARERS = frozenset({"declare", "typeset", "local", "readonly", "export"})
_BACKTICK_SPAN_RE = re.compile(r"`([^`]*)`")
_SUBSTITUTION_OPENERS = ("$(", "`")


def _integer_declaration(arguments: list[str]) -> bool:
    """Whether a declare-family argv sets the integer attribute (``-i``, ``-ai``, ``-i -x``)."""
    return any(
        argument.startswith("-") and "i" in argument[1:]
        for argument in arguments
    )


def _arithmetic_operands(segment: list[str]) -> Iterator[str]:
    from cron import lifecycle_guard as guard

    for position, token in enumerate(segment):
        if token in _INT_OPERATORS:
            if position > 0:
                yield segment[position - 1]
            if position + 1 < len(segment):
                yield segment[position + 1]
    words = segment[guard._peel_reserved_words(segment):]
    index = guard._executed_command_index(words)
    if index is None:
        return
    name = guard._executable_name(words[index])
    arguments = words[index + 1:]
    if name == "let" or (name in _INTEGER_DECLARERS and _integer_declaration(arguments)):
        yield from arguments


def iter_arithmetic_word_payloads(command: str) -> Iterator[str]:
    """Yield dequoted arithmetic operands of *command* whose subscript may run a substitution."""
    from cron import lifecycle_guard as guard

    # Cheap prefilter: without a subscript bracket and a `$`/backtick no operand can qualify,
    # so most commands never pay for another tokenization.
    if "[" not in command or ("$" not in command and "`" not in command):
        return
    seen: set[str] = set()
    for segment in guard._iter_command_segments(command):
        for operand in _arithmetic_operands(segment):
            if "[" not in operand or not any(o in operand for o in _SUBSTITUTION_OPENERS):
                continue
            payload = _BACKTICK_SPAN_RE.sub(r"$(\1)", operand)
            if payload not in seen:
                seen.add(payload)
                yield payload
