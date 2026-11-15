"""Explicit shell startup environment references for the bounded lifecycle walk."""
from __future__ import annotations

import shlex
from collections.abc import Iterator


def _startup_variable(shell: str, arguments: list[str]) -> str | None:
    """Select the startup variable from shell options, never from script arguments."""
    from cron.lifecycle_guard import _SHELL_LONG_OPTIONS_WITH_VALUES

    interactive = False
    posix = shell in {"sh", "dash", "ksh", "ash"}
    index = 0
    while index < len(arguments):
        word = arguments[index]
        index += 1
        if word in {"--", "-"} or not word.startswith(("-", "+")):
            break
        if word in _SHELL_LONG_OPTIONS_WITH_VALUES:
            index += 1
        elif word == "--posix":
            posix = True
        elif word == "--interactive":
            interactive = True
        elif not word.startswith("--"):
            if "i" in word[1:]:
                interactive = word.startswith("-")
            for flag in word[1:]:
                if flag in "oO":
                    value = arguments[index] if index < len(arguments) else ""
                    index += 1
                    if flag == "o" and value == "posix":
                        posix = word.startswith("-")
    if posix:
        return "ENV" if interactive else None
    return "BASH_ENV" if shell == "bash" and not interactive else None


def iter_startup_sources(command: str) -> Iterator[tuple[str, bool]]:
    """Source explicit assignment-prefix/env-wrapper files before child-shell payloads.

    Reuse the ordinary source walk so local/remote read limits, cycles and refusals
    stay identical. Prefix assignments are conservative across wrappers; inherited
    process environment is deliberately not consulted for remote commands.
    """
    # Assignment names may contain shell quotes or escapes. Match only after
    # the shared segment tokenizer removes them, not against raw source text.
    # Quote and backslash removal never create "="; only a $-form ($'\x3d')
    # can. Without either, no tokenized word can be an assignment, so skip the
    # extra tokenizer pass (it counts against the per-scan lexer budget).
    if "=" not in command and "$" not in command:
        return
    from cron.lifecycle_guard import (
        _executable_name, _executed_command_index, _iter_command_segment_views,
        _split_redirections,
    )

    for view in _iter_command_segment_views(command):
        view = _split_redirections(view)[0]
        index = _executed_command_index(view)
        if index is None:
            continue
        shell = _executable_name(view[index])
        if shell not in {"bash", "sh", "dash", "ksh", "ash"}:
            continue
        variable = _startup_variable(shell, view[index + 1:])
        if variable is None:
            continue
        # Wrapper chdir changes both startup resolution and its relative commands.
        # Until the recursive walk carries wrapper cwd, refuse rather than read
        # a same-named clean file from the caller's directory.
        changes_cwd = any(word.startswith(("-C", "--chdir")) for word in view[:index])
        for word in view[:index]:
            name, equals, value = word.partition("=")
            if equals and name == variable and value:
                yield value, changes_cwd


def scan_startup_sources(command, recurse, budget, cwd, executed, source) -> bool:
    """Walk startup input before -c text; never evaluate dynamic names in the guard."""
    for value, changes_cwd in iter_startup_sources(command):
        if changes_cwd:
            if executed:
                budget.refusal = "Cannot statically resolve shell startup working directory through chdir wrapper"
                return True
            continue
        if "$" in value or "`" in value:
            if executed:
                budget.refusal = "Cannot statically resolve shell startup file expansion; use a literal path"
                return True
            continue
        if recurse("source " + shlex.quote(value), cwd, executed, source,
                   nested=True, preserve_source_cwd=True):
            return True
    return False


def script_working_directory(resolved, cwd, preserve_cwd):
    """Startup sources do not chdir; retain the legacy directory rule elsewhere."""
    from cron.lifecycle_guard import _resolve_script_directory

    return cwd if preserve_cwd else _resolve_script_directory(str(resolved)) or cwd
