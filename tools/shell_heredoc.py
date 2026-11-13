"""Conservative heredoc masking for shell-command scanners ('&' guard, blocked-command checks,
cron lifecycle_guard) that false-positive on heredoc *bodies*. Stripping every body is unsafe the
other way (a fake ``<<`` in quotes can swallow an operator; unquoted bodies expand; ``bash <<'EOF'``
executes), so a body is masked ONLY when every delimiter is quoted, every heredoc has an exact
terminator line, the owning simple command (after transparent wrappers such as ``timeout 20`` are
peeled; optionally the first command of a ``name() {`` body) is an allowlisted non-shell
interpreter, and no list operator follows the heredoc. Otherwise the command is returned
untouched: a false positive is acceptable, hiding shell syntax from a guard is not. For the
lifecycle guard's executed-path walk only (``nested=True``), data-consumer heredocs nested in the
quoted body of an outer heredoc that a shell reads verbatim (``ssh h 'bash -s' <<'R'``) are judged
by the same rules; text scanners still see those nested bodies.
Masked bodies keep their newline count (re.MULTILINE)."""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterator

from tools.shell_arith_sink import DATE_PRODUCER_BODY, substitution_in_arithmetic

# Non-shell interpreters whose quoted heredoc bodies are data for THAT interpreter, judged on the
# command the owner actually runs: VAR=... assignments and stdin-passing wrappers (``timeout 20``,
# ``nice``, ``env VAR=1``, ``command``) are peeled, a path prefix is allowed, positional arguments
# may follow. Narrow on purpose: unmatched = visible.
_INERT_HEREDOC_CONSUMER_RE = re.compile(r"^(?:python(?:3(?:\.\d+)*)?|osascript|cat)$", re.IGNORECASE)
# Wrappers that run their operand with the heredoc on its stdin, each with the ONLY options it may
# carry: (options taking the next word, regex of self-contained option words, leading operands).
# Any other option (``env -S'bash -s'``, ``exec -a python3 bash``, ``sudo -s``/``-D dir``) or any
# other wrapper (``su``/``runuser``: the operand is a USER and a shell reads stdin) = visible.
_INERT_HEREDOC_WRAPPERS: dict[str, tuple[frozenset[str], re.Pattern[str], int]] = {
    name: (frozenset(values), re.compile(flags), operands)
    for name, values, flags, operands in (
        ("timeout", {"-s", "-k", "--signal", "--kill-after"},
         r"--preserve-status|--foreground|-v|--verbose|--(?:signal|kill-after)=\S+|-[sk]\S+", 1),
        ("nice", {"-n", "--adjustment"}, r"-n\S+|--adjustment=\S+|-\d+", 0),
        ("ionice", {"-c", "-n", "--class", "--classdata"},
         r"-t|--ignore|-[cn]\d+|--class(?:data)?=\S+", 0),
        ("nohup", set(), r"(?!)", 0),
        ("setsid", set(), r"-f|--fork|-w|--wait", 0),
        ("stdbuf", {"-i", "-o", "-e", "--input", "--output", "--error"},
         r"-[ioe]\S+|--(?:input|output|error)=\S+", 0),
        ("env", {"-u", "--unset", "-C", "--chdir"},
         r"-|-i|--ignore-environment|-0|--null|-u\S+|--unset=\S+|-C\S+|--chdir=\S+", 0),
        ("command", set(), r"-p", 0),
        ("exec", set(), r"(?!)", 0),
        ("eatmydata", set(), r"(?!)", 0),
        ("sudo", {"-u", "-g", "-p", "--user", "--group", "--prompt"},
         r"-[EHnPk]+|--preserve-env(?:=\S+)?|--set-home|--non-interactive|--preserve-groups"
         r"|--(?:user|group|prompt)=\S+|-[ugp]\S+", 0),
        ("doas", {"-u"}, r"-n|-u\S+", 0),
    )
}
# Wrappers whose operand list may start with VAR=value words.
_ASSIGNING_WRAPPERS = frozenset({"env", "sudo"})
# Outer owners whose quoted heredoc body a SHELL parses verbatim: only their bodies' nested
# heredocs may be judged as shell text. A POSIX shell reading its script from stdin.
# zsh is excluded: it sources ``$ZDOTDIR/.zshenv`` (default ``$HOME``) on every start, even a
# non-interactive ``zsh -s``, and that file can rewrite the stdin script before zsh reads it.
_STDIN_SHELLS = frozenset({"bash", "sh", "dash", "ksh"})
# Flags that keep the shell non-interactive and non-login. ``-i``/``-l``/``--login`` source
# startup files located by ``$HOME`` and run ``PROMPT_COMMAND``/``PS1`` before each line, which can
# rewrite the stdin script; ``-H`` (history expansion) and ``-k`` (keyword assignments) change how
# its lines are read. No word containing them, alone or combined, is a verbatim stdin shell.
_SHELL_FLAG_RE = re.compile(r"[-+][abefhmnpstuvxBCEPT]+|--(?:noprofile|norc|posix)")
# ``-o``/``+o`` operands with the same effect as the letters above (plus pipefail/posix).
# ``history``, ``histexpand``, ``keyword``, ``interactive-comments``, ``emacs``/``vi`` and the
# rest are excluded: they change how the stdin script's lines are read.
_SHELL_SET_OPTIONS = frozenset({
    "allexport", "notify", "errexit", "noglob", "hashall", "monitor", "noexec", "privileged",
    "onecmd", "nounset", "verbose", "xtrace", "braceexpand", "noclobber", "errtrace", "physical",
    "functrace", "pipefail", "posix",
})
_SSH_VALUE_OPTIONS = frozenset("BbcDEeFIiJLlmOoPpQRSWw")
_SSH_FLAG_OPTIONS = frozenset("46AaCgKkMnqTtVvXxYy")
_PURE_EXPANSION_RE = re.compile(r"^\$(?:\{[A-Za-z_]\w*(?::?[-=+?][^}]*)?\}|[A-Za-z_]\w*)$")
_REMOTE_SHELL_UNSAFE = ("|", ";", "&", "<", ">", "(", ")", "`", "$(", "\n")
_ASSIGNMENT_WORD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# ``name() {`` / ``function name {`` opening a function body on the heredoc's own line.
_FUNCTION_HEADER_RE = re.compile(
    r"\s*(?:function\s+[A-Za-z_][\w.:-]*\s*(?:\(\s*\))?|[A-Za-z_][\w.:-]*\s*\(\s*\))\s*\{\s+")
# Nested heredocs inside a quoted outer body are classified recursively, this deep at most.
_MAX_NESTED_HEREDOC_DEPTH = 4
# A command that (re)defines a consumer, wrapper or outer-shell name as a function or alias
# anywhere (``python3() { bash; }``) may run a shell where the peeled word says "python": mask nothing.
_SHADOWABLE_NAMES = (r"(?:python[\d.]*|osascript|cat|timeout|nice|ionice|nohup|setsid|stdbuf|env|command"
                     r"|exec|eatmydata|sudo|doas|ssh|bash|sh|dash|zsh|ksh|echo|printf|set|tail|cut|ls)")
_CONSUMER_SHADOW_RE = re.compile(
    rf"(?:^|[\s;&|(){{}}])(?:(?:function\s+)?{_SHADOWABLE_NAMES}\s*\(\s*\)"
    rf"|function\s+{_SHADOWABLE_NAMES}\s+\{{|alias\s+{_SHADOWABLE_NAMES}=)",
    re.IGNORECASE)
_SHADOWABLE_NAME_RE = re.compile(_SHADOWABLE_NAMES, re.IGNORECASE)


def shadows_heredoc_consumer(name: str) -> bool:
    """Whether an alias or function called *name* may rename a heredoc consumer, stdin wrapper or
    shell the masking trusts (the names ``_CONSUMER_SHADOW_RE`` guards in the command text)."""
    return _SHADOWABLE_NAME_RE.fullmatch(name) is not None


def _peel_stdin_wrappers(words: list[str], index: int) -> int | None:
    """Index of the command a chain of stdin-passing wrappers runs, or None when any wrapper or
    option outside ``_INERT_HEREDOC_WRAPPERS`` appears (then the peeled word is not trusted)."""
    from cron.lifecycle_guard import _MAX_PREFIX_PEELS, _TRANSPARENT_COMMAND_PREFIXES

    for _ in range(_MAX_PREFIX_PEELS):
        if index >= len(words):
            return None
        name = words[index].rsplit("/", 1)[-1]
        if name not in _TRANSPARENT_COMMAND_PREFIXES:
            return index
        if name not in _INERT_HEREDOC_WRAPPERS:
            return None
        value_options, flag_re, operands = _INERT_HEREDOC_WRAPPERS[name]
        index += 1
        while index < len(words):
            word = words[index]
            if word == "--":
                index += 1
                break
            if word in value_options:
                index += 2
            elif word.startswith("-"):
                if not flag_re.fullmatch(word):
                    return None
                index += 1
            elif name in _ASSIGNING_WRAPPERS and _ASSIGNMENT_WORD_RE.match(word):
                index += 1
            else:
                break
        for _ in range(operands):
            if index >= len(words) or words[index].startswith("-"):
                return None
            index += 1
    return None


def _peeled_command(words: list[str]) -> tuple[int, str] | None:
    """``(index, basename)`` of the command *words* runs after assignments and stdin wrappers."""
    from cron.lifecycle_guard import _peel_transparent_prefixes

    index = next((i for i, word in enumerate(words) if not _ASSIGNMENT_WORD_RE.match(word)), None)
    if index is None:
        return None
    peeled = _peel_stdin_wrappers(words, index)
    # The strict walk must land exactly where the lifecycle guard's own peeling lands.
    if peeled is None or peeled >= len(words) or peeled != _peel_transparent_prefixes(words, index):
        return None
    # Startup hooks, option imports, command lookup and exported functions can
    # replace the apparent consumer or rewrite the shell's source stream. Check
    # every prefix word, including assignments introduced by env/sudo.
    if any(_reserved_assignment(word) for word in words[:peeled]):
        return None
    return peeled, words[peeled].rsplit("/", 1)[-1]


def _reserved_assignment(word: str) -> bool:
    """Environment words that invalidate interpreter/stream provenance."""
    name, separator, _value = word.partition("=")
    # HOME/ZDOTDIR locate startup files; PROMPT_COMMAND/PSn run code before each line.
    return bool(separator) and (name in {
        "PATH", "IFS", "ENV", "CDPATH", "GLOBIGNORE", "SHELLOPTS", "HOME", "ZDOTDIR",
        "PROMPT_COMMAND", "HISTFILE", "INPUTRC",
    } or name.startswith("BASH") or re.fullmatch(r"PS\d", name) is not None)


def _is_stdin_shell(words: list[str]) -> bool:
    """Whether *words* run a POSIX shell that reads its script from stdin (``bash``, ``bash -s
    args``, ``T=1 sh -e``), never ``-c`` or a script-file operand."""
    peeled = _peeled_command(words)
    if peeled is None or peeled[1] not in _STDIN_SHELLS:
        return False
    # Bash keeps parsing options after `-s` until the first non-option word or `--`, so every
    # option word up to there is validated (`bash -s -i`, `bash -s -o history` are not verbatim).
    reads_stdin = False
    arguments = iter(words[peeled[0] + 1:])
    for word in arguments:
        if word in ("-o", "+o"):
            if next(arguments, None) not in _SHELL_SET_OPTIONS:
                return False  # `-o history`/`histexpand`/`keyword`/…, or a bare `-o` listing
        elif word == "--":
            return reads_stdin  # `bash -s -- args`; `bash -- file` runs the file
        elif word[:1] not in ("-", "+"):
            return reads_stdin  # positional parameters after `-s`, else a script-file operand
        elif not _SHELL_FLAG_RE.fullmatch(word):
            return False  # `-c`, `-i`, `-l`, `-H`, `-O shopt`, …: stdin is not run verbatim
        elif word.startswith("-") and not word.startswith("--") and "s" in word:
            reads_stdin = True
    return True


def _ssh_runs_stdin_shell(words: list[str]) -> bool:
    """Whether *words* are ``ssh [opts] host 'remote command'`` whose remote command is a plain
    stdin shell (``T=$T bash -s``): ssh hands the heredoc to it unchanged."""
    peeled = _peeled_command(words)
    if peeled is None or peeled[1] != "ssh":
        return False
    index = peeled[0] + 1
    while index < len(words) and (
            words[index].startswith("-") or _PURE_EXPANSION_RE.match(words[index])):
        word = words[index]
        index += 1
        if not word.startswith("-"):
            continue  # `${XOPTS:-}`: more ssh options; the remote command is still checked below
        for position, letter in enumerate(word[1:], 1):
            if letter in _SSH_VALUE_OPTIONS:
                index += position == len(word) - 1  # `-o X` consumes the next word, `-oX` not
                break
            if letter not in _SSH_FLAG_OPTIONS:
                return False
    remote = " ".join(words[index + 1:])  # ssh joins the words after the host with spaces
    if not remote.strip() or any(marker in remote for marker in _REMOTE_SHELL_UNSAFE):
        return False
    try:
        return _is_stdin_shell(shlex.split(remote))
    except ValueError:
        return False


def _literal_assignment(unit: str) -> tuple[str, str] | None:
    """Recognize one literal scalar assignment, never shell state or expansions.

    Even quoted expansion markers are rejected: callers split the value as a command,
    not as a second shell program. No attempt is made to evaluate shell syntax.
    """
    match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", unit)
    if (not match or match[2][:1].isspace()
            or any(char in match[2] for char in "$`\\\n;|&<>(){}~*?[]")):
        return None
    name = match[1]
    if _reserved_assignment(unit):
        return None
    try:
        words = shlex.split(match[2])
    except ValueError:
        return None
    return (name, words[0]) if len(words) == 1 else None


def _variable_value(command: str, name: str, before: int) -> str | None:
    """Resolve one literal binding in a wholly declarative prefix.

    A mutator blacklist is insufficient: positional parameters, quoted names and
    arbitrary functions can synthesize eval/printf/read. Unknown commands invalidate
    provenance, even before the assignment. Ambiguous scopes and writes fail closed.
    """
    found: list[str] = []
    cursor = 0
    while cursor < before:
        end, specs, unknown, _post, _owner = _scan_heredoc_command_unit(command, cursor)
        if end > before or specs or unknown:
            return None
        unit = command[cursor:end].strip()
        cursor = end + 1
        if not unit or unit.startswith("#"):
            continue
        assignment = _literal_assignment(unit)
        if assignment is None:
            return None
        if assignment[0] == name:
            found.append(assignment[1])
    return found[0] if len(found) == 1 else None


def _owner_feeds_shell(command: str, command_start: int, owner_start: int, specs: list) -> bool:
    """Whether the owner of the heredoc opened in ``command[command_start:]`` hands its single,
    tab-preserving body unchanged to a shell: ``bash -s``, ``ssh h 'bash -s'``, or ``$SSH h
    'bash -s'`` with ``SSH`` assigned earlier in *command*. Anything else (``sed … <<'R' | bash``,
    an unknown ``$CMD``, ``<<-`` stripping the inner terminator's tabs) may change the body's
    lines before a shell parses them, so its nested heredocs are not judged."""
    if len(specs) != 1 or specs[0][1]:
        return False
    lexer = shlex.shlex(command[owner_start:], posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    words: list[str] = []
    try:
        for token in lexer:
            if token == "<<":
                break
            if token[:1] in ("<", ">", "|", ";", "&", "(", ")") or "\n" in token:
                return False
            words.append(token)
        else:
            return False
    except ValueError:
        return False
    if any("$(" in word or "`" in word for word in words):
        return False
    first = next((i for i, word in enumerate(words) if not _ASSIGNMENT_WORD_RE.match(word)), None)
    if first is not None and _PURE_EXPANSION_RE.match(words[first]):
        # The binding proof ends at command_start, not owner_start. A same-line
        # list prefix can change that binding (including via read/eval/functions).
        if command[command_start:owner_start].strip():
            return False
        value = _variable_value(command, words[first].strip("${}"), command_start)
        if not value:
            return False
        words[first:first + 1] = value.split()
    # Unknown expansions can inject SSH options, a host, or remote shell syntax.
    # Resolving the first word is not proof of the remaining argument values.
    if any("$" in word or "`" in word for word in words):
        return False
    return _is_stdin_shell(words) or _ssh_runs_stdin_shell(words)


def _span_end(command: str, cursor: int, closer: str) -> int:
    """Index just past the backslash-aware span opened at ``cursor``."""
    end = cursor + 1
    while end < len(command):
        if command[end] == closer:
            return end + 1
        end += 2 if command[end] == "\\" and end + 1 < len(command) else 1
    return end


def _mask_simple_quotes(command: str) -> str:
    """Blank inert quoted spans; keep ``$(``/backtick-bearing ones visible."""
    result = []
    cursor = 0
    while cursor < len(command):
        char = command[cursor]
        if char in "'\"":  # single quotes have no escapes; double quotes are backslash-aware
            end = (command.find("'", cursor + 1) + 1 if char == "'"
                   else _span_end(command, cursor, '"'))
            segment = command[cursor:end]
            if not segment.endswith(char):
                result.append(command[cursor:])
                break
            keep = char == '"' and ("$(" in segment or "`" in segment)
            result.append(segment if keep else char * 2)
            cursor = end
        elif char == "`":
            end = _span_end(command, cursor, "`")
            result.append(command[cursor:end])
            cursor = end
        else:
            result.append(char)
            cursor += 1
    return "".join(result)


def _parse_heredoc_operator(command: str, index: int):
    """Parse one ``<<`` opener -> ``(end_index, delimiter, strip_tabs, quoted)`` or None."""
    if not command.startswith("<<", index) or command.startswith("<<<", index):
        return None
    strip_tabs = command.startswith("-", index + 2)
    cursor = index + 3 if strip_tabs else index + 2
    while cursor < len(command) and command[cursor] in " \t":
        cursor += 1
    if cursor >= len(command) or command[cursor] in "\r\n":
        return None
    delimiter: list[str] = []
    quoted = False
    while cursor < len(command) and not (command[cursor].isspace() or command[cursor] in ";&|<>()"):
        char = command[cursor]
        if char == "\\":  # backslash-escaped char: quoted, literal
            if cursor + 1 >= len(command) or command[cursor + 1] in "\r\n":
                return None
            quoted = True
            delimiter.append(command[cursor + 1])
            cursor += 2
        elif char in "'\"":
            quoted = True
            cursor += 1
            while cursor < len(command) and command[cursor] != char:
                current = command[cursor]
                if current in "\r\n":
                    return None
                if char == '"' and current == "\\":
                    if cursor + 1 >= len(command):
                        return None
                    if command[cursor + 1] in '$`"\\\n':  # else backslash is literal in dquotes
                        cursor += 1
                        current = command[cursor]
                delimiter.append(current)
                cursor += 1
            if cursor >= len(command):  # unterminated quote
                return None
            cursor += 1
        else:
            delimiter.append(char)
            cursor += 1
    if not delimiter and not quoted:
        return None
    return cursor, "".join(delimiter), strip_tabs, quoted


def _is_fd_redirect_ampersand(command: str, index: int) -> bool:
    """Return whether ``&`` at ``index`` belongs to ``>&``/``<&``/``&>`` redirection."""
    before = command[index - 1] if index else ""
    after = command[index + 1] if index + 1 < len(command) else ""
    return before in "<>" or after == ">"


def _scan_heredoc_command_unit(command: str, start: int):
    """Scan one logical command.

    Return ``(end, specs, unknown_operator, post_heredoc_list_operator, owner_start)``.
    List operators before the first heredoc select the simple command that owns it. A list
    operator after a heredoc keeps the body visible because another command may consume it.
    """
    cursor = start
    quote = None
    comment = False
    specs = []
    unknown_operator = False
    post_heredoc_list_operator = False
    owner_start = start
    while cursor < len(command):
        char = command[cursor]
        if char == "\n" and (comment or quote is None):
            break
        # Backslash escapes (incl. line continuations) outside single quotes skip the next char.
        escaped = char == "\\" and quote != "'" and not comment and cursor + 1 < len(command)
        if comment or quote is not None or escaped:
            if char == quote:
                quote = None
            cursor += 2 if escaped else 1
        elif char in "'\"`":
            quote = char
            cursor += 1
        elif char == "#" and (cursor == start or command[cursor - 1].isspace()
                              or command[cursor - 1] in ";&|()"):
            comment = True
            cursor += 1
        elif command.startswith("<<<", cursor):
            cursor += 3
        elif command.startswith("<<", cursor):
            parsed = _parse_heredoc_operator(command, cursor)
            if parsed is None:
                unknown_operator = True
                cursor += 2
            else:
                cursor, delimiter, strip_tabs, quoted = parsed
                specs.append((delimiter, strip_tabs, quoted))
        else:
            if char in ";|&" and not (
                char == "&" and _is_fd_redirect_ampersand(command, cursor)
            ):
                if specs:
                    post_heredoc_list_operator = True
                else:
                    owner_start = cursor + 1
            cursor += 1
    return cursor, specs, unknown_operator, post_heredoc_list_operator, owner_start


def _find_heredoc_close(
        command: str, body_start: int, delimiter: str, strip_tabs: bool) -> tuple[int, int] | None:
    """Return ``(terminator_line_start, position_after_it)`` for the exact terminator line."""
    cursor = body_start
    while True:
        newline = command.find("\n", cursor)
        after = len(command) if newline == -1 else newline + 1
        line = command[cursor:after].removesuffix("\n").removesuffix("\r")
        candidate = line.lstrip("\t") if strip_tabs else line
        if candidate == delimiter:
            return cursor, after
        if newline == -1:
            return None
        cursor = after


def _owner_runs_inert_consumer(
        command: str, command_start: int, command_end: int, owner_start: int) -> str | None:
    """``"plain"`` or ``"function"`` when the heredoc opener line is a plain simple command
    (``"function"``: the first command of a ``name() {`` body) whose peeled command is an
    allowlisted data consumer, else None."""
    header = _FUNCTION_HEADER_RE.match(command, command_start, command_end)
    opener_start = header.end() if header else command_start
    masked_opener = _mask_simple_quotes(command[opener_start:command_end])
    if any(marker in masked_opener for marker in ("$(", "`", "<(", ">(", "(", ")", "{", "}")):
        return None
    owner = _mask_simple_quotes(command[max(owner_start, opener_start):command_end]).split()
    peeled = _peeled_command(owner)
    if peeled is None or not _INERT_HEREDOC_CONSUMER_RE.match(peeled[1]):
        return None
    if header is None:
        return "plain"
    # `cat`'s output IS its body; a function's caller may pipe it on (`f | bash`).
    return None if peeled[1].lower() == "cat" else "function"


# Anything that could hand a function's OUTPUT to a shell (`f | bash`, `eval "$(f)"`, `. <(f)`).
_OUTPUT_EXECUTOR_RE = re.compile(r"\||\$\(|`|<\(|>\(|\beval\b|\bsource\b|(?:^|[\s;&(])\.\s")
# Programs that run text they receive as shell code (a remote login shell for bare `ssh h`).
_SINK_SHELLS = r"(?:(?:ba|da|z|k|mk|tc|c|fi|a)?sh|busybox|ssh|su|sudo\s+-[A-Za-z]*[si])"
_WORD_START, _WORD_END = r"(?<![\w.-])", r"(?![\w.-])"
# Shapes that hand some command's OUTPUT to a shell as code, so a data consumer's body (or a
# string its interpreter prints) may be shell text after all: `… | [wrappers] bash`, `| ssh h`,
# `eval`, `. <(…)`/`source /dev/stdin`, `bash <(…)`/`<<<`, `sh -c "$(…)"`, `$(…)` as a command.
_SHELL_SINK_TEMPLATE = (
    rf"(?<!\|)\|&?(?!\|)[^|;&\n]*?{_WORD_START}(?:{_SINK_SHELLS}|eval){_WORD_END}"
    rf"|{_WORD_START}eval{_WORD_END}"
    r"|(?:^|[\s;&|({])(?:source|\.)\s+(?:<\s*\(|/dev/(?:stdin|fd/)|/proc/self/fd/)"
    rf"|{_WORD_START}{_SINK_SHELLS}{_WORD_END}[^|;&\n]*?(?:<\(|<<<)"
    rf"|{_WORD_START}(?:ba|da|z|k|mk|a)?sh\s+(?:[-+][A-Za-z]+\s+)*-[A-Za-z]*c\s*\"?(?:\$\(|`)"
    # [ \t]* not \s*: newline is already an anchor, and \s* made this alternative quadratic on
    # the all-newline bodies strip_inert_heredoc_bodies leaves (review 5890191993).
    r"|(?:^|[;&|({\n]|\b(?:then|do|else)\b)[ \t]*\"?(?:\$\((?!\()")
# `$(date +%s)` prints only digits, so a shell running its output can run no command. That is
# the watcher's `deadline=$(( $(date +%s) + 2700 ))`, whose second `(` reads as a command
# position (t_01a1d67ff0075752). Only this producer is exempt: inside `$((`/`((` bash still runs
# code from other substitutions' output (`a[$(cmd)]` subscripts, `((` falling back to nested
# subshells; review 5897778680).
_NUMERIC_PRODUCER = rf"(?!{DATE_PRODUCER_BODY})"
_SHELL_SINK_RE = re.compile(_SHELL_SINK_TEMPLATE + r"|`)", re.MULTILINE)
_SHELL_SINK_NUMERIC_RE = re.compile(_SHELL_SINK_TEMPLATE + _NUMERIC_PRODUCER + r"|`)", re.MULTILINE)
# What could make `date` something other than the date binary: a function or alias of that name,
# PATH/hash/enable/BASH_ENV changes, exported or sourced definitions. Any of them drops the
# `$(date +%s)` exemption.
_DATE_REBIND_RE = re.compile(
    r"\bdate\s*\(|\bfunction\s+date\b"
    r"|\b(?:alias|hash|enable|source|declare|typeset|export|command_not_found_handle)\b"
    r"|\b(?:PATH|BASH_ENV|ENV)\+?=|(?:^|[\s;&|({])\.\s",
    re.MULTILINE)


def output_reaches_shell(command: str) -> bool:
    """Whether some command's output in *command* may be run as shell code (see
    ``_SHELL_SINK_RE``). Deliberately broad: a match only stops treating consumer heredoc bodies
    and printed string literals as data (the lifecycle walk then scans them as commands); it never
    refuses on its own. The pipe alternative takes ANY later word of the pipe segment as the
    shell, so ``| grep bash`` / ``| grep -c bash`` count as sinks too: telling a wrapper's operand
    from a filter's pattern needs a per-command grammar, and the false sink only fails closed
    (review 5890784121 nit 3; ``| grep bash.sh`` is no sink only because of the word end)."""
    from tools.shell_comments import mask_root_comment_lines

    date_exempt = not _DATE_REBIND_RE.search(command)
    command = mask_root_comment_lines(command)
    sink = _SHELL_SINK_NUMERIC_RE if date_exempt else _SHELL_SINK_RE
    # The regex sees a substitution only in command position; inside arithmetic bash also runs
    # code from its output wherever it sits (`$(( 1 + $(…) ))`, `a[$(…)]`; t_01a1d6ae05c19594).
    return (sink.search(command) is not None
            or substitution_in_arithmetic(command, date_exempt=date_exempt))


def _nonreading_shell_unit(unit: str, functions: set[str]) -> bool:
    """Positive grammar for units that cannot consume or reinterpret the stdin script.

    External programs and file-backed substitutions are not trusted by name: even an
    ordinary pathname can resolve to a script reader or a symlink to /dev/stdin.
    Only literal assignments, nonmutating builtins and already-verified functions
    qualify. Substitutions, arithmetic/indirect expansions, redirects and pipelines
    remain visible. printf is excluded because %n and -v can mutate shell state.
    """
    unit = unit.strip()
    if not unit or unit.startswith("#"):
        return True
    if _literal_assignment(unit) is not None:
        return True
    if any(mark in unit for mark in ("`", "<", ">", "\\", "\n")):
        return False
    # Simple scalar/positional expansions are arguments, never a command name.
    simple_arguments = re.sub(r"\$(?:[A-Za-z_]\w*|[0-9]+)", "ARG", unit)
    if "$" in simple_arguments:
        return False
    for segment in unit.split(";"):
        try:
            tokens = shlex.split(segment, comments=True)
        except ValueError:
            return False
        if not tokens:
            continue
        if any(char in _mask_simple_quotes(segment) for char in "|&(){}"):
            return False
        if tokens[0] in {"echo", ":"} | functions:
            continue
        if tokens == ["set", "-u"]:
            continue
        return False
    return True


class _StdinScriptState:
    """Track whether earlier units leave a nested shell's source stream untouched."""

    def __init__(self) -> None:
        self.intact = True
        self.functions: set[str] = set()
        self.pending_function: str | None = None

    def observe_unit(self, unit: str) -> None:
        unit = unit.strip()
        if self.pending_function and unit == "}":
            self.functions.add(self.pending_function)
            self.pending_function = None
        else:
            safe = _nonreading_shell_unit(unit, self.functions)
            self.intact = self.intact and safe and self.pending_function is None

    def before_heredoc(self, command: str, start: int, owner: int) -> bool:
        if self.pending_function:
            self.intact = False
        if owner > start:
            safe = _nonreading_shell_unit(command[start:owner].rstrip(";&| "), self.functions)
            self.intact = self.intact and safe
        return self.intact

    def begin_function(self, command: str, start: int, end: int) -> bool:
        header = _FUNCTION_HEADER_RE.match(command, start, end)
        if header is None:
            return False
        name = re.search(r"(?:function\s+)?([A-Za-z_][\w.:-]*)", header.group())
        if name is None:
            return False
        self.pending_function = name.group(1)
        return True


def _heredoc_bodies(command: str, start: int, specs: list) -> list[tuple[int, int, int]] | None:
    """Locate all bodies in opener order, failing closed on any missing terminator."""
    bodies = []
    for delimiter, strip_tabs, _quoted in specs:
        closed = _find_heredoc_close(command, start, delimiter, strip_tabs)
        if closed is None:
            return None
        bodies.append((start, *closed))
        start = closed[1]
    return bodies


def heredoc_body_spans(command: str) -> list[tuple[int, int, int]]:
    """``(body_start, terminator_line_start, end)`` of each heredoc body in *command*, in order
    (bodies nested in a body are part of it). Stops at the first opener it cannot parse or whose
    body has no terminator line, leaving the rest of *command* outside every span.
    """
    spans: list[tuple[int, int, int]] = []
    last_opener_index = command.rfind("<<")
    command_start = 0
    while command_start <= last_opener_index:
        command_end, specs, unknown_operator, _, _ = _scan_heredoc_command_unit(command, command_start)
        if unknown_operator or (specs and command_end >= len(command)):
            break
        bodies = _heredoc_bodies(command, command_end + 1, specs) if specs else []
        if bodies is None:
            break
        spans += bodies
        command_start = bodies[-1][2] if bodies else command_end + 1
    return spans


def strip_inert_heredoc_bodies(command: str, *, nested: bool = False, _depth: int = 0) -> str:
    """Mask heredoc bodies that are provably inert data (see module docstring).

    ``nested=True`` (the lifecycle guard's executed-path walk only) also masks data-consumer
    heredocs nested in the single quoted body of an outer heredoc a shell reads verbatim (``ssh h
    'bash -s' <<'R'``); the outer body's own lines stay visible. Text scanners keep the default:
    a nested body's literal text stays visible to them, whatever the outer body does with it."""
    # Runs on every terminal call: skip the state machine when no '<<' exists; stop past the last.
    if "<<" not in command:
        return command
    if (_CONSUMER_SHADOW_RE.search(command)
            or re.search(r"(?:^|[\s;&|])hash\s", command)):
        return command
    last_opener_index = command.rfind("<<")
    replacements: list[tuple[int, int, str, str]] = []
    function_masks = 0
    command_start = 0
    stdin = _StdinScriptState()

    while command_start <= last_opener_index:
        (
            command_end,
            specs,
            unknown_operator,
            post_heredoc_list_operator,
            owner_start,
        ) = _scan_heredoc_command_unit(command, command_start)
        if unknown_operator:
            return command
        if not specs:
            # At depth zero this state gates only recursive masking: direct
            # data consumers retain their existing handling. Unknown commands
            # can export functions or alter PATH/startup hooks for a later shell.
            stdin.observe_unit(command[command_start:command_end])
            if command_end >= len(command):
                break
            command_start = command_end + 1
            continue
        if command_end >= len(command):
            return command  # opener with no body line: unterminated — leave visible
        bodies = _heredoc_bodies(command, command_end + 1, specs)
        if bodies is None:
            return command  # unterminated
        body_cursor = bodies[-1][2]
        quoted = [spec_quoted for _delimiter, _strip_tabs, spec_quoted in specs]
        consumer = None
        if all(quoted) and not post_heredoc_list_operator:
            consumer = _owner_runs_inert_consumer(command, command_start, command_end, owner_start)
        prefix_intact = stdin.before_heredoc(command, command_start, owner_start)
        if _depth and not prefix_intact:
            # Once a previous command could steal stdin, no later source lines can
            # be interpreted as the shell's untouched program.
            return _apply_replacements(command, replacements)
        if consumer:
            if _depth and consumer == "function":
                if not stdin.begin_function(command, command_start, command_end):
                    return command
            function_masks += consumer == "function"
            # Keep each closer: downstream scanners must not close this opener
            # at a later same-delimiter heredoc and skip intervening commands.
            replacements += [(start, terminator, "\n" * command.count("\n", start, terminator), consumer)
                             for start, terminator, _end in bodies]
        elif nested and prefix_intact and _depth < _MAX_NESTED_HEREDOC_DEPTH and all(quoted) and _owner_feeds_shell(
                command, command_start, owner_start, specs):
            # An unquoted outer body expands locally first (`$(…)` runs here), and any owner but a
            # verbatim stdin shell may rewrite the body: only then are nested heredocs judged.
            start, terminator, _end = bodies[0]
            inner = command[start:terminator]
            if "<<" in inner:
                masked = strip_inert_heredoc_bodies(inner, nested=True, _depth=_depth + 1)
                if masked != inner:
                    replacements.append((start, terminator, masked, "nested"))
        # A preceding non-consumer heredoc can execute state changes too (for
        # example source /dev/stdin with an export in its body), at any depth.
        if not consumer:
            stdin.intact = False
        command_start = body_cursor
    result = _apply_replacements(command, replacements)
    if replacements and output_reaches_shell(result):
        # Some output here may be run as shell code (`( python3 - <<'PY' … ) | bash`, `f | bash`,
        # `eval "$(…)"`): a consumer's body may be the shell's program, so mask nothing.
        return command
    if function_masks and not nested and _OUTPUT_EXECUTOR_RE.search(_mask_simple_quotes(result)):
        # A function's output may reach a shell here: its consumer bodies stay visible to text scans.
        result = _apply_replacements(
            command, [item for item in replacements if item[3] != "function"])
    return result


def iter_heredoc_bodies(command: str, *, _depth: int = 0) -> Iterator[tuple[str | None, str]]:
    """``(owner, body)`` for every heredoc in *command*, bodies nested in bodies included.

    *owner* is the text of the command reading the body: its simple command on the opener line,
    or the whole opener line when several heredocs or a list operator share it. ``None`` means
    the owner is unknown (nested past ``_MAX_NESTED_HEREDOC_DEPTH``). An opener whose operator
    does not parse or whose body has no terminator yields the rest of *command* as its body, so a
    caller that treats some owners' bodies as shell code fails closed."""
    if "<<" not in command:
        return
    if _depth > _MAX_NESTED_HEREDOC_DEPTH:
        yield None, command
        return
    last_opener_index = command.rfind("<<")
    cursor = 0
    while cursor <= last_opener_index:
        end, specs, unknown_operator, post_list, owner_start = _scan_heredoc_command_unit(
            command, cursor)
        line = command[cursor:end]
        if unknown_operator:
            yield line, command[end + 1:]
            return
        if not specs:
            if end >= len(command):
                return
            cursor = end + 1
            continue
        owner = line if len(specs) > 1 or post_list else command[owner_start:end]
        bodies = _heredoc_bodies(command, end + 1, specs) if end < len(command) else None
        if bodies is None:
            yield owner, command[end + 1:]
            return
        for start, terminator, _after in bodies:
            body = command[start:terminator]
            yield owner, body
            yield from iter_heredoc_bodies(body, _depth=_depth + 1)
        cursor = bodies[-1][2]


def _apply_replacements(command: str, replacements: list[tuple[int, int, str, str]]) -> str:
    """Single-pass rebuild (replacements are sorted and non-overlapping)."""
    parts: list[str] = []
    previous = 0
    for start, end, replacement, _kind in replacements:
        parts += [command[previous:start], replacement]
        previous = end
    return "".join(parts) + command[previous:]
