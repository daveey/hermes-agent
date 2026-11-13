"""Nested shells holding aliases of their own, for the lifecycle guard (review 5894325109 nit 1).

A shell the command starts may hold aliases the session's names say nothing about: its own
startup files define them. What such a shell parses is walked with unknown alias names.
"""

from __future__ import annotations

import contextlib
import itertools
import re
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from cron.lifecycle_guard import (
    _SHELL_EXECUTABLES,
    _command_token_index,
    _executable_name,
    _executed_command_index,
    _iter_command_segment_views,
    _peel_transparent_prefixes,
    _resolve_lenient,
    _resolved_or_nothing,
    _shell_options_end,
    _split_redirections,
)

if TYPE_CHECKING:
    from cron.lifecycle_guard import _LifecycleScanBudget


@contextlib.contextmanager
def nested_shell_scope(budget: _LifecycleScanBudget) -> Iterator[None]:
    """Walk text such a nested shell parses: its alias names are unknown, so ``alias_names`` is
    ``None`` inside and every ``NAME (`` may open a keyword-led subshell. The session's aliases do
    not reach that shell, so they no longer keep heredoc bodies visible there (``mask_heredocs``).
    Its own startup files are trusted not to rename an interpreter (``alias python3=bash``), as
    everywhere a remote or nested shell's body is masked. On exit the session's state returns,
    except that an alias defined by walked text stays sticky (``aliases_defined``)."""
    saved = budget.alias_names, budget.mask_heredocs
    budget.alias_names, budget.mask_heredocs = None, True
    try:
        yield
    finally:
        budget.mask_heredocs = saved[1]
        if not budget.aliases_defined:
            budget.alias_names = saved[0]


def shell_scope(budget: _LifecycleScanBudget, nested: bool) -> contextlib.AbstractContextManager:
    """``nested_shell_scope(budget)`` when *nested*, else a no-op scope."""
    return nested_shell_scope(budget) if nested else contextlib.nullcontext()


def collect_nested_shell_inputs(
    command: str, walk_command: str, *, cwd: Optional[str], budget: _LifecycleScanBudget,
) -> tuple[list[str], set[Path]]:
    """``(texts, resolved scripts)`` a nested shell holding its own aliases parses in *command*
    (see ``_iter_nested_shell_inputs``). Already unknown alias names need no second walk."""
    texts: list[str] = []
    scripts: set[Path] = set()
    if budget.alias_names is None:
        return texts, scripts
    for text, script in _iter_nested_shell_inputs(command, walk_command, cwd=cwd):
        if script is not None:
            scripts.add(_resolve_lenient(script))
        elif text:
            texts.append(text)
    return texts, scripts


# Login/interactive POSIX shells (`-l`, `-i`, `-ils`, `--login`), every zsh (`~/.zshenv` runs
# even non-interactively), a remote shell over ssh (sshd's bash sources `~/.bashrc`; no remote
# command means a login shell), and `sudo -i`/`-s`, `su`, `exec -l`.
_LOGIN_SHELL_OPTION_RE = re.compile(r"-[A-Za-z]*[il][A-Za-z]*|--login|--interactive")
_SUDO_SHELL_OPTION_RE = re.compile(r"-[A-Za-z]*[is][A-Za-z]*|--login|--shell")
_SUDO_VALUE_OPTIONS = frozenset("ugUCprtTh")
# Cheap pre-filter: every such shell's name ends in "sh" or is su/sudo/exec.
_NESTED_SHELL_HINT_RE = re.compile(r"sh\b|\bsu(?:do)?\b|\bexec\b")


def _option_words(arguments: list[str], value_letters: frozenset[str]) -> tuple[list[str], int]:
    """``(option words, index of the first operand)`` of a getopt-style argv tail; a short option
    in *value_letters* consumes the next word unless the value is glued (``-uroot``)."""
    options: list[str] = []
    index = 0
    while index < len(arguments) and arguments[index].startswith("-") and arguments[index] != "-":
        word = arguments[index]
        index += 1
        if word == "--":
            break
        options.append(word)
        if not word.startswith("--"):
            for position, letter in enumerate(word[1:], 1):
                if letter in value_letters:
                    index += position == len(word) - 1  # `-u root` takes the next word
                    break
    return options, index


def _shell_inputs(arguments: list[str], shell: str) -> tuple[str, Optional[str]]:
    """What a POSIX *shell* given *arguments* parses as code: ``("payload", text)`` for ``-c``,
    ``("script", path)`` for a script operand, else ``("stdin", None)``."""
    index, command_string, reads_stdin = _shell_options_end(arguments, shell)
    if command_string:
        return ("payload", arguments[index]) if index < len(arguments) else ("stdin", None)
    if index < len(arguments) and not reads_stdin:
        return "script", arguments[index]
    return "stdin", None


def _remote_reads_stdin(remote: list[str]) -> bool:
    """Whether an ssh remote command (its words, as ssh joins them) hands stdin to a shell:
    none (a login shell), unparsable, or any segment running a shell that reads stdin."""
    if not remote:
        return True
    text = " ".join(remote)
    try:
        views = list(_iter_command_segment_views(text))
    except ValueError:
        return True
    for view in views:
        index = _executed_command_index(view)
        if index is None:
            continue
        name = _executable_name(view[index])
        if name in _SHELL_EXECUTABLES and _shell_inputs(
                _split_redirections(view[index + 1:])[0], name)[0] == "stdin":
            return True
        if name in ("sudo", "su", "ssh"):
            return True  # another hop: fail closed rather than model it
    return False


def _nested_shell_input(view: list[str]) -> Optional[tuple[str, Optional[str]]]:
    """``_shell_inputs`` of the nested shell *view* starts when that shell holds aliases of its
    own (see above), else ``None``. For ssh only stdin counts: a remote ``-c`` or script names
    remote text and paths."""
    index = _command_token_index(view)
    if index is None:
        return None
    name = _executable_name(view[index])
    if name == "su":
        options = view[index + 1:]
        for position, word in enumerate(options[:-1]):
            if word in ("-c", "--command"):
                return "payload", options[position + 1]
        return "stdin", None
    if name == "sudo":
        options, operand = _option_words(view[index + 1:], _SUDO_VALUE_OPTIONS)
        if any(_SUDO_SHELL_OPTION_RE.fullmatch(word) for word in options):
            rest = _split_redirections(view[index + 1 + operand:])[0]
            return ("payload", " ".join(rest)) if rest else ("stdin", None)
    login_exec = name == "exec" and "-l" in itertools.takewhile(
        lambda word: word.startswith("-"), view[index + 1:])
    peeled = _peel_transparent_prefixes(view, index)
    if peeled >= len(view):
        return None
    name = _executable_name(view[peeled])
    arguments = _split_redirections(view[peeled + 1:])[0]
    if name == "ssh":
        from tools.shell_heredoc import _SSH_VALUE_OPTIONS

        _options, host = _option_words(arguments, frozenset(_SSH_VALUE_OPTIONS))
        return ("stdin", None) if _remote_reads_stdin(arguments[host + 1:]) else None
    if name not in _SHELL_EXECUTABLES:
        return None
    options_end = _shell_options_end(arguments, name)[0]
    if name == "zsh" or login_exec or any(
            _LOGIN_SHELL_OPTION_RE.fullmatch(word) for word in arguments[:options_end]):
        return _shell_inputs(arguments, name)
    return None


def _feeds_nested_shell(owner: Optional[str]) -> bool:
    """Whether a heredoc's *owner* text hands the body to such a nested shell as its script
    (``None``: unknown owner, fail closed)."""
    if owner is None:
        return True
    if not _NESTED_SHELL_HINT_RE.search(owner):
        return False
    try:
        views = list(_iter_command_segment_views(owner))
    except ValueError:
        return True
    return any(_nested_shell_input(view) == ("stdin", None) for view in views)


def _iter_nested_shell_inputs(
    command: str, walk_command: str, *, cwd: Optional[str],
) -> Iterator[tuple[Optional[str], Optional[Path]]]:
    """``(text, None)`` or ``(None, script)``: what a nested shell holding its own aliases parses
    from *command*: heredoc bodies fed to it (at any depth, read from the unmasked *command*),
    its ``-c`` payload and its script (from *walk_command*, as the ordinary walk reads them)."""
    if not _NESTED_SHELL_HINT_RE.search(command):
        return
    from tools.shell_heredoc import iter_heredoc_bodies

    for owner, body in iter_heredoc_bodies(command):
        if _feeds_nested_shell(owner):
            yield body, None
    for view in _iter_command_segment_views(walk_command):
        found = _nested_shell_input(view)
        if found is None:
            continue
        kind, value = found
        if kind == "payload" and value:
            yield value, None
        elif kind == "script" and value:
            for path in _resolved_or_nothing(value, cwd):
                yield None, path
