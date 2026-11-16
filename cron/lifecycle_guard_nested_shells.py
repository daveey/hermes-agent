"""Nested shells holding aliases of their own, for the lifecycle guard (review 5894325109 nit 1).

A shell the command starts may hold aliases the session's names say nothing about: its own
startup files define them. What such a shell parses is walked with unknown alias names.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import re
import shlex
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, Optional

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
    nested: bool, executed: bool = True, working_directory: Optional[str] = None,
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
        # A mention does not cover an execution walk: only the latter
        # resolves variable sinks and fails closed on referenced scan caps.
        key = (resolved, "shell", scope, budget.integer_context)
        if working_directory is not None:
            key += (working_directory,)
        keys.setdefault(key if executed else (*key, "mention"), flag)
    return [(flag, key) for key, flag in keys.items() if key not in visited]


def collect_nested_shell_inputs(
    command: str, walk_command: str, *, cwd: Optional[str], budget: _LifecycleScanBudget,
    session_payloads: Sequence[str] = (),
) -> tuple[list[str], set[Path], Optional[str]]:
    """``(texts, resolved scripts, unresolved)`` a nested shell holding its own aliases parses in
    *command* (see ``_iter_nested_shell_inputs``). Under already unknown alias names a heredoc body needs
    no second walk (the session walk reads it in the same scope), but ``-c`` payloads, here-strings
    and scripts are still returned: `su -lc T`, `runuser --comm=T`, `su -l bob < f` hand the
    shell text or a path no ordinary walk names (review 5903452583 B1). There the session scope is
    at least as strict as the nested one (it may also unmask heredoc bodies), so a text among
    *session_payloads* (walked in the session scope anyway) is left out: walked and charged once.
    *unresolved* names the first stdin source such a shell reads that the walk cannot name
    (``su -l bob < $S``, ``< <(cmd)``, ``0<&3`` with no source): the caller fails closed."""
    texts: list[str] = []
    scripts: set[Path] = set()
    unresolved: Optional[str] = None
    unknown_names = budget.alias_names is None
    # Under unknown names the session walk reads a printed pipe, but not text a `<(…)` feeds a
    # compound command or a redirection-first command (`{ su -l bob; } < <(echo T)`).
    texts.extend(text for text in _iter_nested_printed_payloads(walk_command, pipes=not unknown_names)
                 if not (unknown_names and text in session_payloads))
    for text, script in _iter_nested_shell_inputs(command, walk_command, cwd=cwd,
                                                  heredocs=not unknown_names):
        if isinstance(text, Unresolved):
            unresolved = unresolved or str(text)
        elif script is not None:
            scripts.add(_resolve_lenient(script))
        elif text and not (unknown_names and text in session_payloads):
            texts.append(text)
    return texts, scripts, unresolved


def refuse_unresolved_stdin(
    budget: _LifecycleScanBudget, unresolved: Optional[str], *, executed: bool,
) -> bool:
    """Fail closed (named refusal) on *unresolved* from ``collect_nested_shell_inputs`` in
    executed text; a mere mention is never refused."""
    if not (unresolved and executed):
        return False
    from cron.lifecycle_guard import _refuse_unreadable

    return _refuse_unreadable(budget, Path(unresolved), f"the guard cannot resolve `{unresolved}`, "
                              "the stdin of a login/interactive shell it starts")


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


# ssh hops followed to the command that reads stdin (`ssh hub "ssh worker python3 -" < f`);
# a longer chain fails closed.
_MAX_SSH_HOPS = 4

# Past the first hop (a further ssh hop or a shell's -c payload) stdin counts as data only when
# every segment runs one of these: interpreters and filters that never hand stdin to a shell, and
# commands that do not read stdin at all. Anything else (`source /dev/stdin`, `eval bash`,
# `busybox sh`, `flock l bash`, `docker exec -i c bash`, `xargs bash`) fails closed, as every
# multi-hop chain did before it was followed (review 5914322131 B1). Not listed on purpose:
# `tar`/`sort`/`find` (options that run commands), launchers and wrappers that are not peeled.
_PYTHON_RE = re.compile(r"python(?:\d+(?:\.\d+)*)?")
_HOP_DATA_COMMANDS = frozenset({
    "cat", "tee", "wc", "head", "tail", "base64", "gzip", "gunzip", "zcat", "xz", "unxz",
    "sha256sum", "sha1sum", "md5sum", "dd", "xxd", "jq", "grep", "cut", "tr", "uniq",
    # read no stdin
    "cd", "true", ":", "echo", "printf", "export", "umask", "date", "hostname", "sleep",
})


_PYTHON_FLAG_RE = re.compile(r"-[bBdEhiIOPqRsSuvVx]+")
_PYTHON_VALUE_FLAG_RE = re.compile(r"-[bBdEhiIOPqRsSuvVx]*[WX](.*)")


def _python_reads_program_from_stdin(arguments: list[str]) -> bool:
    """Whether ``python <arguments>`` runs its program from stdin (``python3 -``, bare
    ``python3``, flags that take no code). ``-c``/``-m``/a script operand runs other code
    that may hand stdin to a shell (``python3 -c 'os.execlp("bash", "bash")'``; review
    5941029921 B1); unknown and long options fail closed too."""
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word == "-":
            return True
        if word == "--":
            return index + 1 >= len(arguments) or arguments[index + 1] == "-"
        if _PYTHON_FLAG_RE.fullmatch(word):
            index += 1
            continue
        value = _PYTHON_VALUE_FLAG_RE.fullmatch(word)
        if value is not None:
            index += 1 if value.group(1) else 2  # `-Wignore` / `-W ignore`
            continue
        return False  # -c, -m, --long options, a script operand
    return index == len(arguments)  # bare `python3` (a value flag missing its value: closed)


def _hop_data_command(name: Optional[str], arguments: list[str]) -> bool:
    if name is None:
        return False
    if _PYTHON_RE.fullmatch(name):
        return _python_reads_program_from_stdin(arguments)
    return name in _HOP_DATA_COMMANDS


def _remote_reads_stdin(
    remote: list[str], hops: int = 1, *, first_host: Optional[bool] = None,
) -> bool:
    """Whether an ssh remote command (its words, as ssh joins them) hands stdin to a shell:
    none (a login shell), unparsable, any segment running a shell that reads stdin, or a
    further hop whose own input is such a shell (another ``ssh``), or a user switch (``sudo``,
    ``su``, ``runuser``, ``doas``: not modelled, fail closed).
    A further ssh hop is followed to its remote command, so a chain ending in a data consumer
    (``python3 -``, ``cat``) reads the file as data (self-heal lifecycleguard-6d64a5ee3d48).
    Past the first hop that is an allowlist (``_HOP_DATA_COMMANDS``): any other command, a
    shell's script operand included (``bash /dev/stdin``), counts as a stdin shell."""
    if first_host is None:
        first_host = hops == 1
    if not remote:
        return True
    if hops > _MAX_SSH_HOPS:
        return True
    text = " ".join(remote)
    try:
        views = list(_iter_command_segment_views(text))
    except ValueError:
        return True
    for view in views:
        index = _executed_command_index(view)
        name = _executable_name(view[index]) if index is not None else None
        if index is not None and name in _SHELL_EXECUTABLES:
            kind, value = _shell_inputs(_split_redirections(view[index + 1:])[0], name)
            if kind == "stdin":
                return True
            # A -c payload inherits the stdin: follow it like a hop (`bash -c bash`,
            # `sh -c "exec sh"`; review 5911756789 B1), under the same depth cap.
            if kind == "payload" and _remote_reads_stdin(
                    [value or ""], hops + 1, first_host=first_host):
                return True
            if kind == "script" and hops > 1:
                return True  # `bash /dev/stdin`, `sh -- -`: not modelled past the first hop
            continue
        if _nested_shell_input(view, hops=hops + 1) is not None:
            return True  # an ssh hop feeding a shell, or a user switch's shell (`su -l`, `sudo -i`)
        if name in ("sudo", "su", "runuser", "doas"):
            return True  # a user switch on the remote side: fail closed rather than model it
        # A first-host -c wrapper must not turn plain `git apply` patch data into
        # shell code. Keep all unknown payload commands conservative: treating every
        # payload as a bare first hop would also allow `source /dev/stdin`. Only this
        # exact argv is proven here; options/aliases/other subcommands stay closed.
        if (first_host and index is not None and name == "git"
                and _split_redirections(view[index + 1:])[0] == ["apply"]):
            continue
        if hops > 1 and index is not None and name != "ssh" and not _hop_data_command(
                name, _split_redirections(view[index + 1:])[0]):
            return True  # past the first hop only known data consumers read stdin as data
    return False


def _nested_shell_input(view: list[str], hops: int = 1) -> Optional[tuple[str, Optional[str]]]:
    """``_shell_inputs`` of the nested shell *view* starts when that shell holds aliases of its
    own (see above), else ``None``. For ssh only stdin counts: a remote ``-c`` or script names
    remote text and paths. Command wrappers are peeled one at a time (``env su -l bob``,
    ``nice sudo -i``, ``sudo su - bob``): each may itself be the shell launcher."""
    index = _command_token_index(view)
    login_exec = False
    # Every peel advances index: input length bounds the walk without hiding a shell
    # at (or beyond) an arbitrary wrapper-count limit.
    while index is not None and index < len(view):
        name = _executable_name(view[index])
        found = _launcher_shell_input(view, index, name)
        if found is not None:
            return found
        # `exec -l` anywhere in the chain fails closed: its `-` reaches the next argv[0].
        login_exec = login_exec or (name == "exec" and "-l" in itertools.takewhile(
            lambda word: word.startswith("-"), view[index + 1:]))
        peeled = _peel_transparent_prefixes(view, index, limit=1)
        if peeled == index:
            return _shell_input(view, index, name, login_exec, hops)
        index = peeled
    return None


def _launcher_shell_input(
    view: list[str], index: int, name: str,
) -> Optional[tuple[str, Optional[str]]]:
    """What a su/runuser/doas/sudo at *index* hands its own shell, or ``None`` when it runs the
    command after its options instead (direct-user runuser, plain ``sudo``/``doas``)."""
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
    return None


def _shell_input(
    view: list[str], index: int, name: str, login_exec: bool, hops: int = 1,
) -> Optional[tuple[str, Optional[str]]]:
    """``_nested_shell_input`` for the unwrapped command *name* at *index*: ssh or a shell
    (*hops*: the ssh hop depth, see ``_remote_reads_stdin``)."""
    arguments = _split_redirections(view[index + 1:])[0]
    if name == "ssh":
        from tools.shell_heredoc import _SSH_VALUE_OPTIONS

        _options, host = _option_words(arguments, frozenset(_SSH_VALUE_OPTIONS))
        # OpenSSH resumes getopt after the destination (`ssh h -p 22 sh -s`, `ssh h -T`), so
        # options there are ssh's own and the remote command starts at the next operand.
        remote = arguments[host + 1:]
        _options, command = _option_words(remote, frozenset(_SSH_VALUE_OPTIONS))
        return ("stdin", None) if _remote_reads_stdin(remote[command:], hops) else None
    if name not in _SHELL_EXECUTABLES:
        return None
    options_end = _shell_options_end(arguments, name)[0]
    # ENV is read by interactive POSIX shells (already covered by -i); BASH_ENV
    # additionally lets a plain non-interactive bash define and enable aliases.
    startup_env = name == "bash" and any(word.startswith("BASH_ENV=") for word in view[:index])
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
    function_name = False  # the previous word was the `function` keyword
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
                    after_function = function_name
                    function_name = (command_position and word == "function"
                                     and not getattr(word, "quoted", False))
                    role, command_position = _printed_word_role(word, command_position)
                    # `function NAME {`: the word after the name is a compound command.
                    command_position = command_position or after_function
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


def _reads_nested_stdin(words: list[str]) -> bool:
    """Whether the command *words* starts a shell holding its own aliases that reads stdin."""
    from cron.lifecycle_guard import _command_segment_views

    return any(_nested_shell_input(view) == ("stdin", None) for view in _command_segment_views(words))


def pipe_feeds_consumer(
    command: str, reads: Callable[[list[str]], bool], *, output_processes: bool = False,
) -> bool:
    """Whether a pipe in *command* feeds a command whose words satisfy *reads*, including a
    command inside a compound command the pipe feeds (``| { echo x; bash; }``, ``| ( :; bash )``,
    ``| while read l; do bash; done``): every command there reads the compound's stdin.
    With *output_processes*, also count the implicit pipe feeding each ``>(...)``."""
    return ("|" in command or output_processes) and any(
        _unit_feeds_nested_stdin(unit, reads, output_processes=output_processes)[0]
        for unit in _printed_pipeline_units(command))


def _unit_feeds_nested_stdin(
    unit: _PrintedUnit, reads: Callable[[list[str]], bool] = _reads_nested_stdin,
    *, output_processes: bool = False,
) -> tuple[bool, bool]:
    """Preserve the two-field consumer contract for ordinary pipe-flow callers."""
    feeds, body, _end = _unit_nested_stdin_flow(unit, reads, output_processes=output_processes)
    return feeds, body


def _unit_nested_stdin_flow(
    unit: _PrintedUnit, reads: Callable[[list[str]], bool] = _reads_nested_stdin,
    *, output_processes: bool = False,
) -> tuple[bool, bool, int]:
    """Return ``(printed text, heredoc body output, upstream end)`` for a pipe feeding *reads*.
    The last field ends the last consumer's command; function producers are resolved only
    before that boundary. The second field tracks a heredoc-owning upstream producer. A list
    operator or line break ends a pipe inside a group:
    what follows reads the group's stdin (nit 2)."""
    feeds = feeds_body = False
    upstream_end = 0
    piped = carried = own_body = False
    frames: list[list[bool]] = []  # [piped, carried, holds a body] at each open group
    suspended: dict[int, tuple[bool, bool, bool]] = {}
    words: list[str] = []
    for index, (role, word) in enumerate([*unit.events, ("list", ";")]):
        if role == "data":
            continue
        if role == "word":
            words.append(word)
            if _is_heredoc_opener(word):
                own_body = True
                for frame in frames:
                    frame[2] = True
            continue
        if words and piped and reads(words):
            feeds = True
            feeds_body = feeds_body or carried
            upstream_end = index
        process = (output_processes and role == "open" and word == "("
                   and bool(words) and words[-1] == ">" and _syntax_word(words[-1]))
        words = []
        if role == "pipe":
            piped, carried, own_body = True, carried or own_body, False
        elif role in ("list", "newline", "arm"):
            piped, carried = (frames[-1][0], frames[-1][1]) if frames else (False, False)
            own_body = False
        elif role == "open":
            if process:
                # The substitution receives a new stdin, inherited by its lists
                # and nested groups. Its enclosing command keeps its own stdin.
                suspended[len(frames)] = (piped, carried, own_body)
                piped, carried = True, carried or own_body
            frames.append([piped, carried, False])
            own_body = False
        elif frames:  # close: the group is the pipeline's command now
            piped, carried, own_body = frames.pop()
            if len(frames) in suspended:
                piped, carried, own_body = suspended.pop(len(frames))
        else:
            piped = carried = own_body = False
    return feeds, feeds_body, upstream_end


def _iter_nested_printed_payloads(command: str, *, pipes: bool = True) -> Iterator[str]:
    """Walk printed text with unknown names only within a pipe feeding a startup shell, or a
    process substitution read as its stdin (``su -l bob < <(echo T)``, as ``echo T | su -l bob``).

    The ordinary printed-text approximation still scans each connected compound pipeline;
    unrelated top-level pipelines retain their session scope. Keep the ordinary walk too.
    A heredoc body is its producer's program (``python3 - <<'PY' | bash -l``), so what it may
    print is walked too when that producer is upstream of the pipe (nit 1).
    """
    from cron.lifecycle_guard_output import iter_printed_shell_payloads
    from cron.lifecycle_guard_printed_functions import FunctionProducers

    piped = pipes and "|" in command
    substituted = _STDIN_PROCESS_OPENER.search(command) is not None
    if not (piped or substituted):
        return  # do not tokenize large generators with neither feed a second time
    units = _printed_pipeline_units(command)
    functions = FunctionProducers(units)
    for unit in units:
        texts = _stdin_substitution_texts(unit) if substituted else []
        feeds, feeds_body, upstream_end = _unit_nested_stdin_flow(unit) if piped else (False, False, 0)
        if feeds:
            text = unit.text()
            if feeds_body and unit.bodies:
                text = "\n".join([text, *unit.bodies])
            texts.append(text)
            # Resolve only calls upstream of the pipe-fed startup shell.
            texts.extend(functions.called_texts(unit.events[:upstream_end]))
        for text in texts:
            for payload, _raw_quote in iter_printed_shell_payloads(text, shell_sink=True):
                yield payload


# Only a prefilter: tokenization below removes active continuations in their quote frame.
_STDIN_PROCESS_OPENER = re.compile(r"<(?:\\\n)*\(")
_STDIN_OPERATORS = frozenset({"<", "0<", "<>", "0<>"})
_SUBSTITUTION_WORD = "\x00<(…)"  # what a `<(…)` stands in for in a command's words


def _syntax_word(word: str) -> bool:
    return not getattr(word, "quoted", False)


def _split_substituted_commands(
    events: list[tuple[str, str]], commands: list[tuple[list[str], list[list[tuple[str, str]]]]],
) -> None:
    """Append ``(words, bodies)`` for each simple command in *events*: *bodies* are the events of
    each ``<(…)`` read as its stdin (``< <(…)``, ``0< <(…)``), one placeholder word in *words*.
    Commands inside those bodies are appended too."""
    words: list[str] = []
    bodies: list[list[tuple[str, str]]] = []
    index = 0
    while index < len(events):
        role, word = events[index]
        if (role == "word" and word == "<" and _syntax_word(word) and words
                and words[-1] in _STDIN_OPERATORS and _syntax_word(words[-1])
                and index + 1 < len(events) and events[index + 1] == ("open", "(")):
            depth = 0
            for end in range(index + 1, len(events)):
                depth += {"open": 1, "close": -1}.get(events[end][0], 0)
                if not depth:
                    break
            else:
                end = len(events)  # unterminated: the rest is the body (fail closed)
            body = events[index + 2:end]
            bodies.append(body)
            _split_substituted_commands(body, commands)
            words.append(_SUBSTITUTION_WORD)
            index = end + 1
            continue
        if role == "word":
            words.append(word)
        elif role != "data":
            if words:
                commands.append((words, bodies))
            words, bodies = [], []
        index += 1
    if words:
        commands.append((words, bodies))


def _stdin_substitution_texts(unit: _PrintedUnit) -> list[str]:
    """Printed text of each ``<(…)`` fed to a startup shell's stdin (``su -l bob < <(echo T)``),
    as ``echo T | su -l bob`` is walked. A compound command's closer (``} < <(…)``) counts when any
    command in *unit* is such a shell: its own redirections are not tracked (fail closed, as for
    a file). A heredoc body in the substitution is its producer's program, so it is walked too."""
    from cron.lifecycle_guard import _command_segment_views

    commands: list[tuple[list[str], list[list[tuple[str, str]]]]] = []
    _split_substituted_commands(unit.events, commands)
    reads = [any(_nested_shell_input(view) == ("stdin", None)
                 for view in _command_segment_views(words)) for words, _bodies in commands]
    texts: list[str] = []
    for (words, bodies), feeds in zip(commands, reads):
        if not bodies or not (feeds or (any(reads) and _compound_redirections(words))):
            continue
        for body in bodies:
            producer = _PrintedUnit()
            producer.events = body
            text = producer.text()
            if unit.bodies and any(role == "word" and _is_heredoc_opener(word)
                                   for role, word in body):
                text = "\n".join([text, *unit.bodies])
            texts.append(text)
    return texts


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

    from cron.lifecycle_guard import _command_segment_views, _iter_segment_contexts

    # Contextual segments resolve process-substitution file feeds into redirections.
    # Flat command views retain only the substitution placeholder, losing that file.
    classified = [(view, _nested_shell_input(view))
                  for segment, _call_argument, _piped in _iter_segment_contexts(walk_command)
                  for view in _command_segment_views(segment)]
    # Such a shell inside a compound command reads that command's stdin redirections and
    # heredocs (`{ su -l bob; } < f`, `( su -l bob ) <<< T`, `if …; then su -l bob; fi <<E`).
    # Where the group opens is not tracked (it may be lines earlier), nor whether the shell's own
    # redirection overrides it: with a stdin-reading one anywhere, every compound command's
    # redirections count, failing closed.
    grouped = any(found == ("stdin", None) for _view, found in classified)
    # So does a function body's (`f() { su -l bob; }; f < f`): a call to any function the
    # command defines counts, whichever body holds the shell (`g() { f; }; g < f`).
    functions = _defined_functions(walk_command) if grouped else frozenset()
    for owner, body in (iter_heredoc_bodies(command) if heredocs else ()):
        if _feeds_nested_shell(owner) or (grouped and _reaches_group(owner or "", functions)):
            yield body, None
    if grouped:
        yield from _fed_shell_inputs(walk_command, cwd=cwd)
    from cron.lifecycle_guard_stdin_provenance import source_contexts

    # Provenance is only consumed by stdin readers; do not re-lex ordinary scripts.
    contexts = (source_contexts(walk_command, [view for view, _found in classified])
                if grouped else (_StdinSources({}, {}) for _ in classified))
    for (view, found), local_sources in zip(classified, contexts):
        if grouped and (_compound_redirections(view) or _calls_function(view, functions)):
            yield from _redirected_shell_inputs(view, cwd=cwd, sources=local_sources)
        if found is None:
            continue
        kind, value = found
        if kind == "payload" and value:
            yield value, None
        elif kind == "script" and value:
            for path in _resolved_or_nothing(value, cwd):
                yield None, path
        elif kind == "stdin":
            yield from _redirected_shell_inputs(view, cwd=cwd, sources=local_sources)


class _StdinSources(NamedTuple):
    """Proven reaching descriptor sources and variable values before one command.

    Unknown values carry an unresolvable ``$`` sentinel, not an empty safe union.
    """

    fds: dict[str, list[tuple[str, str]]]
    variables: dict[str, list[str]]


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


def _redirections(view: list[str]) -> Iterator[tuple[str, str, str]]:
    """``(fd, operator, target)`` of each redirection in *view* (a separate target included)."""
    words = iter(view)
    for word in words:
        match = _REDIRECTION.match(word)
        if match is not None:
            fd, operator, target = match.groups()
            yield fd, operator, target or next(words, "")


_ASSIGNMENT_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", re.DOTALL)
_DECLARATION_WORDS = frozenset({"export", "readonly", "declare", "typeset", "local"})


def _assigned_variables(views: Iterator[list[str]]) -> dict[str, list[str]]:
    """``{name: [value, …]}`` for every standalone assignment in the command (``S=f``,
    ``export S=f``; not a prefix assignment, ``S=f cmd``, which no redirection expands). Which
    value a later expansion sees depends on order and scope the walk does not track, so each
    counts."""
    variables: dict[str, list[str]] = {}
    for view in views:
        words = view[1:] if view and view[0] in _DECLARATION_WORDS else view
        matches = [_ASSIGNMENT_RE.fullmatch(word) for word in words if not word.startswith("-")]
        if matches and all(matches):
            for match in filter(None, matches):
                variables.setdefault(match.group(1), []).append(match.group(2))
    return variables


_VARIABLE_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
_MAX_EXPANSIONS = 16  # words and substitutions per target: past it, fail closed


def _expanded_targets(target: str, variables: dict[str, list[str]]) -> Optional[list[str]]:
    """Each word *target* may expand to, its ``$NAME``/``${NAME}`` taken from the command's own
    assignments, else from this process's environment (the local terminal inherits ``$TMPDIR``,
    ``$HERMES_KANBAN_WORKSPACE``). ``None`` when any part cannot be named (an unknown variable,
    ``$1``, a substitution, a glob, a self-referencing value)."""
    words: list[str] = []
    pending = [target]
    steps = 0
    while pending:
        word = pending.pop()
        match = _VARIABLE_RE.search(word)
        if match is None:
            if _unresolvable(word):
                return None
            words.append(word)
            continue
        name = match.group(1) or match.group(2)
        values = variables.get(name) or ([os.environ[name]] if name in os.environ else [])
        steps += len(values)
        # Expansion results are data, never fresh shell syntax. Our path walker
        # cannot preserve quoting or literal tildes through path resolution.
        if not values or steps > _MAX_EXPANSIONS or any(
                _unresolvable(value) or "~" in value for value in values):
            return None
        pending.extend(word[:match.start()] + value + word[match.end():] for value in values)
    return words


_FD_PATH_RE = re.compile(r"/(?:dev|proc/self)/fd/(\d+)")


def _redirected_shell_inputs(
    view: list[str], *, cwd: Optional[str], sources: Optional[_StdinSources] = None,
) -> Iterator[tuple[Optional[str], Optional[Path]]]:
    """Keep stdin redirections in the consuming shell's scope, not the session's. ``/dev/null``
    feeds the shell nothing (and is no regular file to walk). A stdin duplicated from another
    descriptor (``0<&3``, ``< /dev/fd/3``) reads that descriptor's sources; a ``$NAME`` target
    expands from the command's assignments or the environment (*sources*)."""
    from cron.lifecycle_guard_stdin_provenance import descriptor_definition

    inherited, variables = sources or _StdinSources({}, {})
    fds = dict(inherited)
    for fd, operator, target in _redirections(view):
        if fd not in ("", "0"):
            fds[fd] = descriptor_definition(operator, target, fds, variables)
            continue
        duplicated = _FD_PATH_RE.fullmatch(target) if operator in ("<", "<>") else None
        if operator == "<&" and target in ("-", "0"):
            continue  # stdin closed, or left as it is
        if operator == "<&" or (duplicated and duplicated.group(1) != "0"):
            source = duplicated.group(1) if duplicated else target
            dups = fds.get(source, ())
            if not dups or any(dup_operator not in ("<", "<>", "<<<")
                               for dup_operator, _dup_target in dups):
                yield Unresolved(f"{operator}{target}"), None
            for dup_operator, dup_target in dups:
                yield from _stdin_source(dup_operator, dup_target, cwd, variables)
        else:
            yield from _stdin_source(operator, target, cwd, variables)


def _stdin_source(
    operator: str, target: str, cwd: Optional[str], variables: dict[str, list[str]],
) -> Iterator[tuple[Optional[str], Optional[Path]]]:
    """What one input redirection (``< f``, ``<> f``, ``<<< T``) hands the shell reading it; an
    ``Unresolved`` text when the walk cannot name it (see ``_expanded_targets``)."""
    if operator == "<<<":
        yield target, None
        return
    if operator not in ("<", "<>"):
        return
    expanded = _expanded_targets(target, variables)
    if expanded is None:
        yield Unresolved(target), None
    for word in expanded or ():
        if word != "/dev/null":
            for path in _resolved_or_nothing(word, cwd):
                yield None, path


class Unresolved(str):
    """A stdin source a nested shell reads whose content the walk cannot name: a variable, a
    command or process substitution, a glob, a heredoc or unknown descriptor behind ``0<&N``."""


# The tokenizer splits `<(cmd)` and `$(cmd)` at the parenthesis: the target is then `<` or `$`.
_UNRESOLVABLE_TARGET_RE = re.compile(r"[$`*?\[]|^[<>]|^$")


def _unresolvable(target: str) -> bool:
    return _UNRESOLVABLE_TARGET_RE.search(target) is not None


def _compound_redirections(view: list[str]) -> bool:
    """Whether *view* holds only a compound command's closers and its redirections (``} < f``,
    ``done <<< T``, and after a ``)`` just the redirections: ``< f``)."""
    return all(word in _CLOSERS for word in _split_redirections(view)[0])


def _closes_compound(owner: str) -> bool:
    """Whether a heredoc's *owner* text is a compound command's closer (``} <<'E'``, ``fi <<E``):
    the body is then the stdin of the commands that compound command holds."""
    return any(_compound_redirections(view) for view in _iter_command_segment_views(owner))


# `NAME ()` (bash, POSIX) or `function NAME` (bash, ksh), the latter with or without `()`.
_FUNCTION_DEFINITION_RE = re.compile(
    r"(?:^|[\s;&|(){}])(?:function\s+([^\s;&|(){}<>]+)|([A-Za-z_][\w.:-]*)\s*\(\s*\))")


def _defined_functions(command: str) -> frozenset[str]:
    """Names of the shell functions *command* may define."""
    return frozenset(name for match in _FUNCTION_DEFINITION_RE.finditer(command)
                     for name in match.groups() if name)


def _calls_function(view: list[str], functions: frozenset[str]) -> bool:
    """Whether *view* runs one of *functions*."""
    index = _command_token_index(view) if functions else None
    return index is not None and view[index] in functions


def _reaches_group(owner: str, functions: frozenset[str]) -> bool:
    """Whether a heredoc's *owner* text hands the body to a compound command or function call."""
    return any(_compound_redirections(view) or (
                   _calls_function(view, functions)
                   and any(operator in {"<<", "<<-"} for _fd, operator, _target in _redirections(view)))
               for view in _iter_command_segment_views(owner))
