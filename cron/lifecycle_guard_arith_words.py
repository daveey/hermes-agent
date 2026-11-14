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

A LATER assignment to an integer variable is arithmetic too: after
``declare -i x``, ``x=a[\\$\\(S\\)]``, ``x+=…``, ``x[0]=…``, ``declare x=…`` and
``for x in …`` all run ``S`` (bash 5.2.21, t_01a1db20266865b2). The guard keeps no
variable state, so once the text sets an integer attribute anywhere (``eval``
strings and function bodies included) every word holding a subscript and an opener
is a payload. Without such a declaration a plain ``x=a[\\$\\(S\\)]`` stays data.
"""
from __future__ import annotations

import re
from collections.abc import Iterator

_INT_OPERATORS = frozenset({"-eq", "-ne", "-lt", "-le", "-gt", "-ge"})
_INTEGER_DECLARERS = frozenset({"declare", "typeset", "local", "readonly", "export"})
_BACKTICK_SPAN_RE = re.compile(r"`([^`]*)`")
_SUBSTITUTION_OPENERS = ("$(", "`")
# A declare-family name followed, anywhere later in the dequoted segment, by an option word
# carrying ``i``. Matched on the joined words so ``eval "declare -i x"`` and
# ``builtin declare -xi x`` count; over-matching only makes the walk read more files.
_INTEGER_DECLARATION_RE = re.compile(
    r"(?:^|[\s/])(?:declare|typeset|local|readonly|export)\s(?:.*\s)?-[A-Za-z]*i",
    re.DOTALL,
)


def _integer_declaration(arguments: list[str]) -> bool:
    """Whether a declare-family argv sets the integer attribute (``-i``, ``-ai``, ``-i -x``)."""
    return any(
        argument.startswith("-") and "i" in argument[1:]
        for argument in arguments
    )


def _name_taking_mode(name: str, arguments: list[str]) -> bool:
    return name in {"unset", "read", "mapfile", "readarray"} or (
        name in {"test", "[", "printf"} and "-v" in arguments
    ) or (name == "wait" and "-p" in arguments)


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
    # NAME-taking builtins evaluate array subscripts after shell quote removal.
    # Scan all arguments once the name-taking mode is present: options and operands
    # can be interleaved, and scanning a benign referenced script remains allowed.
    if _name_taking_mode(name, arguments):
        yield from arguments


def _recursive_arithmetic_segment(segment: list[str]) -> bool:
    from cron import lifecycle_guard as guard

    words = segment[guard._peel_reserved_words(segment):]
    index = guard._executed_command_index(words)
    if index is None:
        return False
    name = guard._executable_name(words[index])
    arguments = words[index + 1:]
    # NAME subscripts recursively evaluate variables just like explicit arithmetic.
    # A plain NAME does not: `read x` must not activate earlier data assignments.
    return name == "let" or (
        name == "[[" and any(token in _INT_OPERATORS for token in arguments)
    ) or (
        _name_taking_mode(name, arguments) and any("[" in token for token in arguments)
    )


def _indirect_assignment_words(segment: list[str]) -> Iterator[str]:
    from cron import lifecycle_guard as guard

    words = segment[guard._peel_reserved_words(segment):]
    index = guard._executed_command_index(words)
    if index is None:
        yield from words
    else:
        yield from words[:index]
        if guard._executable_name(words[index]) in _INTEGER_DECLARERS:
            yield from words[index + 1:]


def iter_arithmetic_word_payloads(command: str) -> Iterator[str]:
    """Yield dequoted arithmetic operands of *command* whose subscript may run a substitution."""
    from cron import lifecycle_guard as guard

    # Cheap prefilter: without a subscript bracket and a `$`/backtick no operand can qualify,
    # so most commands never pay for another tokenization.
    if "[" not in command or ("$" not in command and "`" not in command):
        return
    seen: set[str] = set()
    segments = list(guard._iter_command_segments(command))
    # With an integer attribute set anywhere, any word may be an assignment to it.
    integer_text = any(
        _INTEGER_DECLARATION_RE.search(" ".join(segment)) for segment in segments
    )
    # Arithmetic recursively evaluates variable values. Recover earlier dequoted
    # assignments too, not just the word at the evaluation site. Preserve argv
    # boundaries so a quoted shell program or echo argument is not an assignment.
    recursive_text = "((" in command or any(
        _recursive_arithmetic_segment(segment) for segment in segments
    )
    for segment in segments:
        operands = list(segment if integer_text else _arithmetic_operands(segment))
        if recursive_text:
            operands.extend(_indirect_assignment_words(segment))
        for operand in operands:
            if "[" not in operand or not any(o in operand for o in _SUBSTITUTION_OPENERS):
                continue
            payload = _BACKTICK_SPAN_RE.sub(r"$(\1)", operand)
            if payload not in seen:
                seen.add(payload)
                yield payload
