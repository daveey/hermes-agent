"""Nested shells holding aliases of their own, for the lifecycle guard (review 5894325109 nit 1).

A shell the command starts may hold aliases the session's names say nothing about: its own
startup files define them. What such a shell parses is walked with unknown alias names.
"""

from __future__ import annotations

import contextlib
import itertools
import re
import shlex
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from cron.lifecycle_guard import (
    _REDIRECTION,
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
from cron.lifecycle_guard_groups import _CLOSERS

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


def unvisited_scopes(
    visited: set[object], budget: _LifecycleScanBudget, resolved: Path, *, python_source: bool,
    nested: bool,
) -> list[tuple[bool, tuple]]:
    """``(nested, visit key)`` for each scope *resolved* still needs a walk in. A shell walk
    depends on its scope (alias names, heredoc masking), so ``visited`` keys carry it: a path one
    reference hands to a nested shell and another to the session shell is walked in both scopes
    (review 5897863903 B1), the session one exactly as before. A shell walk in any scope subsumes
    the file's Python regex scan, never the reverse; the caller also records ``key[:2]``, the
    scope-free ``(resolved, "shell")`` mark, for that."""
    if python_source:
        seen = (resolved, "python") in visited or (resolved, "shell") in visited
        return [] if seen else [(False, (resolved, "python"))]
    wanted = [(False, (budget.alias_names, budget.mask_heredocs))]
    if nested:
        wanted.append((True, (None, True)))  # nested_shell_scope's state
    keys: dict[tuple, bool] = {}
    for flag, scope in wanted:
        keys.setdefault((resolved, "shell", scope, budget.integer_context), flag)
    return [(flag, key) for key, flag in keys.items() if key not in visited]


def collect_nested_shell_inputs(
    command: str, walk_command: str, *, cwd: Optional[str], budget: _LifecycleScanBudget,
    session_payloads: Sequence[str] = (),
) -> tuple[list[str], set[Path]]:
    """``(texts, resolved scripts)`` a nested shell holding its own aliases parses in *command*
    (see ``_iter_nested_shell_inputs``). Under already unknown alias names a heredoc body needs
    no second walk (the session walk reads it in the same scope), but ``-c`` payloads, here-strings
    and scripts are still returned: `su -lc T`, `runuser --comm=T`, `su -l bob < f` hand the
    shell text or a path no ordinary walk names (review 5903452583 B1). There the session scope is
    at least as strict as the nested one (it may also unmask heredoc bodies), so a text among
    *session_payloads* (walked in the session scope anyway) is left out: walked and charged once."""
    texts: list[str] = []
    scripts: set[Path] = set()
    unknown_names = budget.alias_names is None
    if not unknown_names:
        texts.extend(_iter_nested_printed_payloads(walk_command))
    for text, script in _iter_nested_shell_inputs(command, walk_command, cwd=cwd,
                                                  heredocs=not unknown_names):
        if script is not None:
            scripts.add(_resolve_lenient(script))
        elif text and not (unknown_names and text in session_payloads):
            texts.append(text)
    return texts, scripts


# Login/interactive POSIX shells (`-l`, `-i`, `-ils`, `--login`), every zsh (`~/.zshenv` runs
# even non-interactively), a remote shell over ssh (sshd's bash sources `~/.bashrc`; no remote
# command means a login shell), and `sudo -i`/`-s`, `su`, `exec -l`.
_LOGIN_SHELL_OPTION_RE = re.compile(r"-[A-Za-z]*[il][A-Za-z]*|--login|--interactive")
_SUDO_SHELL_OPTION_RE = re.compile(r"-[A-Za-z]*[is][A-Za-z]*|--login|--shell")
_SUDO_VALUE_OPTIONS = frozenset("ugUCprtTh")
# Cheap pre-filter: every such shell's name ends in "sh" or is su/sudo/exec.
_NESTED_SHELL_HINT_RE = re.compile(r"sh\b|\bsu(?:do)?\b|\b(?:exec|runuser|doas)\b")


def _option_words(
    arguments: list[str], value_letters: frozenset[str],
    value_names: frozenset[str] = frozenset(),
) -> tuple[list[str], int]:
    """``(option words, index of the first operand)`` of a getopt-style argv tail.

    Value-taking short and long options consume a separate value, but not a glued one.
    Values never enter the returned option namespace, even when they look like flags.
    """
    options: list[str] = []
    index = 0
    while index < len(arguments) and arguments[index].startswith("-") and arguments[index] != "-":
        word = arguments[index]
        index += 1
        if word == "--":
            break
        options.append(word)
        if word.startswith("--"):
            index += word in value_names  # `--name=value` already carries its value
        else:
            for position, letter in enumerate(word[1:], 1):
                if letter in value_letters:
                    index += position == len(word) - 1  # `-u root` takes the next word
                    break
    return options, index


# util-linux su/runuser long options; getopt_long accepts any unique prefix of these.
_SU_LONG_OPTIONS = (
    "--command", "--session-command", "--fast", "--group", "--supp-group", "--login",
    "--preserve-environment", "--pty", "--no-pty", "--shell", "--whitelist-environment",
    "--user", "--help", "--version",
)


def _su_long_option(word: str) -> str:
    """*word* with an abbreviated su/runuser long option spelled out (``--whitelist`` ->
    ``--whitelist-environment``, ``--comm=x`` -> ``--command=x``); other words unchanged."""
    if not word.startswith("--") or word == "--":
        return word
    name, equals, value = word.partition("=")
    if name in _SU_LONG_OPTIONS:
        return word
    matches = [option for option in _SU_LONG_OPTIONS if option.startswith(name)]
    return matches[0] + equals + value if len(matches) == 1 else word


def _su_payload(words: list[str]) -> Optional[str]:
    """The first ``-c``/``--command``/``--session-command`` value anywhere in su-mode *words*
    (long options already spelled out), including clustered and glued short forms. Operands
    after the user, even past ``--``, reach the shell, so nothing stops the scan."""
    for position, word in enumerate(words):
        following = words[position + 1] if position + 1 < len(words) else None
        if word.startswith("--"):
            name, equals, value = word.partition("=")
            if name in ("--command", "--session-command"):
                if equals:
                    return value
                if following is not None:
                    return following
            continue
        if not word.startswith("-") or word == "-":
            continue
        for letter_index, letter in enumerate(word[1:], 1):
            if letter in "sgGuw":
                break  # takes a value: the rest of the word (or the next one) is not flags
            if letter == "c":
                glued = word[letter_index + 1:]
                if glued:
                    return glued
                if following is not None:
                    return following
                break
    return None


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
    if name in ("su", "runuser"):
        options = [_su_long_option(word) for word in view[index + 1:]]
        direct_user = name == "runuser" and any(
            word == "--user" or word.startswith("--user=")
            or re.fullmatch(r"-[fmpPl]*u.*", word)
            for word in _option_words(options, frozenset("ucsgGw"), frozenset({
                "--user", "--command", "--session-command", "--shell", "--group",
                "--supp-group", "--whitelist-environment",
            }))[0]
        )
        if not direct_user:
            # Direct-user runuser executes argv, not a shell command string. In su mode the
            # operands after the user (even after `--`) are handed to the shell, so a `-c`
            # anywhere is shell code: scan every word and fail closed.
            payload = _su_payload(options)
            return ("payload", payload) if payload is not None else ("stdin", None)
    if name == "doas":
        options, _operand = _option_words(view[index + 1:], frozenset("auC"))
        if any(re.fullmatch(r"-[A-Za-z]*s[A-Za-z]*", word) for word in options):
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
    # ENV is read by interactive POSIX shells (already covered by -i); BASH_ENV
    # additionally lets a plain non-interactive bash define and enable aliases.
    startup_env = name == "bash" and any(word.startswith("BASH_ENV=") for word in view[:peeled])
    if name == "zsh" or login_exec or startup_env or any(
            _LOGIN_SHELL_OPTION_RE.fullmatch(word) for word in arguments[:options_end]):
        return _shell_inputs(arguments, name)
    return None


# Printed pipeline structure (PR #2928 review 5901902665). A word opens or closes a compound
# command only in command position (`echo {` prints a brace); `(`/`)` are operators and always do.
_GROUP_OPEN_WORDS = frozenset({"{", "if", "while", "until", "for", "select", "case"})
_GROUP_CLOSE_WORDS = frozenset({"}", "fi", "done", "esac"})
# Reserved words after which the next word is in command position again (`for`/`select` are not:
# their name and word list are no commands).
_COMMAND_PREFIX_WORDS = frozenset({"{", "if", "while", "until", "then", "else", "elif", "do", "!",
                                   "time"})
_LIST_OPERATORS = frozenset({";", "&", "&&", "||", ";;"})
_PIPE_OPERATORS = frozenset({"|", "|&"})


def _is_heredoc_opener(word: str) -> bool:
    return word.startswith("<<") and not word.startswith("<<<")


def _lines_with_heredoc_bodies(command: str) -> Iterator[tuple[str, list[str]]]:
    """``(logical line, bodies of the heredocs it opens)``, in order. An opener that does not
    parse or has no terminator takes the rest of *command* as its body (fail closed)."""
    if "<<" not in command:
        yield command, []
        return
    from tools.shell_heredoc import _heredoc_bodies, _scan_heredoc_command_unit

    cursor = 0
    last_opener = command.rfind("<<")
    while cursor < len(command):
        if cursor > last_opener:
            yield command[cursor:], []
            return
        end, specs, unknown_operator, _post_list, _owner = _scan_heredoc_command_unit(
            command, cursor)
        line = command[cursor:end]
        bodies = _heredoc_bodies(command, end + 1, specs) if specs and end < len(command) else None
        if unknown_operator or (specs and bodies is None):
            yield line, [command[end + 1:]]
            return
        if not bodies:
            yield line, []
            cursor = end + 1
            continue
        yield line, [command[start:terminator] for start, terminator, _after in bodies]
        cursor = bodies[-1][2]


class _PrintedUnit:
    """One printed-text unit: ``(role, word)`` events and the heredoc bodies its openers own."""

    __slots__ = ("bodies", "events")

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.bodies: list[str] = []

    def text(self) -> str:
        return " ".join(word if role != "word" else shlex.quote(word)
                        for role, word in self.events).replace(" \n ", "\n")


def _case_word_role(cases: dict[int, str], depth: int, word: str) -> Optional[str]:
    """Keep case subjects/pattern punctuation out of command and group tracking."""
    phase = cases.get(depth)
    syntax = not getattr(word, "quoted", False)
    if phase == "subject":
        cases[depth] = "header"
    elif phase == "header" and syntax and word == "in":
        cases[depth] = "pattern"
    elif phase in ("pattern", "pattern-word") and syntax and word == ")":
        cases[depth] = "body"
        return "arm"
    elif phase == "pattern" and syntax and word == "(":
        cases[depth] = "pattern-word"
    elif phase == "body" and syntax and word == ";;":
        cases[depth] = "pattern"
    elif phase == "pattern" and syntax and word == "esac":
        return "end"
    if phase in ("subject", "header", "pattern", "pattern-word"):
        return "data"
    return None


def _printed_word_role(word: str, command_position: bool) -> tuple[str, bool]:
    """Only lexical shell syntax changes compound depth or command position."""
    if getattr(word, "quoted", False):
        return "word", False
    if word in _PIPE_OPERATORS or word in _LIST_OPERATORS or word in ("(", ")"):
        role = ("pipe" if word in _PIPE_OPERATORS else "list"
                if word in _LIST_OPERATORS else "open" if word == "(" else "close")
        return role, True
    if command_position and word in _GROUP_OPEN_WORDS:
        return "open", word in _COMMAND_PREFIX_WORDS
    if command_position and word in _GROUP_CLOSE_WORDS:
        return "close", False
    return "word", command_position and word in _COMMAND_PREFIX_WORDS


def _printed_pipeline_units(command: str) -> list[_PrintedUnit]:
    """Split *command* into printed-text units: top-level lists, with compound groups and
    pipe-newline continuations kept whole. Event roles are ``word``, ``open``, ``close``,
    ``list``, ``pipe`` and ``newline`` (a line break inside a group: a list separator there).
    A heredoc body joins the unit holding its opener (nit 1); when the openers on a line cannot
    be matched to its bodies one to one, every unit the line touches gets them all."""
    from cron.lifecycle_guard import _CONTROL_CHARS, _iter_command_token_lists

    units: list[_PrintedUnit] = []
    unit = _PrintedUnit()
    depth = 0
    continued = False
    command_position = True
    cases: dict[int, str] = {}  # depth -> subject/header, pattern, or arm body
    for line, bodies in _lines_with_heredoc_bodies(command):
        touched = [unit]
        openers: list[_PrintedUnit] = []
        for tokens in _iter_command_token_lists(line):
            if unit.events and not continued:
                if not depth:
                    units.append(unit)
                    unit = _PrintedUnit()
                    touched.append(unit)
                else:
                    unit.events.append(("newline", "\n"))
                command_position = True
            for token in tokens:
                words = re.findall(r"\|&|&&|\|\||;;|[;&|()]", token) if (
                    token and not getattr(token, "quoted", False)
                    and set(token) <= _CONTROL_CHARS) else [token]
                for word in words:
                    case_role = _case_word_role(cases, depth, word)
                    if case_role == "arm":
                        unit.events.append(("arm", ")"))
                        command_position = True
                        continue
                    if case_role == "end":
                        command_position = True
                    if case_role == "data":
                        unit.events.append(("data", word))
                        continue
                    role, command_position = _printed_word_role(word, command_position)
                    if role == "close":
                        cases.pop(depth, None)
                    depth = depth + 1 if role == "open" else max(0, depth - (role == "close"))
                    if role == "open" and word == "case":
                        cases[depth] = "subject"
                    if role == "list" and not depth:
                        units.append(unit)
                        unit = _PrintedUnit()
                        touched.append(unit)
                        continued = False
                        continue
                    if role == "word" and _is_heredoc_opener(word):
                        openers.append(unit)
                    unit.events.append((role, word))
                    continued = role == "pipe"
        if len(openers) == len(bodies):
            for owner, body in zip(openers, bodies):
                owner.bodies.append(body)
        else:
            for owner in touched:
                owner.bodies.extend(bodies)
    units.append(unit)
    return [unit for unit in units if unit.events]


def _unit_feeds_nested_stdin(unit: _PrintedUnit) -> tuple[bool, bool]:
    """``(printed text, heredoc body output)``: whether a pipe in *unit* feeds a startup shell's
    stdin, and whether a heredoc-owning producer upstream of that pipe feeds it. A list operator
    or line break ends a pipe inside a group: what follows reads the group's stdin (nit 2)."""
    from cron.lifecycle_guard import _command_segment_views

    feeds = feeds_body = False
    piped = carried = own_body = False
    frames: list[list[bool]] = []  # [piped, carried, holds a body] at each open group
    words: list[str] = []
    for role, word in [*unit.events, ("list", ";")]:
        if role == "data":
            continue
        if role == "word":
            words.append(word)
            if _is_heredoc_opener(word):
                own_body = True
                for frame in frames:
                    frame[2] = True
            continue
        if words and piped and any(_nested_shell_input(view) == ("stdin", None)
                                   for view in _command_segment_views(words)):
            feeds = True
            feeds_body = feeds_body or carried
        words = []
        if role == "pipe":
            piped, carried, own_body = True, carried or own_body, False
        elif role in ("list", "newline", "arm"):
            piped, carried = (frames[-1][0], frames[-1][1]) if frames else (False, False)
            own_body = False
        elif role == "open":
            frames.append([piped, carried, False])
            own_body = False
        elif frames:  # close: the group is the pipeline's command now
            piped, carried, own_body = frames.pop()
        else:
            piped = carried = own_body = False
    return feeds, feeds_body


def _iter_nested_printed_payloads(command: str) -> Iterator[str]:
    """Walk printed text with unknown names only within a pipe feeding a startup shell.

    The ordinary printed-text approximation still scans each connected compound pipeline;
    unrelated top-level pipelines retain their session scope. Keep the ordinary walk too.
    A heredoc body is its producer's program (``python3 - <<'PY' | bash -l``), so what it may
    print is walked too when that producer is upstream of the pipe (nit 1).
    """
    from cron.lifecycle_guard_output import iter_printed_shell_payloads

    if "|" not in command:
        return  # do not tokenize large no-pipe generators a second time
    for unit in _printed_pipeline_units(command):
        feeds, feeds_body = _unit_feeds_nested_stdin(unit)
        if feeds:
            text = unit.text()
            if feeds_body and unit.bodies:
                text = "\n".join([text, *unit.bodies])
            for payload, _raw_quote in iter_printed_shell_payloads(text, shell_sink=True):
                yield payload


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
    command: str, walk_command: str, *, cwd: Optional[str], heredocs: bool = True,
) -> Iterator[tuple[Optional[str], Optional[Path]]]:
    """``(text, None)`` or ``(None, script)``: what a nested shell holding its own aliases parses
    from *command*: heredoc bodies fed to it (at any depth, read from the unmasked *command*;
    skipped unless *heredocs*), its ``-c`` payload and its script (from *walk_command*, as the
    ordinary walk reads them)."""
    if not _NESTED_SHELL_HINT_RE.search(command):
        return
    from tools.shell_heredoc import iter_heredoc_bodies

    classified = [(view, _nested_shell_input(view))
                  for view in _iter_command_segment_views(walk_command)]
    # Such a shell inside a compound command reads that command's stdin redirections and
    # heredocs (`{ su -l bob; } < f`, `( su -l bob ) <<< T`, `if …; then su -l bob; fi <<E`).
    # Where the group opens is not tracked (it may be lines earlier), nor whether the shell's own
    # redirection overrides it: with a stdin-reading one anywhere, every compound command's
    # redirections count, failing closed.
    grouped = any(found == ("stdin", None) for _view, found in classified)
    for owner, body in (iter_heredoc_bodies(command) if heredocs else ()):
        if _feeds_nested_shell(owner) or (grouped and _closes_compound(owner or "")):
            yield body, None
    if grouped:
        yield from _fed_shell_inputs(walk_command, cwd=cwd)
    for view, found in classified:
        if grouped and _compound_redirections(view):
            yield from _redirected_shell_inputs(view, cwd=cwd)
        if found is None:
            continue
        kind, value = found
        if kind == "payload" and value:
            yield value, None
        elif kind == "script" and value:
            for path in _resolved_or_nothing(value, cwd):
                yield None, path
        elif kind == "stdin":
            yield from _redirected_shell_inputs(view, cwd=cwd)


def _fed_shell_inputs(walk_command: str, *, cwd: Optional[str]) -> Iterator[tuple[None, Path]]:
    """Files fed to such a shell's stdin by a pipe or an enclosing group's redirection (``cat f |
    su -l bob``, ``cat f | { su -l bob; }``, ``{``⏎``su -l bob``⏎``} < f``): the ordinary walk
    hands a piped file only to a plain shell, and in the session's scope. A call-argument segment
    counts too (``t ( su -l bob )``): unknown alias names may make that ``(`` a subshell."""
    from cron.lifecycle_guard import _command_segment_views, _iter_segment_contexts

    for segment, _call_argument, piped in _iter_segment_contexts(walk_command):
        if piped and any(_nested_shell_input(view) == ("stdin", None)
                         for view in _command_segment_views(segment)):
            for source in piped:
                if source == "/dev/null":
                    continue  # feeds the shell nothing (and is no regular file to walk)
                for path in _resolved_or_nothing(source, cwd):
                    yield None, path


def _redirected_shell_inputs(
    view: list[str], *, cwd: Optional[str],
) -> Iterator[tuple[Optional[str], Optional[Path]]]:
    """Keep stdin redirections in the consuming shell's scope, not the session's. ``/dev/null``
    feeds the shell nothing (and is no regular file to walk)."""
    words = iter(view)
    for word in words:
        match = _REDIRECTION.match(word)
        if match is None:
            continue
        fd, operator, target = match.groups()
        target = target or next(words, "")
        if fd not in ("", "0"):
            continue
        if operator == "<<<":
            yield target, None
        elif operator in ("<", "<>") and target != "/dev/null":
            for path in _resolved_or_nothing(target, cwd):
                yield None, path


def _compound_redirections(view: list[str]) -> bool:
    """Whether *view* holds only a compound command's closers and its redirections (``} < f``,
    ``done <<< T``, and after a ``)`` just the redirections: ``< f``)."""
    return all(word in _CLOSERS for word in _split_redirections(view)[0])


def _closes_compound(owner: str) -> bool:
    """Whether a heredoc's *owner* text is a compound command's closer (``} <<'E'``, ``fi <<E``):
    the body is then the stdin of the commands that compound command holds."""
    return any(_compound_redirections(view) for view in _iter_command_segment_views(owner))
