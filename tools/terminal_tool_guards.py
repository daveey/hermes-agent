"""Pre-execution guards for the terminal tool.

Pure functions that decide whether a command may run at all: workdir
validation, the foreground long-lived/background-operator guidance, the
supervised-gateway lifecycle block, and the Windows self-repo git guard.
Each ``*_block`` helper returns a finished JSON error string, or None when
the command may proceed. Split out of tools/terminal_tool.py; the origin
module re-imports every public helper so ``tools.terminal_tool.<name>``
keeps resolving.
"""

import functools
import json
import logging
import re
import shlex
import stat
from pathlib import Path
from typing import Any, Optional

from tools.shell_heredoc import strip_inert_heredoc_bodies

logger = logging.getLogger("tools.terminal_tool")


# Workdir allowlist: Unicode alnum plus path/drive/UNC separators and common
# punctuation; shell metacharacters stay rejected. Unicode is allowed on
# purpose (e.g. CJK vault paths). Defense-in-depth — the cwd is also
# shlex-quoted before reaching the shell.
_WORKDIR_SAFE_ASCII_CHARS = frozenset('/\\:_-.~ +@=,')


def _is_safe_workdir_char(ch: str) -> bool:
    if not ch or ord(ch) < 32 or ord(ch) == 127:  # control chars / NUL
        return False
    return ch.isalnum() or ch in _WORKDIR_SAFE_ASCII_CHARS


def _validate_workdir(workdir: str) -> str | None:
    """Error message if *workdir* has a disallowed character, else None.
    Allowlist rather than deny-list so novel metacharacters can't slip through."""
    for ch in workdir or "":
        if not _is_safe_workdir_char(ch):
            return (
                f"Blocked: workdir contains disallowed character {ch!r}. "
                "Use a simple filesystem path without shell metacharacters."
            )
    return None


def _safe_command_preview(command: Any, limit: int = 200) -> str:
    """Return a log-safe preview for possibly-invalid command values."""
    if command is None:
        return "<None>"
    if isinstance(command, str):
        return command[:limit]
    try:
        return repr(command)[:limit]
    except Exception:
        return f"<{type(command).__name__}>"


def _blocked_json(error: str, status: str) -> str:
    """The guard result envelope: exit_code 1 + *error* + *status*."""
    return json.dumps({"output": "", "exit_code": 1, "error": error, "status": status}, ensure_ascii=False)


_SHELL_LEVEL_BACKGROUND_RE = re.compile(
    r"(?:^|[;&|]\s*|&&\s*|\|\|\s*|\$\(\s*)(?:nohup|disown|setsid)\b", re.IGNORECASE | re.MULTILINE
)
_INLINE_BACKGROUND_AMP_RE = re.compile(r"\s&\s")
_TRAILING_BACKGROUND_AMP_RE = re.compile(r"\s&\s*(?:#.*)?$")


def _strip_quotes(command: str) -> str:
    """Blank quoted / backtick content and provably-inert heredoc bodies so
    regex checks can't match keywords (nohup, setsid, '&') inside strings.

    Heredocs are masked FIRST: their delimiter may itself be quoted
    (``<<'EOF'``). ``strip_inert_heredoc_bodies`` is conservative — only a
    quoted, terminated delimiter on a simple opener fed to a known non-shell
    consumer is masked, so a real background operator can't hide in one.
    """
    result = strip_inert_heredoc_bodies(command)
    result = re.sub(r"'[^']*'", "''", result)
    result = re.sub(r'"(?:[^"\\]|\\.)*"', '""', result)
    return re.sub(r"`[^`]*`", "``", result)


_LONG_LIVED_FOREGROUND_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:dev|start|serve|watch)\b",
    r"\bdocker\s+compose\s+up\b",
    r"\bnext\s+dev\b",
    r"\bvite(?:\s|$)",
    r"\bnodemon\b",
    r"\buvicorn\b",
    r"\bgunicorn\b",
    r"\bpython(?:3)?\s+-m\s+http\.server\b",
))

# The patterns above match a server name anywhere, so a command that only MENTIONS one
# (`grep -rn uvicorn .`, `ls node_modules/vite`, `npm install nodemon`) was refused too. A
# command segment is excused when the word it executes is known to treat its arguments as
# data, or when it is a bounded form of a server command. Everything else keeps the anywhere
# match: an unrecognised wrapper (`direnv exec . uvicorn`) must still be refused, so the closed
# set is the excuse list, never a launcher list. An excuse can only turn a refusal into a run;
# nothing here refuses a command the patterns do not match.
_SEGMENT_SPLIT_RE = re.compile(r"([;&|\n]+)")
# fd redirects (`2>&1`, `&>log`) are not separators. The lookbehind keeps a long digit run from
# being rescanned at every position.
_FD_REDIRECT_RE = re.compile(r"(?<!\d)\d*[<>]&(?:\d+|-)?|&>>?")
_NULL_REDIRECT_RE = re.compile(r"(?<!\d)\d*>>?\s*/dev/null(?![^\s)}])")
# Past this many segments nothing is excused: the command keeps the plain anywhere match, which
# bounds the cost of a pathological input.
_MAX_EXCUSABLE_SEGMENTS = 2000
_LOG_FOLLOWERS = frozenset({"tail", "docker", "docker-compose", "podman", "nerdctl", "kubectl", "oc", "journalctl"})
_PACKAGE_PLAIN_OPTIONS = frozenset({"-q", "--quiet", "-y", "--yes", "-g", "--global", "-v", "--verbose",
                                    "--no-cache", "--no-cache-dir", "--no-input", "--disable-pip-version-check"})
_FLAG_END_RE = re.compile(r"[>)}]")
_GIT_PLAIN_GLOBAL_OPTIONS = frozenset({"--no-pager", "-P", "-p", "--paginate", "--bare", "--no-replace-objects", "--literal-pathspecs"})


def _blank_fd_redirect(match: re.Match) -> str:
    """`2>&1` is no separator and writes nothing; `&>file` / `>&file` still write a file."""
    text = match.group()
    return " > " if text.startswith("&>") or text.endswith("&") else " "
_ENV_ASSIGNMENT_RE = re.compile(r"[A-Za-z_]\w*=")
_DURATION_RE = re.compile(r"\d+(?:\.\d+)?[smhd]?$")
_PYTHON_RE = re.compile(r"(?:python[\d.]*|py)$")
_SYSTEM_BIN_DIRS = ("/bin/", "/sbin/", "/usr/bin/", "/usr/sbin/", "/usr/local/bin/", "/opt/homebrew/bin/")
_TRANSPARENT_PREFIXES = frozenset({"time", "then", "do", "else", "elif", "if", "while", "until", "!", "env"})
_SUDO_FLAGS = frozenset({"-E", "-H", "-n", "-k", "-S", "-b"})
_COMPOSE_VALUE_FLAGS = frozenset({"-f", "--file", "-p", "--project-name", "--project-directory", "--profile", "--env-file"})
_DATA_COMMANDS = frozenset((
    "echo printf cat ls ll dir type grep egrep fgrep rg ag ack head wc stat du df file which whereis "
    "whence rm rmdir mkdir cp mv ln touch chmod chown cd pushd test [ sed awk sort uniq cut tr jq yq "
    "tree basename dirname realpath readlink diff cmp tar zip unzip fd find tail tee locate pgrep ps "
    "lsof curl wget git export unset column nl base64 xxd od fold paste tac rev comm md5sum sha256sum "
    "shasum true false done fi esac"
).split())
# Subcommands of `docker` / `docker compose` that report and return.
_DOCKER_REPORT_SUBCOMMANDS = frozenset({"ps", "port", "config", "images", "version", "ls", "inspect"})
_PACKAGE_TOOLS = frozenset("npm pnpm yarn bun pip pip3 uv poetry pdm pipx brew apt apt-get apk dnf conda cargo gem".split())
_PACKAGE_DATA_SUBCOMMANDS = frozenset((
    "install i add update up upgrade remove rm uninstall ls list view info show why outdated audit ci "
    "pkg search freeze download sync lock"
).split())
_VITE_LAUNCHERS = frozenset({"npx", "bunx", "pnpx", "npm", "pnpm", "yarn", "bun", "exec", "dlx", "x"})


def _has_flag(args: list, short: str = "", long: tuple = ()) -> bool:
    """True when *args* carry one of the *short* letters (alone or in a cluster such as `-fn`,
    `-100f`) or one of the *long* options, bare or with a value (`--follow=name`)."""
    for arg in args:
        if arg.startswith("--"):
            if arg.split("=", 1)[0] in long:
                return True
        elif arg.startswith("-") and any(letter in arg for letter in short):
            return True
    return False


def _git_runs_command(args: list) -> bool:
    """git forms that execute a command named in their arguments. Options before the subcommand
    are git's own (`-c alias.x=!cmd`); after it they belong to the subcommand (`switch -c name`)."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "-C":
            i += 2
        elif args[i] in _GIT_PLAIN_GLOBAL_OPTIONS:
            i += 1
        else:  # `-c alias.x=!cmd`, or an option this walk cannot place
            return True
    subcommand, rest = "".join(args[i:i + 1]), args[i + 1:]
    return (subcommand in ("bisect", "difftool", "mergetool", "filter-branch")
            or (subcommand == "submodule" and "foreach" in rest)
            or (subcommand == "rebase" and (_has_flag(rest, "x") or any(a.startswith("--exec") for a in rest)))
            or (subcommand == "grep" and any(a.startswith(("-O", "--open-files-in-pager")) for a in rest)))


# Data commands stop being data when an argument makes them run a command or never exit.
_DATA_COMMAND_EXCEPTIONS = {
    "find": lambda args: any(a in ("-exec", "-execdir", "-ok", "-okdir") for a in args),
    # `--f…` can only abbreviate `--follow` among tail's long options.
    "tail": lambda args: _has_flag(args, "fF") or any(a.startswith("--f") for a in args),
    "git": _git_runs_command,
    "fd": lambda args: _has_flag(args, "xX", ("--exec", "--exec-batch")),
    "rg": lambda args: _has_flag(args, long=("--pre", "--hostname-bin")),
    "ag": lambda args: _has_flag(args, long=("--pager",)),
    "sort": lambda args: _has_flag(args, long=("--compress-program",)),
    "tar": lambda args: "-I" in args or _has_flag(args, long=("--to-command", "--checkpoint-action", "--use-compress-program")),
}


def _command_word(tokens: list) -> tuple[str, list]:
    """``(word, args)`` for the command a segment executes, past env assignments and the few
    prefixes that pass a command through unchanged. A prefix carrying an option this does not
    know is returned as the word itself, which is not a data command and keeps the anywhere
    match."""
    i = 0
    while i < len(tokens):
        word = tokens[i].lstrip("({").lower()
        # `./echo` is a local script; only a system directory makes `/usr/bin/echo` echo.
        if word.startswith(_SYSTEM_BIN_DIRS) and ".." not in word:
            word = word.rsplit("/", 1)[-1]
        if not word:  # a spaced group opener: `( grep vite x )`
            i += 1
        elif word == "sudo":
            i += 1
            while i < len(tokens) and tokens[i].startswith("-"):
                if tokens[i] in ("-u", "-g"):
                    i += 2
                elif tokens[i] in _SUDO_FLAGS:
                    i += 1
                else:
                    return word, []
        elif word == "timeout" and _DURATION_RE.match(tokens[i + 1] if i + 1 < len(tokens) else ""):
            i += 2
        elif word in _TRANSPARENT_PREFIXES or _ENV_ASSIGNMENT_RE.match(tokens[i]):
            i += 1
        else:
            return word, tokens[i + 1:]
    return "", []


def _vite_one_shot(words: list) -> bool:
    """`vite build` / `vite optimize` with vite itself as the command (`nodemon --exec vite build`
    is not) and no watch flag, since a watched build never exits."""
    for i, word in enumerate(words[:-1]):
        if re.split(r"[/\\]", word)[-1] == "vite" and words[i + 1] in ("build", "optimize"):
            return (all(w in _VITE_LAUNCHERS or w.startswith("-") or _ENV_ASSIGNMENT_RE.match(w) for w in words[:i])
                    and not _has_flag(words, "w", ("--watch",)))
    return False


def _is_bounded_server_form(word: str, args: list, tokens: list) -> bool:
    """A server command that exits by itself: a one-shot build or a config check."""
    if word.rsplit("/", 1)[-1] == "gunicorn":
        return "--check-config" in args
    return _vite_one_shot([t.lower() for t in tokens])


def _is_detached_compose_up(word: str, args: list) -> bool:
    """`docker compose [options] up -d`: returns once the containers have started."""
    if word != "docker" or args[:1] != ["compose"]:
        return False
    # What follows a bare `--` belongs to another program, so a `-d` there does not count.
    own = args[1:args.index("--")] if "--" in args else args[1:]
    i = 0
    while i < len(own) and own[i].startswith("-"):
        i += 2 if own[i] in _COMPOSE_VALUE_FLAGS else 1
    return own[i:i + 1] == ["up"] and any(a in ("-d", "--detach") for a in own[i:])


def _is_data_command(word: str, args: list) -> bool:
    """True when *word* only reads, writes or lists what its arguments name."""
    if word == "command":
        return args[:1] in (["-v"], ["-V"])
    if (word == "uv" and args[:1] == ["pip"]) or (_PYTHON_RE.match(word.rsplit("/", 1)[-1]) and args[:2] == ["-m", "pip"]):
        args = args[args.index("pip") + 1:]
    elif word not in _PACKAGE_TOOLS:
        runs_or_follows = _DATA_COMMAND_EXCEPTIONS.get(word)
        return word in _DATA_COMMANDS and not (runs_or_follows and runs_or_follows(args))
    # Only options known to take no value may precede the subcommand: after any other, a
    # "subcommand" may be that option's value (`npm --prefix install run dev`).
    i = 0
    while i < len(args) and args[i] in _PACKAGE_PLAIN_OPTIONS:
        i += 1
    return "".join(args[i:i + 1]) in _PACKAGE_DATA_SUBCOMMANDS


def _follows_logs(tokens: list) -> bool:
    """A log follower anywhere in the segment, behind any wrapper (`command tail -f app.log`,
    `docker compose logs -f`). It names no server, so it must be found by its own shape."""
    seen = set()  # a name's first occurrence has every later one's arguments after it
    for i, token in enumerate(tokens):
        name = token.lstrip("({").rsplit("/", 1)[-1].lower()
        if name not in _LOG_FOLLOWERS or name in seen:
            continue
        seen.add(name)
        # A flag may be glued to a closer or a redirect: `(… logs --follow)`, `-f>out.txt`.
        rest = [_FLAG_END_RE.split(arg, 1)[0] for arg in tokens[i + 1:]]
        if name == "tail":
            if _DATA_COMMAND_EXCEPTIONS["tail"](rest):
                return True
        elif name == "journalctl" or "logs" in rest:
            # Before `logs`, `-f` is compose's file option.
            own = rest if name == "journalctl" else rest[rest.index("logs") + 1:]
            if _has_flag(own, "f", ("--follow",)):
                return True
    return False


def _returns_promptly(word: str, args: list) -> bool:
    """Commands that are not data commands but end by themselves: a timed sleep, a docker report."""
    if word == "sleep":
        return bool(args) and all(_DURATION_RE.match(arg) for arg in args)
    if word == "docker":
        own = args[1:] if args[:1] == ["compose"] else args
        i = 0
        while i < len(own) and own[i].startswith("-"):
            i += 2 if own[i] in _COMPOSE_VALUE_FLAGS else 1
        return "".join(own[i:i + 1]) in _DOCKER_REPORT_SUBCOMMANDS
    return False


def _parse_segment(segment: str) -> tuple[str, list, list] | None:
    """``(word, args, tokens)`` for an excusable segment, or None when it must keep the anywhere
    match: a substitution runs its contents whatever the outer command is
    (`echo $(npm run dev)`), and an unbalanced `)` before the end is a case arm
    (`dev ) npm run dev ;;`) whose first word is a pattern. A closing `)` / `}` at the very end
    only ends a subshell or group."""
    body = segment.rstrip(")} \t\r")
    if any(marker in segment for marker in _EXECUTABLE_SUBSTITUTION_MARKERS) or body.count(")") > body.count("("):
        return None
    tokens = body.split()
    return (*_command_word(tokens), tokens)


def _names_a_server(text: str) -> bool:
    return any(pattern.search(text) for pattern in _LONG_LIVED_FOREGROUND_PATTERNS)


class _CommandSegments:
    """The segments of one command and what surrounds each. Every table is one pass over the
    command, built on first use, so a long command costs linear time."""

    def __init__(self, unquoted: str):
        parts = _SEGMENT_SPLIT_RE.split(_FD_REDIRECT_RE.sub(_blank_fd_redirect, unquoted))
        self.texts, self.separators = parts[::2], parts[1::2]

    @functools.cached_property
    def parsed(self) -> list:
        return [_parse_segment(text) for text in self.texts]

    @functools.cached_property
    def is_data(self) -> list:
        return [seg is not None and _is_data_command(seg[0], seg[1]) for seg in self.parsed]

    def _piped(self, index: int) -> bool:
        return index < len(self.separators) and "|" in self.separators[index] and "||" not in self.separators[index]

    @functools.cached_property
    def piped_to_runner(self) -> list:
        """Per segment: its output reaches something that is not a data command and may run it
        (`echo npm run dev | tee x | sh`)."""
        reaches = [False] * len(self.texts)
        for index in range(len(self.texts) - 2, -1, -1):
            reaches[index] = self._piped(index) and (not self.is_data[index + 1] or reaches[index + 1])
        return reaches

    @functools.cached_property
    def writes_a_file(self) -> list:
        """Per segment: it, or a `tee` it is piped into, writes its text to a file."""
        writes = [">" in _NULL_REDIRECT_RE.sub(" ", text) or (seg is not None and seg[0] == "tee")
                  for text, seg in zip(self.texts, self.parsed)]
        for index in range(len(self.texts) - 2, -1, -1):
            writes[index] = writes[index] or (self._piped(index) and writes[index + 1])
        return writes

    @functools.cached_property
    def harmless_after(self) -> list:
        """Per segment: every later segment is empty, a data command, or returns promptly."""
        harmless = [True] * len(self.texts)
        for index in range(len(self.texts) - 2, -1, -1):
            later, seg = index + 1, self.parsed[index + 1]
            harmless[index] = harmless[later] and (
                not self.texts[later].strip() or self.is_data[later] or (seg is not None and _returns_promptly(seg[0], seg[1])))
        return harmless

    def excused(self, index: int) -> bool:
        seg = self.parsed[index]
        if seg is None:
            return False
        word, args, tokens = seg
        if _is_bounded_server_form(word, args, tokens):
            return True
        if _is_detached_compose_up(word, args):
            # It returns at once, which helps only if nothing after it holds the call open
            # (`up -d && docker compose logs -f`, `… && npm test`).
            return self.harmless_after[index]
        if not self.is_data[index] or self.piped_to_runner[index]:
            return False
        # Text written to a file can be run by what follows (`echo npm run dev > x.sh; sh x.sh`).
        return not self.writes_a_file[index] or self.harmless_after[index]


def _starts_long_lived_server(unquoted: str) -> bool:
    # Whole-string check first: nearly every command names no server at all.
    if not _names_a_server(unquoted):
        return False
    command = _CommandSegments(unquoted)
    if len(command.texts) > _MAX_EXCUSABLE_SEGMENTS:
        return True
    # An excused mention must not carry a log follower through with it, whichever comes first
    # (`grep uvicorn requirements.txt && tail -f app.log`, `vite build && docker compose logs -f`).
    if any(_follows_logs(text.split()) for text in command.texts):
        return True
    return any(_names_a_server(text) and not command.excused(index) for index, text in enumerate(command.texts))

# Every refusal below rejects the WHOLE command before anything runs. Saying so stops the resend
# that keeps only the refused tail: a dropped `cat > x.sh <<EOF` setup then runs another session's
# stale x.sh from the shared scratch dir instead of failing "not found".
_NOTHING_RAN = (
    " Nothing in this command ran (no file writes, heredocs, cd or setup steps). Re-send all of it: "
    "put setup in a foreground call first, or the whole script in the background call."
)

# Ordered (predicate on the unquoted command, guidance) — first hit wins.
_FOREGROUND_GUIDANCE = (
    (
        _SHELL_LEVEL_BACKGROUND_RE.search,
        "Foreground command uses shell-level background wrappers (nohup/disown/setsid). "
        "Re-send WITHOUT the wrapper as terminal(command=\"<cmd>\", background=true, "
        "notify_on_complete=true) so Hermes tracks the process, then run readiness "
        "checks and tests in separate commands." + _NOTHING_RAN,
    ),
    (
        lambda s: _INLINE_BACKGROUND_AMP_RE.search(s) or _TRAILING_BACKGROUND_AMP_RE.search(s),
        "Foreground command uses '&' backgrounding. Re-send WITHOUT the '&' as "
        "terminal(command=\"<cmd>\", background=true) — add notify_on_complete=true "
        "for bounded jobs — then run health checks and tests in follow-up terminal calls." + _NOTHING_RAN,
    ),
    (
        _starts_long_lived_server,
        "This foreground command appears to start a long-lived server/watch process. "
        "Run it with background=true, verify readiness (health endpoint/log signal), "
        "then execute tests in a separate command." + _NOTHING_RAN,
    ),
)


def _looks_like_help_or_version_command(command: str) -> bool:
    """Return True for informational invocations that should never be blocked."""
    normalized = " ".join(command.lower().split())
    return (
        " --help" in normalized
        or normalized.endswith(" -h")
        or " --version" in normalized
        or normalized.endswith(" -v")
    )


def _foreground_background_guidance(command: str) -> str | None:
    """Guidance text when a foreground command looks long-lived or uses shell
    backgrounding (it should be a managed background session), else None."""
    if _looks_like_help_or_version_command(command):
        return None
    unquoted = _strip_quotes(command)
    return next((msg for hit, msg in _FOREGROUND_GUIDANCE if hit(unquoted)), None)


# `&` spellings that are not backgrounding: and-lists, fd redirects (`2>&1`, `>&2`, `&>`, `&>>`,
# `<&3`) and the stderr pipe `|&`.
_NON_BACKGROUND_AMP_RE = re.compile(r"&&|\d*[<>]&(?:\d+|-)?|&>>?|\|&")
_TRAILING_AMP_ONLY_RE = re.compile(r"\s&\s*$")
# Executable substitutions run code the quote-masking cannot see into (`"$(sleep 9 & echo $!)"`
# hides a second, untracked background job), so any spelling of one refuses the conversion. Raw
# substring match on purpose: a false refusal is today's behaviour, a false conversion is not.
_EXECUTABLE_SUBSTITUTION_MARKERS = ("$(", "`", "<(", ">(")


def _strip_trailing_amp(command: str) -> str:
    """*command* minus its final `&` and the UNESCAPED whitespace right before it.

    A backslash-escaped space is part of the last argument (`trailing\\ ` is the word `trailing `),
    so trimming stops at the first whitespace byte preceded by an odd run of backslashes.
    """
    body = command.rstrip()[:-1]
    end = len(body)
    while end and body[end - 1].isspace():
        backslashes = len(body[:end - 1]) - len(body[:end - 1].rstrip("\\"))
        if backslashes % 2:
            break
        end -= 1
    return body[:end]


def _approval_identity(command: str) -> tuple:
    """Everything the approval path keys a decision on for *command*: hardline verdict, the
    dangerous pattern key/description (what session/permanent approvals are stored under), the
    sudo-stdin floor, runtime self-delete, the user's deny rules and the permanent allowlist."""
    from agent.runtime_self_protection import command_deletes_runtime
    from tools.approval import _command_matches_permanent_allowlist, _match_user_deny_rule
    from tools.approval_detection import _check_sudo_stdin_guard, detect_dangerous_command, detect_hardline_command
    return (
        tuple(detect_hardline_command(command)),
        tuple(detect_dangerous_command(command)),
        tuple(_check_sudo_stdin_guard(command)),
        command_deletes_runtime(command),
        _match_user_deny_rule(command),
        bool(_command_matches_permanent_allowlist(command)),
    )


def trailing_amp_background_command(command: str) -> str | None:
    """The command without its trailing `&` when that `&` is its ONLY backgrounding, else None.

    Only the unambiguous shape converts: one line, one and-or list or pipeline (`a && b | c &`
    backgrounds the whole list in bash, exactly what background=true runs), nothing after the
    `&`. Inline `a & b`, `;` sequences, multi-line scripts/heredocs, nohup/disown/setsid and any
    executable substitution (`$(...)`, backticks, `<(...)`/`>(...)`) keep the refusal: the tool
    cannot prove there is no other background job or that later commands don't depend on a
    readiness check. Quoted `&` (and inert heredoc bodies) never count. Conversion must leave
    the approval identity untouched, otherwise the refusal stands.
    """
    if not isinstance(command, str) or _looks_like_help_or_version_command(command):
        return None
    if any(marker in command for marker in _EXECUTABLE_SUBSTITUTION_MARKERS):
        return None
    unquoted = _strip_quotes(command)
    if not _TRAILING_AMP_ONLY_RE.search(unquoted) or not command.rstrip().endswith("&"):
        return None
    body = unquoted.rstrip()[:-1]
    if "\n" in body or "\r" in body or ";" in body or "#" in body or _SHELL_LEVEL_BACKGROUND_RE.search(body):
        return None
    if "&" in _NON_BACKGROUND_AMP_RE.sub("", body):
        return None
    stripped = _strip_trailing_amp(command)
    if not stripped.strip():
        return None
    # Approval runs on what executes (the stripped command). Some patterns key on the `&` itself
    # (`hermes gateway run &` = gateway outside its service manager), and approvals are stored per
    # pattern key, so a different key would let a pre-approval for another category pass.
    if _approval_identity(command) != _approval_identity(stripped):
        return None
    return stripped


def _read_script_for_guard(env: Any, guard_cwd: str, script_path: str, max_bytes: int) -> Optional[str]:
    """Best-effort script read: host filesystem first, then a bounded
    ``env.execute('head -c ... < path')`` for remote backends. Binary content
    (NUL byte) is not a script: feeding it to the guard tokenizes machine code
    into bogus paths and crashes the scanner, so it yields None."""
    if env is None:
        return None
    try:
        local_path = Path(script_path).expanduser()
        if not local_path.is_absolute():
            local_path = Path(guard_cwd) / local_path
        if local_path.is_file():
            metadata = local_path.stat()
            if stat.S_ISREG(metadata.st_mode) and metadata.st_size <= max_bytes:
                data = local_path.read_bytes()
                if len(data) <= max_bytes:
                    return None if b"\x00" in data else data.decode("utf-8", errors="replace")
    except Exception:
        pass
    # Remote backend: bound the read at the source with `head -c` so an
    # oversized binary never crosses the wire (an unbounded `cat` once
    # pinned the gateway's tool thread for 30+ min on a shlex scan). One
    # byte over budget is enough for lifecycle_guard to fail closed. The
    # `< path` redirect keeps leading-dash paths out of argv.
    try:
        result = env.execute(f"head -c {max_bytes + 1} < {shlex.quote(script_path)}")
        if result.get("returncode", -1) == 0:
            output = result.get("output", "")
            return None if output and "\x00" in output else output
    except Exception:
        pass
    return None


def _session_aliases_for_guard(env: Any) -> Optional[frozenset[str]]:
    """Alias names the backend's command shell holds before the command runs (its login
    snapshot turns ``expand_aliases`` on); ``None`` = unknown. Looked up on the class so a test
    double's auto-attributes (``MagicMock``) never stand in for a backend's answer; a backend
    without the hook has no snapshot."""
    hook = getattr(type(env), "lifecycle_guard_session_aliases", None)
    if hook is None:
        return frozenset()
    try:
        names = hook(env)
    except (AttributeError, TypeError, ValueError, OSError):  # a half-built backend: unknown
        return None
    if names is None or isinstance(names, frozenset):
        return names
    return None


# hermes-sync can idle-restart the gateway it runs under, so the guard refuses it inside the
# gateway (by lifecycle content, or because the script is over the scan budget). The generic
# "separate shell" advice made agents wrap it in systemd-run/nohup; name the sanctioned route.
_HERMES_SYNC = "hermes-sync"


def _runs_hermes_sync(command: str) -> bool:
    """Whether *command* EXECUTES hermes-sync (a mere mention such as ``grep x hermes-sync`` does
    not count); see ``cron.lifecycle_guard.executes_command_named``."""
    from cron.lifecycle_guard import executes_command_named

    return executes_command_named(command, _HERMES_SYNC)


def _refusal_hint(command: str) -> str:
    if not _runs_hermes_sync(command):
        return ""
    from hermes_constants import display_hermes_home, get_default_hermes_root, get_hermes_home

    # hermes-sync logs under its own home (the root ~/.hermes), not a profile gateway's home.
    root = get_default_hermes_root(home=get_hermes_home())
    sync_log = f"{display_hermes_home(root)}/logs/hermes-sync.log"
    return (
        " hermes-sync runs only from its scheduled cron/systemd pass, never from a gateway chat. "
        "To deploy a pushed hermes-mods commit from here, run "
        "`git -C ~/code/hermes-mods fetch -q origin && git -C ~/code/hermes-mods merge --ff-only origin/main`, "
        "then wait for the next scheduled "
        f"sync pass and read its `mods-reload:` line in {sync_log}. "
        "Do not wrap hermes-sync in systemd-run, nohup, setsid or a background shell to get past "
        "this refusal."
    )


def gateway_lifecycle_block(
    *,
    command: str,
    env: Any,
    env_type: str,
    cwd: str,
    workdir: Optional[str],
    session_key: str,
) -> Optional[str]:
    """Refuse gateway lifecycle commands issued from inside the supervised gateway.

    ``systemctl``/``launchctl``/``hermes gateway restart|stop|uninstall``
    targeting hermes-gateway would SIGTERM the gateway — and this very
    subprocess — before completing, so the service may never come back.
    Applies unconditionally (``force=True`` cannot bypass it). Gated on the
    SUPERVISED-gateway probe, not the raw ``_HERMES_GATEWAY`` marker: that
    marker leaks into every process that merely imports gateway.run (hermes
    serve, CLI, web server), which must still be able to restart the gateway;
    an unsupervised foreground ``hermes gateway run`` has no KeepAlive to turn
    a self-restart into a respawn loop, so it passes too.
    Returns the JSON error string when blocked, else None.
    """
    from tools.environments.sandbox_scope import hosted_scope
    from tools.process_registry import _is_supervised_gateway_process
    from tools.terminal_tool import _resolve_command_cwd, get_session_cwd

    # A hosted command runs in the agent's remote sandbox, which cannot reach this gateway; the
    # scan below would read the scripts it names on this host.
    if hosted_scope() is not None or not _is_supervised_gateway_process():
        return None
    from cron.lifecycle_guard import (
        _MAX_REFERENCED_SCRIPT_BYTES,
        HOST_INTERPRETER_KILL_REJECTION,
        contains_host_interpreter_kill,
        contains_launchctl_submit_command,
        lifecycle_scan_budget_summary,
        lifecycle_scan_root_within_budget,
        scan_gateway_lifecycle,
    )
    # Keep the specific launchctl diagnostic when this optional pre-scan fits the
    # budget. The full fail-closed guard below still runs when it does not, so
    # oversized roots never reach shlex here.
    if lifecycle_scan_root_within_budget(command) and contains_launchctl_submit_command(command):
        return _blocked_json(
            "Blocked: launchctl submit/bootstrap is restricted inside a supervised "
            "gateway regardless of the job label, to prevent indirect gateway "
            "restart loops. This guard does not inspect the job's KeepAlive settings "
            "or determine whether it is independent of Hermes. Perform authorized "
            "LaunchAgent maintenance from a separate shell outside the gateway, "
            "not by switching launchctl verbs to bypass this rejection.",
            "error",
        )
    guard_cwd_base = get_session_cwd(session_key)
    if guard_cwd_base is None:
        guard_cwd_base = getattr(env, "cwd", None) or cwd
    guard_cwd = _resolve_command_cwd(
        workdir=workdir, default_cwd=guard_cwd_base, session_key=session_key, env_type=env_type,
        mounted_host=getattr(env, "host_cwd", None),
        env=env,
    )
    # A local backend shares this host's disk: a path the scan cannot find here cannot run there
    # either, so a backend read would repeat the miss and spend the remote-read cap (a large tool
    # script naming ~100 non-file words once failed closed on "remote reads").
    read_remote_script = None if env_type == "local" else (
        lambda p: _read_script_for_guard(env, guard_cwd, p, _MAX_REFERENCED_SCRIPT_BYTES)
    )
    unsafe, refusal = scan_gateway_lifecycle(
        command,
        cwd=guard_cwd,
        read_remote_script=read_remote_script,
        session_aliases=_session_aliases_for_guard(env),
    )
    if unsafe and refusal:
        # Not a lifecycle command: a script the command EXECUTES could not be scanned (budget,
        # size, device, live SQLite, cloud placeholder). Say so, or the model rewords and retries
        # the same command in a loop (#113944).
        return _blocked_json(
            f"Blocked: the lifecycle guard could not scan this command or referenced script: {refusal}. "
            "Nothing in the command is known to contain a gateway lifecycle command, but a "
            "script the command executes must be scannable (a regular text file "
            f"{lifecycle_scan_budget_summary()}) before it can run inside the gateway process."
            + _refusal_hint(command),
            "error",
        )
    if unsafe:
        # Name the ownership-scoped route for image-name kills: the intent is almost always "stop
        # MY background job", and re-rolling the same over-broad spelling is what takes the gateway down.
        if lifecycle_scan_root_within_budget(command) and contains_host_interpreter_kill(command):
            return _blocked_json(HOST_INTERPRETER_KILL_REJECTION, "error")
        return _blocked_json(
            "Blocked: command or referenced script cannot restart, stop, or "
            "uninstall the gateway from inside the gateway process. The gateway would "
            "kill this command before it could complete (SIGTERM propagates "
            "to child processes). Run `hermes gateway restart` from a "
            "separate shell outside the running gateway." + _refusal_hint(command),
            "error",
        )
    return None


# This is an origin-footgun guard, not a shell sandbox. Reuse the existing
# command-position scanner: quoted documentation/queries are not invocations.
# Scripts, substitutions, remote SSH payloads and generated argv are deliberately
# not interpreted here. Do not turn this into a blanket worker-CLI prohibition.
_HERMES_VALUE_OPTIONS = frozenset({
    "--profile", "-p", "--model", "-m", "--provider", "--reasoning",
    "--skills", "-s", "--toolsets", "-t", "--query", "-q", "--query-file",
    "--image", "--format", "--resume", "-r", "--in", "--max-turns",
    "--run-budget", "--source",
})


def _is_titled_chat_upsert(argv: list[str]) -> bool:
    """Recognize literal CLI options without mistaking option values for flags."""
    chat = named_continue = create = False
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--":
            break
        option, equals, value = token.partition("=")
        if option in {"--help", "-h", "--version"}:
            return False
        if option in {"--continue", "-c"}:
            if not equals and i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                i += 1
                value = argv[i]
            named_continue = bool(value)
        elif token == "--create-if-missing":
            create = True
        elif option in _HERMES_VALUE_OPTIONS:
            if not equals:
                i += 1
        elif not token.startswith("-"):
            # First positional must be the chat subcommand; other Hermes
            # subcommands (including help text/search) must never match.
            if not chat:
                if token != "chat":
                    return False
                chat = True
        i += 1
    return chat and named_continue and create


def background_user_chat_block(command: str, *, background: bool) -> Optional[str]:
    """Reject agent background titled-chat upserts before acquiring any backend.

    PTY/persistence/approval do not make a child CLI a Desktop-owned runtime.
    Ordinary workers (-q, --query-file, --resume) remain supported. Foreground
    CLI invocations remain unchanged. This also covers timeout promotion.
    """
    if not background:
        return None
    from cron.lifecycle_guard import (
        _executed_command_index, _iter_command_segments, _iter_shell_command_payloads,
    )

    pending = [(strip_inert_heredoc_bodies(command), 0)]
    while pending:
        text, depth = pending.pop()
        for segment in _iter_command_segments(text):
            index = _executed_command_index(segment)
            if index is None:
                continue
            name = Path(segment[index]).name
            argv = segment[index + 1:]
            # The lifecycle helper recognizes -c/--command, but not the
            # common combined spelling -lc used by terminal launchers.
            if depth < 4 and name in {"sh", "bash", "zsh", "dash", "ksh"}:
                for j, flag in enumerate(argv[:-1]):
                    if re.fullmatch(r"-[a-zA-Z]+", flag) and "c" in flag and flag != "-c":
                        pending.append((argv[j + 1], depth + 1))
                        break
            if re.fullmatch(r"python(?:3(?:\.\d+)?)?", name) and argv[:2] == ["-m", "hermes_cli.main"]:
                argv = argv[2:]
            elif name not in {"hermes", "hermes.exe"}:
                continue
            if _is_titled_chat_upsert(argv):
                return _blocked_json(
                    "Blocked: a background `hermes chat --continue TITLE --create-if-missing` "
                    "creates or takes over a named chat in an unattended CLI, not an interactive "
                    "Desktop conversation. For an existing Desktop chat, use cross_session "
                    "action=list then action=send with its exact target. For a new chat, create "
                    "it in Desktop (the routed gateway API is session.create followed by "
                    "prompt.submit on the returned session_id, on the same gateway/profile). "
                    "If no Desktop creation route is available, ask the user to create the chat; "
                    "do not substitute a detached CLI. For an independent worker, use "
                    "`hermes chat -q ... --source tool` without the titled-chat upsert flags "
                    "and report it as a worker, not a Desktop conversation.",
                    "blocked",
                )
        if depth < 4:
            pending.extend((payload, depth + 1) for payload in _iter_shell_command_payloads(text))
    return None


def self_repo_block(
    *,
    command: str,
    cwd: str,
    workdir: Optional[str],
    session_key: str,
) -> Optional[str]:
    """Windows-only guard against git-mutating the checkout backing this interpreter.

    NTFS locks loaded module files, so rewriting the live checkout can corrupt
    the running process; POSIX keeps old inodes alive for open handles, so the
    guard is off there (``guard_active``). Local backend only — remote
    backends cannot reach that checkout. Returns the JSON error string when
    blocked, else None.
    """
    from tools.self_repo_guard import detect_self_repo_git_mutation, guard_active
    from tools.terminal_tool import _resolve_command_cwd

    if not guard_active():
        return None
    guard_cwd = _resolve_command_cwd(workdir=workdir, default_cwd=cwd, session_key=session_key)
    hit, msg = detect_self_repo_git_mutation(command, guard_cwd)
    if not hit:
        return None
    logger.warning("Blocked self-repo git mutation (command: %s)", _safe_command_preview(command))
    return _blocked_json(msg, "blocked")


def locked_skill_block(*, command: str, cwd: str, workdir: Optional[str], session_key: str) -> Optional[str]:
    """Refuse commands that write into a skill the user locked, change the lock record, or run
    ``hermes curator unlock`` (see tools/skill_lock). Local backend only: a remote or container
    backend cannot reach this profile's skills dir. Returns the JSON error string when blocked."""
    from tools.skill_lock import command_refusal
    from tools.terminal_tool import _resolve_command_cwd

    guard_cwd = _resolve_command_cwd(workdir=workdir, default_cwd=cwd, session_key=session_key)
    reason = command_refusal(command, guard_cwd)
    if reason is None:
        return None
    logger.warning("Blocked write to a locked skill (command: %s)", _safe_command_preview(command))
    return _blocked_json(f"Blocked: {reason}", "blocked")
