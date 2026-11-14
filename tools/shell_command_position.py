"""Command-position helpers shared by lifecycle and arithmetic sink scans."""

from __future__ import annotations

import re
import shlex

from tools.shell_prefix_expansions import mask_prefix_expansions

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


def _prefix_words(prefix: str) -> list[str] | None:
    words: list[str] = []
    redirect_target = False
    was_target = False
    for match in _PREFIX_TOKEN.finditer(mask_prefix_expansions(prefix.replace("\\\n", "")) + '"'):
        raw = match.group()
        was_target = redirect_target
        redirect_target = False
        if was_target:
            continue
        if _REDIRECTION_RE.fullmatch(raw):
            redirect_target = True
        elif raw in (";", "\n", "&", "|", "(", ")"):
            words.clear()
        else:
            words.append("".join(shlex.split(raw)))
    return None if was_target else words


def quoted_name_is_arithmetic(prefix: str) -> bool:
    """Whether the open double-quoted name at *prefix* is a builtin's target.

    The caller stops just before its subscript opener, inside double quotes. Close
    that quote for tokenization; literal separators remain data, not command starts.
    """
    try:
        words = _prefix_words(prefix)
    except (ValueError, RecursionError):
        return True  # An unrecognized word must not hide an arithmetic sink.
    if words is None:
        return False  # The current word is a redirection destination.
    index = _builtin_index(words)
    if index < 0:
        return False  # Proven command introspection, not an unknown command.
    if index >= len(words):
        return True
    name, arguments = words[index], words[index + 1:]
    if name in _NAME_CONSUMERS:
        return bool(arguments)
    if name == "printf" and len(arguments) == 1:
        return arguments[0].startswith("-v")
    if name in ("printf", "test", "[", "[["):
        return len(arguments) >= 2 and arguments[-2] == "-v"
    # Unknown commands may be functions or builtins with arithmetic operands.
    # Only positively identified data consumers may exempt a quoted subscript.
    return name not in ("echo", "cat", "grep")
