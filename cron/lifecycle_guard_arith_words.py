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

The recursive walk carries this hint across eval and referenced texts. Source/dot,
an expanded command name, or expanded declare-family arguments may establish the
attribute too: conservatively scan subscript payloads without evaluating any text.
The hint is sticky for one scan (even across child shells), not variable/scope
tracking. It may read extra files, but clean script twins remain allowed.
"""
from __future__ import annotations

import re
from collections.abc import Iterator

_INT_OPERATORS = frozenset({"-eq", "-ne", "-lt", "-le", "-gt", "-ge"})
_INTEGER_DECLARERS = frozenset({"declare", "typeset", "local", "readonly", "export"})
_BACKTICK_SPAN_RE = re.compile(r"`([^`]*)`")
_SUBSTITUTION_OPENERS = ("$(", "`")
_ASSIGNMENT_WORD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\[.*\])?\+?=", re.DOTALL)
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
    ) or (name == "wait" and "-p" in arguments) or (
        name in _INTEGER_DECLARERS
        and (any(any(c in argument.split("=", 1)[0] for c in "[$") for argument in arguments)
             or any(argument.startswith("-") and "n" in argument[1:] for argument in arguments))
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
    # NAME-taking builtins evaluate array subscripts after shell quote removal.
    # Scan all arguments once the name-taking mode is present: options and operands
    # can be interleaved, and scanning a benign referenced script remains allowed.
    if _name_taking_mode(name, arguments):
        yield from arguments


def _recursive_arithmetic_segment(segment: list[str]) -> bool:
    from cron import lifecycle_guard as guard

    words = segment[guard._peel_reserved_words(segment):]
    index = guard._executed_command_index(words)
    # A compound initializer is segmented as `a+=`, `[x]=1`; its indexed
    # member is an arithmetic site too. Do not inspect ordinary command argv.
    prefix = words if index is None else words[:index + 1]
    if any(re.match(r"(?:[A-Za-z_]\w*)?\[.*\]\+?=", word) for word in prefix):
        return True
    if index is None:
        return False
    name = guard._executable_name(words[index])
    arguments = words[index + 1:]
    # NAME subscripts recursively evaluate variables just like explicit arithmetic.
    # Expansion-supplied names and nameref targets can hide the entire subscript
    # in an earlier assignment. A plain NAME does not: `read x` stays inert.
    # Bash unset does not re-expand a subscript supplied through a parameter.
    expanded_name = any("[" in token or (name != "unset" and "$" in token)
                        for token in arguments)
    return name == "let" or (
        name == "[[" and (any(token in _INT_OPERATORS for token in arguments)
                          or ("-v" in arguments and expanded_name))
    ) or (
        _name_taking_mode(name, arguments) and expanded_name
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


def _implicit_arithmetic_expansion(command: str) -> bool:
    """Recognize active legacy arithmetic, parameter indices and substring offsets.

    This is only a context prefilter, not an evaluator. Preserve shell quoting so
    literal examples in printf/echo do not activate unrelated data assignments.
    """
    tokens = re.compile(r'''\\.|'|"|\$\$|\$\[|\$\{![A-Za-z_]\w*(?=[}:])|\$\{[#!]?(?:[A-Za-z_]\w*|\d+|[@*#?$!-])(?:\[|:(?![-=?+]))''')
    quoted = False
    pos = 0
    while match := tokens.search(command, pos):
        token = match.group()
        pos = match.end()
        if token.startswith("\\") or token == "$$":
            continue
        if token == '"':
            quoted = not quoted
        elif token == "'" and not quoted:
            close = command.find("'", pos)
            if close < 0:
                return True  # uncertain shell syntax: fail towards scanning
            pos = close + 1
        elif token.startswith("$"):
            return True
    return False


def may_set_integer_attributes(command: str) -> bool:
    """Conservative attribute hint, including declarations without a substitution."""
    from cron import lifecycle_guard as guard

    if not any(hint in command for hint in (*_INTEGER_DECLARERS, "source", ".", "$", "`")):
        return False
    for segment in guard._iter_command_segments(command):
        if _INTEGER_DECLARATION_RE.search(" ".join(segment)):
            return True
        words = segment[guard._peel_reserved_words(segment):]
        index = guard._executed_command_index(words)
        if index is None:
            continue
        name = guard._executable_name(words[index])
        # Do not evaluate expansions or source files to discover attributes. Either
        # can introduce an integer declaration; allow the later subscript walk now,
        # before referenced files are traversed. Unknown commands may be declare.
        if name in {"source", "."} or (
            any(char in words[index] for char in "$`")
            and not _ASSIGNMENT_WORD_RE.match(words[index])
        ):
            return True
        if name in _INTEGER_DECLARERS and any(
            ("$" in word or "`" in word) and not _ASSIGNMENT_WORD_RE.match(word)
            for word in words[index + 1:]
        ):
            return True
    return False


def iter_arithmetic_word_payloads(command: str, *, integer_context: bool = False) -> Iterator[str]:
    """Yield dequoted arithmetic operands of *command* whose subscript may run a substitution."""
    from cron import lifecycle_guard as guard

    # Cheap prefilter: without a subscript bracket and a `$`/backtick no operand can qualify,
    # so most commands never pay for another tokenization.
    if "[" not in command or ("$" not in command and "`" not in command):
        return
    seen: set[str] = set()
    segments = list(guard._iter_command_segments(command))
    # With an integer attribute set anywhere, any word may be an assignment to it.
    integer_text = integer_context or any(
        _INTEGER_DECLARATION_RE.search(" ".join(segment)) for segment in segments
    )
    # Arithmetic recursively evaluates variable values. Recover earlier dequoted
    # assignments too, not just the word at the evaluation site. Preserve argv
    # boundaries so a quoted shell program or echo argument is not an assignment.
    recursive_text = "((" in command or _implicit_arithmetic_expansion(command) or any(
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
            if payload != command and payload not in seen:
                seen.add(payload)
                yield payload
