"""Gateway lifecycle guard for cron job creation.

A cron job that restarts/stops the gateway from inside the gateway (``hermes gateway restart``,
``launchctl kickstart ai.hermes.gateway``, ``systemctl restart hermes-gateway``) kills the process,
the supervisor revives it, auto-resume re-runs the turn: a SIGTERM-respawn loop.
``cron.jobs.create_job`` rejects such specs on every creation path. Patterns are command-shaped —
anchored on concrete command identifiers — so they cannot fire on prose. Defence-in-depth layer.
"""

from __future__ import annotations

import itertools
import logging
import os
import re
import shlex  # unused here; tests reach shlex.shlex through lifecycle_guard.shlex
import stat
import sys
from pathlib import Path
from typing import Callable, Iterator, Optional

from cron.lifecycle_guard_aliases import _may_define_alias, _session_aliases_shadow_consumers
from cron.lifecycle_guard_groups import CONTROL_CHARS as _CONTROL_CHARS
from cron.lifecycle_guard_groups import (
    heredoc_scripts, iter_script_segment_contexts, join_continued_lines)
from cron.lifecycle_guard_words import REDIRECTION as _REDIRECTION
from cron.lifecycle_guard_words import redirection_parts, tokenize_redirections
from cron.lifecycle_guard_lines import join_line_continuations
# Lines a printer may print, walked as the sink's command (moved out for FILE_LINES).
from cron.lifecycle_guard_printf import printed_argument_lines as _printed_argument_lines
from cron.lifecycle_guard_printf import with_escapes_decoded as _with_escapes_decoded
from cron.lifecycle_guard_substitutions import SubstitutionNestingLimit
from cron.lifecycle_guard_tokens import _iter_command_token_lists, _shlex_tokens
# How `uv [global options] run [options] <command>` reads its argv.
from cron.lifecycle_guard_uv import _uv_run_command_indices

logger = logging.getLogger(__name__)


class GatewayLifecycleBlocked(ValueError):
    """Raised when a cron job spec contains a gateway-lifecycle command."""


# Shell-level command shapes that target the gateway lifecycle; each branch is anchored on a
# concrete command identifier so it fires only on command-shaped strings, never prose.
_GATEWAY_LIFECYCLE_PATTERN = re.compile(
    r"(?i)"
    # Branch A: destructive `hermes gateway` ops. `start` is excluded: starting from inside a
    # gateway is benign and a job may legitimately start a sibling profile. The lookbehind keeps
    # `hermes` from being a path component or word tail (`/docs/hermes gateway restart-notes.md`)
    # while every real command position (text start, whitespace, `;`/`&`/`|`, `$(`, backtick,
    # U+FFFD) still matches.
    # See #77173.
    # Windows spells the CLI with a launcher suffix (`hermes.exe`, npm-style `hermes.cmd`/`.ps1`);
    # same command, so the suffix is optional here.
    r"(?:(?<![/\w.\-])hermes(?:\.(?:exe|cmd|bat|com|ps1))?\s+gateway\s+(?:restart|stop|uninstall)\b)"
    # Branch B: launchctl ops anchored on a hermes-gateway label so unrelated hermes services stay
    # unblocked. `submit`/`bootstrap` register a NEW keepalive job wrapping an arbitrary helper (a
    # laundered restart); neutral-label submissions are caught by
    # `contains_launchctl_submit_command`. `bootout`/`remove`/`disable` are the
    # modern/legacy/durable forms of `unload`.
    # `submit` and `bootstrap` are included alongside the direct verbs (kickstart/etc.): `launchctl submit
    # -l ai.hermes.gateway-<suffix> -- <helper-script>` (or `launchctl bootstrap gui/<uid> <plist>`) creates
    # a NEW keepalive job wrapping an arbitrary helper, which is how a blocked direct restart/kill gets
    # laundered into a persistent restart loop instead (#62891) — same foot-gun, indirect shape.
    # Neutral-label submissions that dodge this text anchor are caught separately by
    # `contains_launchctl_submit_command` (execution-aware, label-independent). `bootout`/`remove`/`disable`
    # sit alongside `unload`: Apple deprecated load/unload in favour of bootstrap/bootout, so `bootout` is
    # the modern spelling of an already-listed verb, `remove` is its legacy sibling, and `disable` is what
    # makes an unload durable across boots. Omitting them left the bypassable approval layer
    # (tools/approval.py, skipped on force=True) as the only cover, while this hard block — documented as
    # "force=True cannot help here" — let them through (#80260).
    r"|(?:launchctl\s+(?:kickstart|unload|load|stop|restart|submit|bootstrap|bootout|remove|disable)\b[^\n]*\bhermes[.\-]?gateway)"
    # Branch C: systemctl ops on a hermes-gateway unit.
    r"|(?:systemctl\s+(?:-\S+\s+)*(?:restart|stop|start)\b[^\n]*\bhermes[.\-]?gateway)"
    # Branch D: pkill/kill of the gateway process, both token orders. Leading \b keeps "skill" from
    # matching as "kill".
    # `taskkill` / `Stop-Process` are the Windows spellings of the same operation; `\bp?kill\b`
    # cannot reach inside `taskkill`, so they are named outright. Service-control forms (`net stop`,
    # `sc stop`) presuppose a service install this guard has no evidence of and stay uncovered.
    r"|(?:\b(?:p?kill|taskkill|stop-process)\b[^\n]*\bhermes\b[^\n]*\bgateway)"
    r"|(?:\b(?:p?kill|taskkill|stop-process)\b[^\n]*\bgateway\b[^\n]*\bhermes)"
)

# Branch E: process killers whose TARGET is the interpreter image hosting the gateway. A supervised
# gateway is literally `python.exe` / `python3.12` (`python -m hermes_cli.main gateway run`), so
# `taskkill /F /IM python.exe`, `pkill -9 python3` or `killall python` carry no hermes/gateway token
# yet terminate it (#113667). Token-aware rather than a line regex: option VALUES are never read as
# targets (`pkill -u <user> chrome`), `-f` cmdline patterns are judged as patterns, and other image
# names (`taskkill /F /IM chrome.exe`) stay available. Numeric-PID kills are out of scope:
# the explicit PID / `proc_*` id IS the ownership-scoped route the rejection points to.
_INTERPRETER_IMAGE_RE = re.compile(r"^pythonw?(?:\d+(?:\.\d+)*)?(?:\.exe)?$")
_HOST_INTERPRETER_NAME = Path(sys.executable).name.lower() if sys.executable else ""
_KILLER_VALUE_OPTIONS = {
    # procps pkill/pgrep options that consume the next token, so the value is never the pattern.
    "pkill": frozenset({
        "-g", "--pgroup", "-G", "--group", "-P", "--parent", "-s", "--session", "-t", "--terminal",
        "-u", "--euid", "-U", "--uid", "-F", "--pidfile", "--ns", "--nslist", "-d", "--delimiter",
        "--signal", "-O", "--older", "-r", "--runstates", "--cgroup", "-A", "--ignore-ancestors",
    }),
    "killall": frozenset({"-s", "--signal", "-u", "--user", "-o", "--older-than", "-y", "--younger-than",
                          "-n", "--ns", "-Z", "--context"}),
}
_KILLER_VALUE_OPTIONS["pgrep"] = _KILLER_VALUE_OPTIONS["pkill"]
_KILLER_VALUE_OPTIONS["pidof"] = frozenset({"-o", "--omit-pid", "-S", "--separator"})
# Regex metacharacters a pkill/pgrep ERE may carry around the interpreter name (`^python3?$`, `python.*`).
_ERE_TAIL = re.compile(r"[.*+?\\\[\](){}|].*$")
_ERE_WILDCARD_ONLY = re.compile(r"^[.*+?$\s]*$")
_NAME_KILLERS = frozenset({"pkill", "killall", "taskkill", "stop-process"})
_NAME_ENUMERATORS = frozenset({"pgrep", "pidof", "get-process"})
_KILL_VERB_RE = re.compile(r"(?i)\b(?:kill|taskkill|stop-process)\b")
# A `-f` pattern that does not start with the interpreter reaches the gateway cmdline
# (`python -m hermes_cli.main gateway run` / `hermes gateway run`) only through its own tokens;
# an unrelated script that merely contains "hermes" (`hermes-polis/run.sh`, `my_hermes_bot.py`)
# cannot match it. Same hermes+gateway pairing as Branch D, plus the module path.
_GATEWAY_CMDLINE_TOKEN_RE = re.compile(r"(?i)hermes_cli|\bhermes\b[^\n]*\bgateway\b|\bgateway\b[^\n]*\bhermes\b")
# Rejection text for Branch E, shared by every tool surface that runs the guard so the agent is
# pointed at the ownership-scoped route (proc_* id / explicit PID) rather than the shell.
HOST_INTERPRETER_KILL_REJECTION = (
    "Blocked: this command kills every process whose image/name matches the Python "
    "interpreter, which is the process hosting this gateway (and this command). "
    "Stop only the process you own instead: process(action=\"kill\", session_id=\"proc_…\") "
    "for a background job Hermes started, or kill/taskkill by its explicit PID."
)


def _is_interpreter_image(value: str, *, substring: bool = False) -> bool:
    """True when *value* (a process/image name, `*`-wildcard allowed) denotes the Python interpreter
    that hosts the gateway. *substring*: pkill/pgrep match an ERE anywhere in the name, so `py`
    reaches `python3` too."""
    name = value.strip().strip("\"'").lower().removesuffix(".exe")
    if not name:
        return False
    if name.endswith("*"):
        prefix = name.rstrip("*")
        return "python".startswith(prefix) or _HOST_INTERPRETER_NAME.startswith(prefix)
    if _INTERPRETER_IMAGE_RE.match(name) or name == _HOST_INTERPRETER_NAME.removesuffix(".exe"):
        return True
    return substring and len(name) >= 2 and "python".startswith(name)


def _pattern_reaches_host_interpreter(pattern: str, *, full_cmdline: bool, exact: bool) -> bool:
    """pkill/pgrep/killall operand semantics: an ERE against the process NAME (or, with `-f`, the full
    command line). `python -m hermes_cli.main …` is the gateway's own cmdline, so a `-f` pattern
    that names the interpreter and then only wildcards or a `hermes` token reaches it, while
    `python mt_add.py` (a specific script) does not."""
    core = pattern.strip().strip("\"'").lstrip("^")
    core = core.removesuffix("$")
    head, _, rest = core.partition(" ") if full_cmdline else (core, "", "")
    # A literal interpreter name first (`python3.12`: the dot is a version separator, not an ERE
    # wildcard); only then read the head as an ERE with a metacharacter tail (`python3?`, `python.*`).
    match = None if _is_interpreter_image(head, substring=not exact) else _ERE_TAIL.search(head)
    if match:
        rest = head[match.start():] + " " + rest
        head = head[: match.start()]
    if not _is_interpreter_image(head, substring=not exact):
        return full_cmdline and bool(_GATEWAY_CMDLINE_TOKEN_RE.search(core))
    return not rest.strip() or bool(_ERE_WILDCARD_ONLY.match(rest)) or "hermes" in rest.lower()


def _killer_targets_host_interpreter(name: str, args: list[str]) -> bool:
    """Whether killer/enumerator *name* with argv *args* would select the host interpreter."""
    if name in ("pkill", "pgrep", "killall", "pidof"):
        # pkill/pgrep: ERE anywhere in the name unless -x; killall: exact name unless -r; pidof: exact.
        exact = name == "pidof" or (name == "killall") != any(t in ("-r", "--regexp", "-x", "--exact") for t in args)
        full_cmdline = name in ("pkill", "pgrep") and any(t in ("-f", "--full") for t in args)
        operands: list[str] = []
        position = 0
        while position < len(args):
            token = args[position]
            if token == "--":
                operands += args[position + 1:]
                break
            if token in _KILLER_VALUE_OPTIONS[name]:
                position += 2
                continue
            if not token.startswith("-"):
                operands.append(token)
            position += 1
        return any(_pattern_reaches_host_interpreter(op, full_cmdline=full_cmdline, exact=exact) for op in operands)
    if name == "taskkill":
        for position, token in enumerate(args[:-1]):
            option = token.lower().lstrip("/-")
            value = args[position + 1]
            if option == "im" and _is_interpreter_image(value):
                return True
            if option == "fi":
                filter_match = re.match(r"(?i)\s*['\"]?imagename\s+eq\s+(\S+)", value)
                if filter_match and _is_interpreter_image(filter_match.group(1)):
                    return True
        return False
    # Stop-Process / Get-Process: `-Name`/`-ProcessName` (also `-Name:value`), comma lists, positional
    # names for Get-Process.
    values: list[str] = []
    for position, token in enumerate(args):
        option, _, inline_value = token.partition(":")
        if option.lower() in ("-name", "-processname", "-n"):
            values.append(inline_value if inline_value else (args[position + 1] if position + 1 < len(args) else ""))
        elif name == "get-process" and not token.startswith("-") and (position == 0 or not args[position - 1].startswith("-")):
            values.append(token)
    return any(_is_interpreter_image(part) for value in values for part in value.split(","))


def _segment_names_host_interpreter(tokens: list[str], killers: frozenset[str]) -> bool:
    """Whether a tokenized segment runs one of *killers* against the host interpreter. The
    executable is read at the wrapper-peeled position first, then at the first killer token anywhere
    in the segment (`xargs kill`, Python argv lists)."""
    index = _executed_command_index(tokens)
    candidates = [index] if index is not None else []
    candidates += [i for i, token in enumerate(tokens) if _executable_name(token).lower().removesuffix(".exe") in killers]
    for position in candidates:
        name = _executable_name(tokens[position]).lower().removesuffix(".exe")
        if name in killers and _killer_targets_host_interpreter(name, tokens[position + 1:]):
            return True
    return False


def contains_host_interpreter_kill(text: str) -> bool:
    """Conservatively match when substitution nesting prevents a complete scan."""
    try:
        return _contains_host_interpreter_kill(text)
    except (SubstitutionNestingLimit, RecursionError):
        return True


def _contains_host_interpreter_kill(text: str) -> bool:
    """Branch E entrypoint: a process killer aimed at the interpreter image hosting the gateway, or a
    name-derived PID feed into one (`pgrep python | xargs kill`, `kill $(pidof python3)`,
    `Get-Process python | Stop-Process`). Segment-tokenized, so quoted/spliced spellings and Python
    argv lists resolve the same way the shell resolves them."""
    normalized = join_line_continuations(text)
    kill_verb_present = bool(_KILL_VERB_RE.search(normalized))
    for segment in _iter_command_segments(normalized):
        joined = " ".join(segment)
        stripped = _ARGV_LIST_PUNCTUATION.sub(" ", joined)
        # Python argv lists (`subprocess.run(["taskkill", "/IM", "python.exe"])`) re-split only when
        # list punctuation was present, so a quoted `-f 'python my_script.py'` stays one operand.
        for tokens in ([segment, stripped.split()] if stripped != joined else [segment]):
            if _segment_names_host_interpreter(tokens, _NAME_KILLERS):
                return True
            if kill_verb_present and _segment_names_host_interpreter(tokens, _NAME_ENUMERATORS):
                return True
    return False

# Every branch uses `[^\n]*` between verb and label so matches cannot span unrelated lines. A POSIX
# backslash-newline continuation is therefore collapsed to a space before matching (as the shell
# does) rather than loosening `[^\n]*`.
# Every branch above uses `[^\n]*` between its verb and the gateway identifier so the match can't span
# unrelated lines of a longer cron prompt/script, but that also means a real multi-line shell invocation
# split across continuation lines (e.g. `launchctl submit \` / `  -l ai.hermes.gateway-... \` / `  -- ...`,
# the exact reported shape in #62891) would otherwise slip past. Collapse continuations to a single space
# before matching, mirroring what the shell itself does, rather than loosening `[^\n]*` and risking false
# positives across genuinely separate lines. `join_line_continuations` (cron/lifecycle_guard_lines.py)
# does the collapse; a comment's trailing backslash continues nothing there, as in bash.

# Python argv-list punctuation (`subprocess.run(["launchctl", "bootout", ...])`) separates exec'd
# words with brackets/commas. Stripped only for the token-join re-scan, never from raw text.
# See #68289.
_ARGV_LIST_PUNCTUATION = re.compile(r"[\[\],]+")

# Branch A2: `hermes -p <profile> gateway restart|stop` (also `--profile <name>` /
# `--profile=<name>`). The selector breaks Branch A's adjacency. A sibling-profile restart is a
# legitimate fleet operation, so the profile name is captured and blocked only when it equals the
# profile running the guard. `start` stays excluded as in Branch A.
# Unlike Branch A this form is NOT unconditionally self-targeting: issued from inside gateway `zeus`,
# `hermes -p venus gateway restart` operates on a sibling profile's gateway and is a legitimate fleet
# operation. The pattern captures the named profile so `contains_gateway_lifecycle_command` can block only
# the self-targeting shape (named profile == the profile running the guard). See #78028.
_PROFILE_FLAG_LIFECYCLE_PATTERN = re.compile(
    r"(?i)"
    r"hermes\s+"
    # Each flag has one parse: '-' plus its remainder, never two ways to split
    # '--'. A following flag cannot also be an optional value (#129281).
    # Possessive token/space runs avoid repartitioning whitespace on failure;
    # whole flag groups can still backtrack to expose the profile selector.
    # Do not cap the number of flags: that would silently allow long self-stops.
    r"(?:-\S++(?:\s++(?!-\S)\S++)?\s++)*"
    # The selector: exactly the shapes the CLI's `_apply_profile_override` accepts.
    r"(?:--profile=([^\s]+)|(?:-p|--profile)\s+([^\s]+))"
    # Any global flags between the selector and the subcommand.
    r"(?:\s++-\S++(?:\s++(?!-\S)\S++)?)*"
    r"\s+gateway\s+(?:restart|stop)"
)

# Branch B needs the label AFTER the verb in one `[^\n]*` span; a loop that builds the label in an
# EARLIER `;`-segment (`label=${item%%:*}; launchctl bootout "gui/$uid/$label"`) leaves only
# `$label` next to the verb. These verbs act on an EXISTING job, so the hermes-gateway label anchor
# stays correct, but the check is "verb anywhere AND label anywhere".
# No profile identity available: cannot prove self-targeting, so do not block — sibling restarts must stay
# allowed (#78028).
_LAUNCHCTL_LIFECYCLE_VERBS_RE = re.compile(
    r"(?i)\blaunchctl\s+(?:kickstart|unload|load|stop|restart|bootout|kill|disable|remove)\b"
)
_HERMES_GATEWAY_LABEL_RE = re.compile(r"(?i)\bhermes[.\-]?gateway\b")

_SHELL_EXECUTABLES = frozenset({"sh", "bash", "dash", "ksh", "zsh"})
# bash long options that consume the NEXT token (`bash --rcfile FILE script`).
_SHELL_LONG_OPTIONS_WITH_VALUES = frozenset({"--rcfile", "--init-file"})
_SHELL_COMMAND_FLAGS = {"-c", "--command"}
_MAX_REFERENCED_SCRIPT_BYTES = 1024 * 1024
_MAX_REFERENCED_SCRIPT_DEPTH = 8

# Directory names directly under `Library` that mark a FileProvider-backed subtree: `Mobile
# Documents` is iCloud Drive; `CloudStorage` hosts third-party providers (Dropbox, OneDrive, Google
# Drive, ...).
_CLOUD_PLACEHOLDER_MARKERS = frozenset({"Mobile Documents", "CloudStorage"})

# Executables whose arguments are DATA (search patterns, SQL, log filters) and cannot execute their
# argument text. Deliberately conservative: no `awk` (system()), no `sed` (`s///e`), no
# `echo`/`printf` (routinely piped into a shell), no `mysql` (`\\!` and `system` escapes).
_DATA_SINK_EXECUTABLES = frozenset(
    {"grep", "egrep", "fgrep", "rg", "ag", "ack", "journalctl", "sqlite3", "psql"}
)
# Argument shapes that smuggle execution back INTO a data sink (command/process substitution, psql
# `\!`). Any hit disables masking for the whole segment — fail closed to the plain regex verdict.
_UNSAFE_DATA_ARG_MARKERS = ("`", "$(", "<(", ">(", "\\!")
# sqlite3 dot-commands (`.shell`, `.system`) also disable masking. Dot must be followed by a NAME
# character so relative paths (`.`, `./x`) stay paths.
_DOT_COMMAND_ARGUMENT = re.compile(r"^\.[A-Za-z]")
# A data sink piped into a shell/interpreter can feed matched lines to execution; never mask it.
_PIPE_TO_INTERPRETER = re.compile(
    r"\|\s*&?\s*(?:sudo\s+)?(?:(?:sh|bash|dash|ksh|zsh|xargs|eval|source)\b"
    r"|(?:python[\d.]*|pypy[\d.]*|py|uv)(?![\w.-]))"
)

# Bytes sniffed before reading a referenced file in full (see _BINARY_MAGICS).
_BINARY_SNIFF_BYTES = 4096

_ReadRemoteScriptFn = Callable[[str], Optional[str]]

# Wrappers that hand execution to their argument tail: the real command sits further right, so a
# first-token-only guard would let `sudo bash ~/restart.sh` / `sudo launchctl submit ...` walk past.
# A guard that reads only the first token sees `sudo`/`env`/`nohup` and never inspects what they run, so
# `sudo bash ~/restart.sh` walked past the same walk that stops `bash ~/restart.sh`, and `sudo launchctl
# submit ...` past the label-independent submit block (#62891). `_PIPE_TO_INTERPRETER` above already reads
# `sudo ` this way for the pipe case; this generalises that reading to the command position.
_TRANSPARENT_COMMAND_PREFIXES = frozenset({
    "sudo", "doas", "env", "nohup", "setsid", "nice", "ionice", "stdbuf",
    "timeout", "exec", "command", "builtin", "eatmydata",
    # time(1) (`/usr/bin/time x`, `command time x`); the `time` keyword is peeled separately.
    "time",
    # Privilege and namespace wrappers: options, then the command they run.
    "pkexec", "su", "runuser", "setpriv", "systemd-run", "nsenter", "unshare",
})

# Wrapper options that consume the NEXT token, so a value is never mistaken for the command.
_TRANSPARENT_PREFIX_VALUE_OPTIONS = {
    "exec": {"-a"},
    "sudo": {"-u", "-g", "-U", "-C", "-p", "-r", "-t", "-T", "--user", "--group", "--prompt"},
    "doas": {"-u", "-C"},
    "env": {"-u", "--unset", "-S", "--split-string", "-C", "--chdir"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "--class", "--classdata"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "time": {"-f", "--format", "-o", "--output"},
    "pkexec": {"--user"},
    "su": {"-s", "--shell", "-g", "--group", "-G", "--supp-group"},
    "runuser": {"-u", "--user", "-s", "--shell", "-g", "--group", "-G", "--supp-group"},
    "setpriv": {"--reuid", "--regid", "--groups", "--inh-caps", "--ambient-caps", "--bounding-set",
                "--selinux-label", "--apparmor-profile"},
    "systemd-run": {"-u", "--unit", "-p", "--property", "-E", "--setenv", "--slice",
                    "--description", "--uid", "--gid", "--on-calendar", "--service-type"},
    "nsenter": {"-t", "--target", "-S", "--setuid", "-G", "--setgid", "-r", "--root", "-w", "--wd"},
    "unshare": {"--map-user", "--map-group", "--setgroups", "-R", "--root", "-w", "--wd"},
}

# Wrapper options carrying a COMMAND STRING (shell source, re-scanned like `sh -c`); treating it as
# an opaque value would hide whatever it runs (`env -S 'bash ~/restart.sh'`).
_STRING_COMMAND_OPTIONS = {
    "env": ("-S", "--split-string"),
    "su": ("-c", "--command"),
    "runuser": ("-c", "--command"),
}

# Wrappers whose first non-option operand is a VALUE, not the command (`timeout 60 bash x.sh`).
_TRANSPARENT_PREFIX_OPERANDS = {"timeout": 1}

_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Python program runners whose file operand (or stdin) is scanned as Python source.
_PYTHON_RUNNER_RE = re.compile(r"^(?:pythonw?|pypy)(?:\d+(?:\.\d+)*)?(?:\.exe)?$|^py(?:\.exe)?$")
_PYTHON_SOURCE_SUFFIXES = (".py", ".pyw")

# Bound the walk: a pathological token run must not spin here.
_MAX_PREFIX_PEELS = 8

_BINARY_MAGICS = (
    b"\x7fELF",              # ELF — Linux/BSD executables and shared objects
    b"\xfe\xed\xfa\xce",     # Mach-O 32-bit
    b"\xfe\xed\xfa\xcf",     # Mach-O 64-bit
    b"\xce\xfa\xed\xfe",     # Mach-O 32-bit, byte-swapped
    b"\xcf\xfa\xed\xfe",     # Mach-O 64-bit, byte-swapped
    b"\xca\xfe\xba\xbe",     # Mach-O universal ("fat") binary
    b"MZ",                   # PE/COFF — Windows .exe/.dll
    b"!<arch>",              # static archive (.a)
    b"\x1f\x8b",             # gzip
    b"PK\x03\x04",           # zip (also .jar/.whl/.egg)
)


# --- profile identity -------------------------------------------------------------------------

def _current_profile_name() -> Optional[str]:
    """Profile running the guard (``hermes_cli.profiles.current_profile_name``); ``None`` if none."""
    from hermes_cli.profiles import current_profile_name

    return current_profile_name()


def _named_profile_is_current(named: str) -> bool:
    """True when *named* is the profile executing the guard. Without a profile identity
    self-targeting cannot be proven, so nothing is blocked (sibling restarts stay allowed)."""
    current = _current_profile_name()
    return bool(current) and named.strip().casefold() == current.strip().casefold()


# --- direct string scans ----------------------------------------------------------------------

def _contains_launchctl_gateway_lifecycle(normalized_text: str) -> bool:
    """Order-independent companion to Branch B — see the verbs regex comment."""
    return bool(_LAUNCHCTL_LIFECYCLE_VERBS_RE.search(normalized_text)) and bool(
        _HERMES_GATEWAY_LABEL_RE.search(normalized_text)
    )


def contains_gateway_lifecycle_command(text: str, *, mask_heredocs: bool = True) -> bool:
    """Return a conservative match if substitution nesting prevents a complete scan."""
    try:
        return _contains_gateway_lifecycle_command(text, mask_heredocs=mask_heredocs)
    except (SubstitutionNestingLimit, RecursionError):
        return True


def _contains_gateway_lifecycle_command(text: str, *, mask_heredocs: bool = True) -> bool:
    """Return True if *text* contains a gateway lifecycle command pattern. ``mask_heredocs=False``
    scans inert-looking heredoc bodies too (a session alias may shadow their consumer).

    Passes, in order: raw-text regex (the only pass that fires on inputs shlex cannot tokenize, e.g.
    Python source); profile-flag form; the same regex on each shell-tokenized segment with quotes/
    escapes resolved (closes splice bypasses like ``kick"start"`` / ``kick\\start``);
    order-independent launchctl pass. Single choke point for every recursion level of
    ``_contains_unsafe_gateway_action``.

    That second pass exists because a real shell resolves quote-splicing (``kick"start"``) and
    backslash-escaping (``kick\\start``) into one literal word — ``kickstart`` — before the command ever
    runs. The raw text still has the quote or backslash sitting between the verb's two halves, so the first
    pass alone lets a spliced verb reach ``launchctl``/``systemctl`` untouched while still executing as the
    blocked lifecycle command (#80269, reported against #80260's bootout parity fix). Tokenizing closes that
    gap while keeping the same gateway-label anchoring (``_GATEWAY_LIFECYCLE_PATTERN`` still requires a
    ``hermes``/``gateway`` token) — this function is the single choke point
    ``_contains_unsafe_gateway_action`` calls at every recursion level, so referenced-script and ``sh -c``
    payload scanning inherit the fix automatically.
    """
    if not text:
        return False
    # Provably inert heredoc bodies (quoted delimiter, data-sink consumer like `cat > f <<'EOF'`)
    # are documentation, not commands. The stripper fails open on ANY ambiguity (unquoted delimiter,
    # shell consumer, unterminated body), so executable heredocs are still scanned.
    # Heredoc bodies that are provably inert data (quoted delimiter, data-sink consumer like `cat > file
    # <<'EOF'`) are masked before scanning (#88336): a runbook line "a human can run: hermes gateway
    # restart" inside such a body is documentation, not a command this shell will execute.
    from tools.shell_heredoc import strip_inert_heredoc_bodies

    if mask_heredocs:
        text = strip_inert_heredoc_bodies(text)
    normalized = join_line_continuations(text)
    if _GATEWAY_LIFECYCLE_PATTERN.search(normalized):
        return True
    # Profile-flag form: blocked only when the named profile IS the one running the guard.
    # Profile-flag form (#78028): `hermes -p <profile> gateway restart|stop` bypasses Branch A because the
    # selector sits between `hermes` and `gateway`. It is only the same foot-gun when the named profile IS
    # the profile running the guard — sibling-profile restarts are legitimate fleet operations and stay
    # allowed.
    profile_match = _PROFILE_FLAG_LIFECYCLE_PATTERN.search(normalized)
    if profile_match:
        named = profile_match.group(1) or profile_match.group(2)
        # Profile ids cannot contain quotes (`^[a-z0-9][a-z0-9_-]{0,63}$`), so a shell-quoted
        # `-p 'zeus'` compares equal to the bare name.
        if named and _named_profile_is_current(named.strip().strip("\"'")):
            return True
    # Token-aware pass. Tokens are also re-joined with Python argv-list punctuation stripped, since
    # `subprocess.run(["launchctl", "bootout", ...])` separates argv words with commas/brackets.
    # Token-aware second pass (#80269): re-run the pattern on shell-tokenized segments where quotes/escapes
    # are resolved, closing splice bypasses like `kick"start"`. Runs after the profile-flag check so both
    # passes apply independently.
    for segment in _iter_command_segments(normalized):
        joined = " ".join(segment)
        if joined and _GATEWAY_LIFECYCLE_PATTERN.search(joined):
            return True
        stripped = _ARGV_LIST_PUNCTUATION.sub(" ", joined)
        if stripped != joined and _GATEWAY_LIFECYCLE_PATTERN.search(stripped):
            return True
    # The label may be built in an earlier `;`-segment, so no pass above sees verb + label together.
    # Order-independent launchctl pass (#77083): a shell loop can build the gateway label from a variable
    # defined in an earlier `;`-separated segment (`label=${item%%:*}; launchctl bootout
    # "gui/$uid/$label"`), so neither the same-span regex nor same-segment tokenization sees verb and label
    # together. Check "verb anywhere AND label anywhere" instead.
    # Branch E (#113667): killers aimed at the interpreter image itself carry no hermes/gateway token.
    return _contains_launchctl_gateway_lifecycle(normalized) or contains_host_interpreter_kill(normalized)


# Whole-walk work limits. The per-file cap and depth bound above limit one read, not the walk: a
# command can reference arbitrarily many scripts, and the pure-Python shlex pass (quadratic on a
# giant token) once held the GIL for minutes. These caps bound one whole walk and are charged
# BEFORE any text reaches shlex. Exhaustion fails closed (an unscanned script could hide a
# lifecycle command) and is logged at WARNING so an operator can tell it from a real block. Sizes
# sit well above any legitimate wrapper graph; remote reads are a backend roundtrip each, so they
# get a far tighter cap.
# See #78398.
_MAX_LIFECYCLE_SCAN_BYTES = _MAX_REFERENCED_SCRIPT_BYTES  # 1 MiB across the walk
_MAX_LIFECYCLE_SCAN_LINES = 16384
_MAX_LIFECYCLE_SCAN_LINE_BYTES = 64 * 1024
_MAX_LIFECYCLE_SCAN_PATHS = 1024
_MAX_LIFECYCLE_SCAN_REMOTE_READS = 64


class _LifecycleScanBudget:
    """Shared work budget for one complete referenced-script walk. ``refusal`` records why the walk
    failed closed for a reason other than a lifecycle command (budget, size, device, live SQLite,
    cloud placeholder) so the caller can tell the model the real reason (#113944)."""

    __slots__ = (
        "alias_names",
        "aliases_defined",
        "bytes_remaining",
        "integer_context",
        "lines_remaining",
        "mask_heredocs",
        "overrun",
        "paths_remaining",
        "refusal",
        "remote_reads_remaining",
        "separator_lines",
        "variable_candidates",
    )

    def __init__(self, session_aliases: Optional[frozenset[str]] = frozenset(), *,
                 separator_lines: int = 0) -> None:
        # Read the module constants at construction so tests/operators can lower them at runtime.
        self.bytes_remaining = _MAX_LIFECYCLE_SCAN_BYTES
        self.lines_remaining = _MAX_LIFECYCLE_SCAN_LINES
        self.paths_remaining = _MAX_LIFECYCLE_SCAN_PATHS
        self.remote_reads_remaining = _MAX_LIFECYCLE_SCAN_REMOTE_READS
        self.refusal: Optional[str] = None
        # Message-only: which limit the last refused ``charge_text`` hit, e.g. "has 17340 lines,
        # over the 16384-line scan cap". Never consulted for a verdict.
        self.overrun: Optional[str] = None
        # Message-only: newlines a caller added when joining the root text (cron's empty-prompt
        # join). Charged like any line, never reported as the text's own; spent on the first charge.
        from cron.lifecycle_guard_variables import VariableCandidates

        self.variable_candidates = VariableCandidates()
        self.separator_lines = separator_lines
        # Alias names that may expand in walked text: the ones the command's shell already holds
        # (*session_aliases*, see `scan_gateway_lifecycle`), or ``None`` when any name may be an
        # alias: unknown session names, or (sticky for the whole walk) any walked text (the
        # command, an eval payload, a sourced script) that may define one. An alias can make
        # `NAME ( … )` a keyword-led subshell; only a `(` whose opener holds one of these names
        # (or any `(`, when ``None``) may be one (t_01a1d565699c975b: a lone `alias ll='ls -l'`
        # once made `expanduser("~/.hermes/kanban.db")` an executed script).
        self.alias_names: Optional[frozenset[str]] = (
            session_aliases if isinstance(session_aliases, frozenset)
            and all(isinstance(name, str) for name in session_aliases) else None)
        # Sticky: some walked text may define or enable an alias (see `note_aliases`).
        self.aliases_defined = False
        self.integer_context = False  # Sticky possible integer attributes across recursive text.
        # False when a session alias may shadow a heredoc consumer, wrapper or shell name
        # (`alias python3=bash`): then no heredoc body is provably inert, so none is masked.
        self.mask_heredocs = not _session_aliases_shadow_consumers(session_aliases)

    def note_aliases(self, text: str) -> None:
        """Record (sticky) that walked *text* may define or enable an alias."""
        if not self.aliases_defined and _may_define_alias(text):
            self.aliases_defined = True
            self.alias_names = None

    def charge_text(self, text: str) -> bool:
        """Charge *text* before tokenization; False when it does not fit (``overrun`` says why)."""
        # UTF-8 is >= one byte per code point, so the char count is a free lower bound.
        if len(text) > self.bytes_remaining:
            self.overrun = _overrun("is at least", len(text), "byte", self.bytes_remaining, _MAX_LIFECYCLE_SCAN_BYTES)
            return False
        encoded = len(text.encode("utf-8", errors="replace"))
        if encoded > self.bytes_remaining:
            self.overrun = _overrun("is", encoded, "byte", self.bytes_remaining, _MAX_LIFECYCLE_SCAN_BYTES)
            return False
        separators, self.separator_lines = self.separator_lines, 0
        lines = text.count("\n") + 1
        if lines > self.lines_remaining:
            # Report the text's own lines (`wc -l` for a newline-terminated file), not the charged
            # count, which adds one for the last line and one per join separator.
            own = text.count("\n") - separators + (0 if text.endswith("\n") else 1)
            self.overrun = _overrun(
                "has", own, "line", self.lines_remaining, _MAX_LIFECYCLE_SCAN_LINES, charged=lines,
            )
            return False
        # One huge token is the quadratic shlex case; bound the longest physical line (chars, a
        # lower bound on bytes — tight enough for a DoS bound without a per-line encode).
        longest = max((len(line) for line in text.split("\n")), default=0)
        if longest > _MAX_LIFECYCLE_SCAN_LINE_BYTES:
            self.overrun = (
                f"has a {longest}-character line, over the "
                f"{_MAX_LIFECYCLE_SCAN_LINE_BYTES}-character per-line scan cap"
            )
            return False
        self.bytes_remaining -= encoded
        self.lines_remaining -= lines
        return True

    def charge_path(self) -> bool:
        """Charge one unique referenced path before any local/remote read."""
        if self.paths_remaining <= 0:
            return False
        self.paths_remaining -= 1
        return True

    def charge_remote_read(self) -> bool:
        """Charge one remote-backend read (a network roundtrip each)."""
        if self.remote_reads_remaining <= 0:
            return False
        self.remote_reads_remaining -= 1
        return True


def _capped_read_limit(max_bytes: Optional[int]) -> int:
    """Per-read byte cap: never above the per-file cap, never negative. One definition so local and
    remote reads cannot diverge.

    See #76762, #77703.
    """
    if max_bytes is None:
        return _MAX_REFERENCED_SCRIPT_BYTES
    return min(_MAX_REFERENCED_SCRIPT_BYTES, max(0, int(max_bytes)))


def lifecycle_scan_root_within_budget(text: str) -> bool:
    """Whether *text* may safely enter an optional tokenizer pass (``tools/terminal_tool.py`` gates
    its launchctl pre-scan on this). A FRESH budget, independent of the full guard's walk: the
    pre-scan may pass while the walk later exhausts, still fail-closed — only the friendlier
    launchctl diagnostic is lost. ``False`` is not a verdict: callers must still run the full guard."""
    try:
        return _LifecycleScanBudget().charge_text(text)
    except Exception:
        return False


def lifecycle_scan_budget_summary() -> str:
    """The walk caps in words, for the "must be scannable" sentence of every caller's refusal."""
    return (
        f"within the scan budget ({_format_size(_MAX_LIFECYCLE_SCAN_BYTES)}, "
        f"{_MAX_LIFECYCLE_SCAN_LINES} lines, {_format_size(_MAX_LIFECYCLE_SCAN_LINE_BYTES)} per line)"
    )


def _format_size(size: int) -> str:
    for unit, scale in (("MiB", 1024 * 1024), ("KiB", 1024)):
        if size % scale == 0:
            return f"{size // scale} {unit}"
    return f"{size} bytes"


def _overrun(
    verb: str, amount: int, unit: str, remaining: int, cap: int, *, charged: Optional[int] = None,
) -> str:
    """``has 17340 lines, over the 16384-line scan cap``, or ``over the N lines left of`` that cap
    when the text alone would fit but earlier text in the same walk already spent part of it.
    *charged* is the budget's own count when it differs from *amount*; it is named only when
    *amount* alone does not exceed what was left."""
    charged = amount if charged is None else charged
    counted = f" ({charged} counted)" if amount <= remaining else ""
    # Name the whole cap only when nothing was spent yet or *amount* alone exceeds it; a charged
    # count over the cap is not enough (a cap-line file after earlier text is over what is left).
    if amount > cap or remaining >= cap:
        return f"{verb} {amount} {unit}s{counted}, over the {cap}-{unit} scan cap"
    return f"{verb} {amount} {unit}s{counted}, over the {remaining} {unit}s left of the {cap}-{unit} scan cap"


def _budget_exhausted(
    budget: _LifecycleScanBudget, what: str, depth: int, *, source: Optional[str] = None,
) -> bool:
    """Fail closed. A ``text`` overrun names the text's *source* (a quoted script path, default
    "the command") and the cap ``charge_text`` recorded, or the agent cannot tell what to change."""
    if what == "text":
        if source:
            budget.refusal = f"{source} {budget.overrun} (text at depth {depth})"
        else:
            budget.refusal = (
                f"the scan budget was exhausted: the command {budget.overrun} (text at depth {depth})"
            )
    else:
        budget.refusal = f"the scan budget was exhausted ({what} at depth {depth})"
    logger.warning(
        "lifecycle guard scan budget exhausted: %s; "
        "failing closed — see _MAX_LIFECYCLE_SCAN_* in cron/lifecycle_guard.py",
        budget.refusal,
    )
    return True


def _depth_limit_reached(budget: _LifecycleScanBudget, depth: int, *, executed: bool) -> bool:
    """Only executed text turns an incomplete scan into a refusal."""
    if executed:
        budget.refusal = f"the scan depth limit was reached (depth {depth})"
    return executed


def _unreadable_reason(path: Path) -> str:
    """Name why an *executed* script failed closed without being scanned (live SQLite, device,
    oversized). Message-only: the fail-closed verdict itself came from the bounded reader."""
    from hermes_cli.sqlite_safe_read import has_live_connection

    if has_live_connection(path):
        return f"`{path}` is a SQLite database open in this gateway process"
    try:
        metadata = os.stat(path)
    except OSError:
        return f"`{path}` could not be read"
    if not stat.S_ISREG(metadata.st_mode):
        return f"`{path}` is not a regular file"
    return f"`{path}` is larger than the scan cap ({_MAX_REFERENCED_SCRIPT_BYTES} bytes) or the remaining walk budget"


def _refuse_unreadable(budget: _LifecycleScanBudget, path: Path, reason: str) -> bool:
    logger.warning("lifecycle guard cannot scan referenced script %s: %s; failing closed", path, reason)
    budget.refusal = reason
    return True


# --- shell tokenization -----------------------------------------------------------------------

def _split_segments(tokens: list[str], *, keep_controls: bool = False) -> Iterator[list[str]]:
    """Yield non-empty runs of *tokens* between control-operator tokens; with *keep_controls* each
    control token is also yielded as its own segment so the line can be rebuilt in order."""
    segment: list[str] = []
    for token in tokens:
        if token and set(token) <= _CONTROL_CHARS:
            if segment:
                yield segment
                segment = []
            if keep_controls:
                yield [token]
            continue
        segment.append(token)
    if segment:
        yield segment


def _iter_command_segments(command: str) -> Iterator[list[str]]:
    """Yield shell-tokenized command segments per logical line (see ``_iter_command_token_lists``)."""
    for tokens in _iter_command_token_lists(command):
        yield from _split_segments(tokens)


def _peel_reserved_words(segment: list[str]) -> int:
    """Shared keyword walk for command lists, printers and arithmetic name consumers."""
    from tools.shell_command_position import peel_reserved_words

    return peel_reserved_words(segment)


def _opens_call_arguments(segment: list[str]) -> bool:
    """Whether ``(`` follows an interpreter call, not a shell command prefix.

    Keywords are recognized in command position, including operands such as
    ``time -p`` and ``coproc name``. Merely inspecting the adjacent word mistakes
    these executable subshells for inert interpreter arguments. Conversely an
    ordinary command's argument named ``time`` is not a shell keyword.
    """
    if _peel_reserved_words(segment) == len(segment):
        return False
    return not segment[-1].endswith(("$", "<", ">", "="))


def _iter_segment_contexts(
    command: str, alias_names: frozenset[str] = frozenset(),
) -> Iterator[tuple[list[str], bool, list[str]]]:
    """``(segment, call_argument, piped_files)``: whether the segment is the argument list of a
    call (the segment right after a ``(`` that follows an ordinary word), and the files a ``cat``
    pipes into its stdin (``cat f.sh | bash``), through groups, compound commands and process
    substitution too (``{ cat f; } | bash``, ``cat f | if …; fi``, ``bash <(cat f)``), across
    lines; each heredoc body is walked alone. A ``(`` whose opener holds any word in
    *alias_names* is never a call: the alias may expand to a keyword (``alias t='time '``)."""
    def opens_call(segment: list[str]) -> bool:
        return _opens_call_arguments(segment) and alias_names.isdisjoint(segment)
    for script in heredoc_scripts(command):
        yield from iter_script_segment_contexts(
            join_continued_lines(_iter_command_token_lists(script)),
            cat_operands=_cat_operands, cat_relays_stdin=_cat_relays_stdin,
            opens_call_arguments=opens_call, peel_reserved_words=_peel_reserved_words,
            stdin_redirections=lambda words: _split_redirections(words)[1])


def _iter_positioned_command_segments(command: str) -> Iterator[tuple[list[str], bool]]:
    """``_iter_command_segments`` plus whether each segment is the argument list of a call."""
    for segment, call_argument, _ in _iter_segment_contexts(command):
        yield segment, call_argument


def _executable_name(token: str) -> str:
    """Command name of an executable token. ``Path(token).name`` is "" for ``.``, ``..`` and ``/``;
    the POSIX dot-source builtin is spelled ``.``, so fall back to the raw token or ``.
    ./helper.sh`` would escape the sourced-script scan."""
    return Path(token).name or token


def _peel_transparent_prefixes(segment: list[str], index: int, limit: int = _MAX_PREFIX_PEELS) -> int:
    """Index of the command a chain of at most *limit* wrappers executes. Unchanged if not a
    wrapper; may be ``len(segment)`` when a wrapper has no operand — callers must bounds-check."""
    for _ in range(limit):
        if index >= len(segment):
            return index
        name = _executable_name(segment[index])
        if name == "uv":
            uv_command = _peel_uv_run(segment, index)
            if uv_command is None:
                return index
            index = uv_command
            continue
        if name not in _TRANSPARENT_COMMAND_PREFIXES:
            return index
        value_options = _TRANSPARENT_PREFIX_VALUE_OPTIONS.get(name, frozenset())
        index += 1
        while index < len(segment):
            token = segment[index]
            if token == "--":
                # POSIX end-of-options: the command starts at the next token.
                index += 1
                break
            if token in value_options:
                index += 2
                continue
            if token.startswith("-") or _ENV_ASSIGNMENT.match(token):
                index += 1
                continue
            break
        for _ in range(_TRANSPARENT_PREFIX_OPERANDS.get(name, 0)):
            if index < len(segment) and not segment[index].startswith("-"):
                index += 1
    return index


def _command_token_index(segment: list[str]) -> Optional[int]:
    """Return the executable token index after simple env assignments."""
    return next((i for i, token in enumerate(segment) if not _ENV_ASSIGNMENT.match(token)), None)


def _executed_command_index(segment: list[str]) -> Optional[int]:
    """Index of the command a segment actually executes (env assignments and wrappers peeled)."""
    index = _command_token_index(segment)
    if index is None:
        return None
    index = _peel_transparent_prefixes(segment, index)
    return index if index < len(segment) else None


def _peel_uv_run(segment: list[str], index: int) -> Optional[int]:
    """Index of the command ``uv [opts] run [opts] <command>`` runs (may be ``len(segment)``);
    ``None`` when *index* is not such a ``uv`` (``uv pip ...``, ``uv tool run``)."""
    indices = _uv_run_command_indices(segment, index)
    return indices[0] if indices else None


def _uv_alternate_command_indices(segment: list[str]) -> list[int]:
    """Commands a ``uv run`` in *segment* may run besides the one ``_peel_uv_run`` picks (an
    unknown option read as taking a value), each peeled through further wrappers."""
    alternates: list[int] = []
    for position, token in enumerate(segment):
        if _executable_name(token) != "uv":
            continue
        for candidate in _uv_run_command_indices(segment, position)[1:]:
            peeled = _peel_transparent_prefixes(segment, candidate)
            if peeled < len(segment) and peeled not in alternates:
                alternates.append(peeled)
    return alternates


def _runs_under_uv(segment: list[str], index: int) -> bool:
    """Whether a wrapper chain reached ``segment[index]`` through ``uv run``."""
    return any(_executable_name(token) == "uv" and _peel_uv_run(segment, position) is not None
               for position, token in enumerate(segment[:index]))


def _split_redirections(arguments: list[str]) -> tuple[list[str], list[str]]:
    """Strip redirections and follow opened inputs through ordered fd copies/moves to stdin."""
    kept: list[str] = []
    opened: dict[str, str] = {}
    index = 0
    while index < len(arguments):
        match = redirection_parts(arguments[index], _REDIRECTION)
        index += 1
        if match is None:
            kept.append(arguments[index - 1])
            continue
        fd, operator, target = match
        if not target and index < len(arguments):
            target = arguments[index]
            index += 1
        fd = fd or ("0" if operator.startswith("<") else "1")
        source = target.removesuffix("-")
        provenance = opened.get(source) if operator in ("<&", ">&") else None
        opened.pop(fd, None)  # Every redirection replaces the destination's old binding.
        if operator in ("<", "<>") and target:
            opened[fd] = target
        elif provenance is not None:
            opened[fd] = provenance
        if operator in ("<&", ">&") and target.endswith("-"):
            opened.pop(source, None)  # A move copies first, then closes its source.
    return kept, [opened["0"]] if "0" in opened else []


def _cat_operands(segment: Optional[list[str]]) -> list[str]:
    """Files a ``cat`` segment writes to its stdout (its stdout piped on to a program reader),
    its own stdin included (``cat < f.py | python3``, ``cat - < f.py``, ``tee x < f.py``)."""
    for view in _command_segment_views(segment) if segment else []:
        index = _executed_command_index(view)
        if index is not None and (name := _executable_name(view[index])) in ("cat", "tee"):
            arguments, stdin = _split_redirections(view[index + 1 :])
            return [a for a in arguments if name == "cat" and not a.startswith("-")] + stdin
    return []


def _cat_relays_stdin(segment: list[str]) -> bool:
    """Whether a ``cat`` or ``tee`` segment writes the stdin it is fed (``… | cat | bash``,
    ``{ cat; } < f``, ``… | tee log | bash``): no redirection, heredoc or here-string of its own
    on fd 0 (``cat < x``, ``cat <<< x`` ignore the feed) and, for ``cat``, no file operand or a
    ``-`` one (``cat x.txt`` ignores it; ``tee``'s operands are files it writes)."""
    for view in _command_segment_views(segment):
        index = _executed_command_index(view)
        if index is not None and (name := _executable_name(view[index])) in ("cat", "tee"):
            words, arguments = iter(view[index + 1 :]), []
            for word in words:
                match = redirection_parts(word, _REDIRECTION)
                if match is None:
                    arguments.append(word)
                    continue
                fd, operator, target = match
                target = target or next(words, "")
                if fd in ("", "0") and operator.startswith("<") and (operator, target) != ("<&", "0"):
                    return False  # `cat < x`, `cat <<< x`, `cat <&3` read their own stdin
            return name == "tee" or "-" in arguments \
                or all(argument.startswith("-") for argument in arguments)
    return False


def contains_launchctl_submit_command(command: str) -> bool:
    """Return a conservative match if substitution nesting prevents a complete scan."""
    try:
        return _contains_launchctl_submit_command(command)
    except (SubstitutionNestingLimit, RecursionError):
        return True


def _contains_launchctl_submit_command(command: str) -> bool:
    """Detect an executed ``launchctl submit``/``bootstrap``, not quoted text.

    Label-independent by design: a NEW job's label is attacker-chosen, so a neutral name defeats any
    label-anchored regex. Both verbs register a persistent launchd job — never safe in the gateway.

    See #62891.
    """
    for segment in _iter_command_segment_views(command):
        index = _executed_command_index(segment)
        if index is not None and _executable_name(segment[index]) == "launchctl":
            arguments = segment[index + 1 :]
            if arguments and arguments[0].lower() in {"submit", "bootstrap"}:
                return True
    return False


_MAX_EXECUTES_NAMED_DEPTH = 4

# External time(1) options that consume the NEXT token (`env time -o log -f %e cmd`).
_EXTERNAL_TIME_VALUE_OPTIONS = frozenset({"-f", "--format", "-o", "--output"})


def _peel_external_time(segment: list[str], index: int) -> int:
    """Index of the command an external ``time`` runs. After an assignment or a wrapper
    (``x=1 time cmd``, ``env time cmd``) ``time`` is not the keyword but /usr/bin/time, which
    the reserved-word walk cannot see; unchanged when *segment[index]* is not ``time``. May be
    ``len(segment)`` — callers must bounds-check."""
    for _ in range(_MAX_PREFIX_PEELS):
        if index >= len(segment) or _executable_name(segment[index]) != "time":
            return index
        index += 1
        while index < len(segment):
            token = segment[index]
            if token == "--":
                index += 1
                break
            if token in _EXTERNAL_TIME_VALUE_OPTIONS:
                index += 2
                continue
            if token.startswith("-"):
                index += 1
                continue
            break
        index = _peel_transparent_prefixes(segment, index)
    return index


def _command_segment_views(segment: list[str]) -> list[list[str]]:
    """*segment* as written, then without its leading reserved words when it has any, then each
    of those with its command-position redirections moved just after the executable (see
    ``_hoist_command_redirections``). Callers read every view, so peeling only ever ADDS a
    reference. Without the peeled view `time ./x.sh`, `if true; then ./x.sh; fi` and
    `{ ./x.sh; }` read the keyword as the command and never open ``./x.sh``; without the hoisted
    one `< f.sh bash` and `bash<f.sh` read the redirection as the command.
    """
    peeled = segment[_peel_reserved_words(segment):]
    views = [segment, peeled] if peeled and len(peeled) != len(segment) else [segment]
    for view in list(views):
        hoisted = _hoist_command_redirections(view)
        if hoisted != view and hoisted not in views:
            views.append(hoisted)
    return views


def _hoist_command_redirections(segment: list[str]) -> list[str]:
    """*segment* with each redirection that sits where the command word is read (before it, or
    glued to it: `< f.py python3`, `sudo < f.sh bash`, `python3<f.py`) moved just after the
    executable, before any later redirections. Keep their left-to-right binding order while
    exposing the command. Only the command position is split: an argument such as
    ``bash -c 'python3<f.py'`` may be quoted program text, and its payload is re-scanned whole."""
    tokens = list(segment)
    moved: list[str] = []
    for _ in range(len(segment)):
        index = _command_token_index(tokens)
        if index is None:
            break
        index = _peel_transparent_prefixes(tokens, index)
        if index >= len(tokens):
            break
        word = tokens[index]
        width = _redirection_width(word)
        if not width:
            # `python3<f.py`: a word is the command up to its first `<`/`>`.
            glued = min((p for p in (word.find("<"), word.find(">")) if p >= 0), default=-1)
            if glued < 0:
                return tokens[:index + 1] + moved + tokens[index + 1:]
            tokens[index: index + 1] = [word[:glued], word[glued:]]
            index += 1
            width = _redirection_width(tokens[index])
        moved.extend(tokens[index: index + width])
        del tokens[index: index + width]
    return tokens + moved


def _redirection_width(word: str) -> int:
    """Words a redirection starting at *word* spans: 1 with a glued target (``<f``), 2 when the
    target is the next word (``< f``), 0 when *word* is not a redirection."""
    match = redirection_parts(word, _REDIRECTION)
    if match is None:
        return 0
    return 1 if match[2] else 2


def _iter_command_segment_views(command: str) -> Iterator[list[str]]:
    """Every ``_command_segment_views`` view of every segment of *command*."""
    for segment in _iter_command_segments(command):
        yield from _command_segment_views(segment)


def _shell_options_end(arguments: list[str], shell: str) -> tuple[int, bool, bool]:
    """``(operand_index, command_string, reads_stdin)`` for a POSIX *shell*'s *arguments*: the
    index of the first operand past the options, whether a ``-c`` option is among them (that
    operand is then the inline source, not a script), and whether a short-option cluster holds
    ``s`` (commands come from stdin). ``c`` counts anywhere in a ``-`` cluster (``-ec``, ``-lc``,
    ``-xc``, ``-ce``), and the options go on after it (``-c -e 'cmd'``) as the shell reads them.
    Each ``o`` in a short-option cluster (``-o x``, ``-euo pipefail``) consumes the next
    token, as does each ``O`` (``+O extglob``) except under zsh, whose ``-O`` is the valueless
    CORRECT flag; ``--rcfile`` and ``--init-file`` consume one too; a lone ``-`` ends the options
    like ``--``. Shells without a ``-O`` (dash) reject it, so reading its value there only skips
    a command that never runs."""
    value_letters = "o" if shell == "zsh" else "oO"
    index = 0
    reads_stdin = False
    command_string = False
    while index < len(arguments):
        argument = arguments[index]
        if argument in ("--", "-"):
            return index + 1, command_string, reads_stdin
        if argument in _SHELL_COMMAND_FLAGS:
            command_string = True
            index += 1
            continue
        if argument in _SHELL_LONG_OPTIONS_WITH_VALUES:
            index += 2
            continue
        if argument.startswith("--"):
            index += 1
            continue
        if argument.startswith(("-", "+")):
            if argument[0] == "-":
                reads_stdin = reads_stdin or "s" in argument[1:]
                command_string = command_string or "c" in argument[1:]
            index += 1 + sum(letter in value_letters for letter in argument[1:])
            continue
        break
    return index, command_string, reads_stdin


def _shell_script_operand(arguments: list[str], shell: str) -> Optional[str]:
    """The script a POSIX *shell* runs from *arguments*; ``None`` for ``-c`` (inline source) or no
    operand. Options and their values are skipped as ``_shell_options_end`` describes."""
    index, command_string, _ = _shell_options_end(arguments, shell)
    return arguments[index] if not command_string and index < len(arguments) else None


def executes_command_named(command: str, name: str, *, _depth: int = 0) -> bool:
    """Whether *command* EXECUTES a program whose basename is *name*: as a segment's command word
    (shell keywords such as ``if``/``then``/``time``/``!``, env assignments and wrappers such as
    sudo/nohup/systemd-run peeled), as the script a shell or Python interpreter runs, or inside an
    ``sh -c`` payload. A mere mention (``grep x name``) does not count. A command over the scan
    budget answers False, so callers must not use this as a refusal gate (it serves hints)."""
    if name not in command or not lifecycle_scan_root_within_budget(command):
        return False
    for raw_segment in _iter_command_segments(command):
        segment = raw_segment[_peel_reserved_words(raw_segment):]
        index = _executed_command_index(segment)
        if index is None:
            continue
        index = _peel_external_time(segment, index)
        if index >= len(segment):
            continue
        executable = _executable_name(segment[index])
        if executable == name:
            return True
        arguments = segment[index + 1 :]
        if executable in _SHELL_EXECUTABLES:
            script = _shell_script_operand(arguments, executable)
        elif _is_interpreter_image(executable):
            script = next((t for t in arguments if not t.startswith("-")), None)
        else:
            continue
        if script is not None and _executable_name(script) == name:
            return True
    if _depth >= _MAX_EXECUTES_NAMED_DEPTH:
        return False
    return any(executes_command_named(payload, name, _depth=_depth + 1)
               for payload in _iter_shell_command_payloads(command))


def _mask_data_sink_arguments(text: str) -> str:
    """Replace data-sink executables' arguments with a neutral placeholder.

    The regex cannot tell an EXECUTED lifecycle command from the same characters as *data* (a grep
    pattern, a SQL literal): every argument of a ``_DATA_SINK_EXECUTABLES`` segment becomes ``arg``
    and a match that survives is a real command. Strictly fail-closed: masking is skipped when the
    line pipes into a shell/interpreter, any argument carries an execution marker, or the line
    cannot be tokenized. Masking can only ALLOW what the plain regex would block, never block more.
    """
    lines_out: list[str] = []
    changed = False
    for line in text.splitlines() or [text]:
        if _PIPE_TO_INTERPRETER.search(line):
            lines_out.append(line)
            continue
        try:
            tokens = tokenize_redirections(line, _shlex_tokens, _REDIRECTION)
        except ValueError:
            lines_out.append(line)
            continue

        rebuilt: list[str] = []
        for segment in _split_segments(tokens, keep_controls=True):
            index = _command_token_index(segment)
            if index is not None and Path(segment[index]).name in _DATA_SINK_EXECUTABLES:
                arguments = segment[index + 1 :]
                if not any(
                    _DOT_COMMAND_ARGUMENT.match(argument)
                    or any(marker in argument for marker in _UNSAFE_DATA_ARG_MARKERS)
                    for argument in arguments
                ):
                    changed = True
                    rebuilt.extend(segment[: index + 1])
                    rebuilt.extend("arg" for _ in arguments)
                    continue
            rebuilt.extend(segment)
        lines_out.append(" ".join(rebuilt))
    return "\n".join(lines_out) if changed else text


def _lifecycle_command_scan_with_data_exemption(text: str, *, mask_heredocs: bool = True) -> bool:
    """Lifecycle scan exempting matches inside data arguments: cheap regex first (no-match pays
    nothing), then re-scan with data-sink arguments masked; only a surviving match blocks."""
    # Keep exhaustion typed inside the walk so scan_gateway_lifecycle retains its reason.
    if not _contains_gateway_lifecycle_command(text, mask_heredocs=mask_heredocs):
        return False
    normalized = join_line_continuations(text)
    return _contains_gateway_lifecycle_command(
        _mask_data_sink_arguments(normalized), mask_heredocs=mask_heredocs)


def _direct_lifecycle_scan(command: str, *, mask_heredocs: bool = True) -> bool:
    """Pure-string direct scans: lifecycle regex (data-exempted) + submit."""
    return (
        _lifecycle_command_scan_with_data_exemption(command, mask_heredocs=mask_heredocs)
        or contains_launchctl_submit_command(command)
    )


# --- path handling ----------------------------------------------------------------------------

def _resolve_lenient(path: Path) -> Path:
    """``path.resolve(strict=False)``, falling back to *path* on OSError (unreadable/long) or
    ValueError (embedded NUL from decoded binary tokenized as a path) — never crash the guard."""
    try:
        return path.resolve(strict=False)
    except (OSError, ValueError):
        return path


def _is_cloud_placeholder_path(path: Path) -> bool:
    """True for paths inside a macOS FileProvider-backed subtree. ``O_NONBLOCK`` does not make
    regular-file reads non-blocking, so opening an evicted placeholder can wait indefinitely for
    hydration before any command timeout starts: identify the boundary from the path alone."""
    parts = path.parts
    return any(
        parts[index - 1] == "Library" and part in _CLOUD_PLACEHOLDER_MARKERS
        for index, part in enumerate(parts)
        if index
    )


def _on_cloud_path(path: Path) -> bool:
    """Lexical OR resolved cloud check: covers direct cloud paths and local symlinks into one."""
    return _is_cloud_placeholder_path(path) or _is_cloud_placeholder_path(_resolve_lenient(path))


def _expand_candidate_path(candidate: str) -> Optional[Path]:
    """Sanitize a tokenized path candidate at the ingestion boundary. Tokens from shlex-splitting
    arbitrary (possibly binary-decoded) text can carry NUL or junk that each downstream ``Path`` op
    rejects differently (ValueError, RuntimeError when HOME is unset under launchd, OSError); reject
    once here. ``None`` = not a real path, nothing to scan.

    Every OS-facing ``Path`` operation downstream (``expanduser``, ``os.open``, ``resolve``) raises a
    *different* exception for the same junk (``ValueError: embedded null byte``, ``RuntimeError: Could not
    determine home directory`` when HOME is unset under launchd, OSError for over-long paths). Rejecting
    here — once, before any OS call — is the whole-class fix; catching per-syscall was the whack-a-mole that
    produced #76762, #77703, #77780, and #78256.
    """
    if not candidate or "\x00" in candidate:
        return None
    try:
        return Path(candidate).expanduser()
    except (ValueError, RuntimeError, OSError):
        return None


def _resolved_or_nothing(candidate: str, cwd: Optional[str]) -> Iterator[Path]:
    """Yield *candidate* anchored on *cwd* (or the process cwd) when it is a real path."""
    path = _expand_candidate_path(candidate)
    if path is None:
        return
    if not path.is_absolute():
        try:
            path = Path(cwd or Path.cwd()) / path
        except OSError:
            # Path.cwd() can raise when the process cwd was deleted.
            return
    yield path


def _resolve_script_path(script_path: str) -> Optional[Path]:
    """Resolve a cron ``script`` value the way ``cron.scheduler`` does (relative paths live under
    ``<HERMES_HOME>/scripts/``) so the guard scans the file that will actually run."""
    from hermes_constants import get_hermes_home

    raw = _expand_candidate_path(script_path)
    if raw is None:
        return None
    if raw.is_absolute():
        return raw
    try:
        return get_hermes_home() / "scripts" / raw
    except (RuntimeError, OSError):
        # get_hermes_home() falls back to Path.home(), which raises when neither HERMES_HOME nor
        # HOME is resolvable (launchd/systemd) — same ingestion contract: nothing to scan.
        return None


def _resolve_script_directory(script_path: str) -> Optional[str]:
    """Return the directory *script_path* resolves to, handling relative names."""
    try:
        path = _resolve_script_path(script_path)
        if path is not None and path.is_absolute():
            return str(path.parent)
    except Exception:
        pass
    return None


# --- referenced-script discovery --------------------------------------------------------------

def _iter_option_values(segment: list[str], start: int, option: str) -> Iterator[str]:
    """Yield values given to *option*, in both ``--opt v`` and ``--opt=v`` form."""
    prefix = option + "="
    for position in range(start + 1, len(segment)):
        token = segment[position]
        if token == option and position + 1 < len(segment):
            yield segment[position + 1]
        elif token.startswith(prefix):
            yield token[len(prefix):]


def _references_at(
    segment: list[str], index: int, cwd: Optional[str], piped: Optional[list[str]] = None,
) -> Iterator[Path]:
    """Yield the scripts the token at *index* executes, if any; *piped* are files a pipe feeds to
    its stdin, run by a shell given no script operand (or ``-s``)."""
    if index >= len(segment):
        return
    executable = segment[index]
    executable_name = _executable_name(executable)

    if executable_name in {".", "source"}:
        if len(segment) > index + 1:
            yield from _resolved_or_nothing(segment[index + 1], cwd)
        return

    if executable_name in _SHELL_EXECUTABLES:
        arguments, stdin = _split_redirections(segment[index + 1 :])
        operand_index, command_string, reads_stdin = _shell_options_end(
            arguments, executable_name)
        # An rc file a shell may source (`bash --rcfile x.sh -i`) is scanned like its script; past
        # the options (`bash clean.sh --rcfile x`) it is only the script's argument.
        for arg_index in range(min(operand_index, len(arguments)) - 1):
            if arguments[arg_index] in _SHELL_LONG_OPTIONS_WITH_VALUES:
                yield from _resolved_or_nothing(arguments[arg_index + 1], cwd)
        if command_string:
            return
        if operand_index < len(arguments):
            yield from _resolved_or_nothing(arguments[operand_index], cwd)
        else:
            reads_stdin = True
        if reads_stdin:
            for source in stdin or piped or []:
                yield from _resolved_or_nothing(source, cwd)
        return

    # A bare "/" is pathlib's division operator in Python sources, not an executable; resolving it
    # hits the filesystem root and fails the regular-file check, hard-blocking innocent .py scripts.
    if executable.strip("/") and ("/" in executable or executable.endswith((".sh", ".bash", ".zsh"))):
        yield from _resolved_or_nothing(executable, cwd)


def _iter_referenced_shell_scripts(
    command: str, *, cwd: Optional[str] = None, call_arguments: bool = False,
    alias_names: frozenset[str] = frozenset(),
) -> Iterator[Path]:
    """Yield scripts executed directly or through a POSIX shell. Each segment is read at the
    original token AND at the peeled wrapper target, both with and without leading reserved words
    (`time`, `!`, `then`, `{`, …) — additive on purpose: peeling must never REMOVE
    a reference (a local ``./timeout`` is a script, not the coreutils wrapper). With
    *call_arguments* the same discovery runs over call-argument segments instead
    (``expanduser("~/x")``): paths there are interpreter string literals, i.e. mentions.
    A ``(`` led by one of *alias_names* opens no call (see ``_iter_segment_contexts``)."""
    for segment, is_call_argument, piped in _iter_segment_contexts(command, alias_names):
        if is_call_argument != call_arguments:
            continue
        for view in _command_segment_views(segment):
            index = _command_token_index(view)
            if index is None:
                continue
            yield from _references_at(view, index, cwd, piped)
            peeled = _peel_transparent_prefixes(view, index)
            # `uv run x.py` runs Python: the interpreter scan reads it, never the shell walk (#77131).
            for target in [peeled, *_uv_alternate_command_indices(view)]:
                if target != index and not (
                        target < len(view) and view[target].endswith(_PYTHON_SOURCE_SUFFIXES)
                        and _runs_under_uv(view, target)):
                    yield from _references_at(view, target, cwd, piped)


# CPython short options that take a value (glued `-Wignore` or the next token), and the ones after
# which no script FILE runs (`-c` inline code, `-m` module).
_PYTHON_VALUE_SHORT_OPTIONS = frozenset("WX")
_PYTHON_NO_SCRIPT_SHORT_OPTIONS = frozenset("cm")
_PYTHON_VALUE_LONG_OPTIONS = frozenset({"--check-hash-based-pycs"})


def _python_script_operand(arguments: list[str]) -> Optional[str]:
    """The script file a CPython argv tail runs: ``-`` when the program is read from stdin (a
    ``-`` operand or none), ``None`` for ``-c``/``-m``. Short-option clusters are read like
    CPython's getopt (`-uc code` is ``-u -c``)."""
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            index += 1
            break
        if argument == "-" or not argument.startswith("-"):
            break
        if argument.startswith("--"):
            index += 2 if argument in _PYTHON_VALUE_LONG_OPTIONS else 1
            continue
        for position, flag in enumerate(argument[1:], start=1):
            if flag in _PYTHON_NO_SCRIPT_SHORT_OPTIONS:
                return None
            if flag in _PYTHON_VALUE_SHORT_OPTIONS:
                if position == len(argument) - 1:
                    index += 1  # the value is the next token
                break
        index += 1
    return arguments[index] if index < len(arguments) else "-"


def _iter_python_interpreter_scripts(command: str, *, cwd: Optional[str] = None) -> Iterator[Path]:
    """Yield files a segment runs as ``python3 [options] <file>`` (also ``py``/``pypy3``, behind
    wrappers and reserved words: ``do /usr/bin/env python3 <file>``, ``uv run [python] <file>``),
    or feeds a Python reading its program from stdin (``python3 < f``, ``cat f | python3``). An
    interpreter is never itself a wrapper, so only the executed command is read."""
    for raw_segment, _, piped in _iter_segment_contexts(command):
        for segment in _command_segment_views(raw_segment):
            primary = _executed_command_index(segment)
            if primary is None:
                continue
            for index in [primary, *_uv_alternate_command_indices(segment)]:
                under_uv = _runs_under_uv(segment, index)
                if segment[index].endswith(_PYTHON_SOURCE_SUFFIXES) and under_uv:
                    yield from _resolved_or_nothing(segment[index], cwd)
                    continue
                # `uv run -` runs the Python program on its stdin.
                stdin_program = segment[index] == "-" and under_uv
                if not stdin_program and not _PYTHON_RUNNER_RE.match(
                        _executable_name(segment[index]).lower()):
                    continue
                arguments, stdin = _split_redirections(segment[index + 1 :])
                operand = "-" if stdin_program else _python_script_operand(arguments)
                for source in (stdin or piped) if operand == "-" else [operand] if operand else []:
                    yield from _resolved_or_nothing(source, cwd)


def _python_source_is_unsafe(text: str, budget: _LifecycleScanBudget) -> bool:
    """Scan an interpreter-run Python file the way ``check_gateway_lifecycle`` scans a ``.py`` cron
    script: the lifecycle regex with data-sink masking, never the shell reference walk (a
    false-positive generator on Python sources, #77131). The masker tokenizes, so charge first;
    text over the budget is not scanned and not refused (see ``_walk_candidates``)."""
    return budget.charge_text(text) and _lifecycle_command_scan_with_data_exemption(text)


def _iter_shell_command_payloads(command: str) -> Iterator[str]:
    """Yield code passed through ``sh|bash|... -c`` (and ``su -c`` / ``env -S``) for recursive
    scanning; reserved words before the shell (`time bash -c …`) are peeled."""
    for segment in _iter_command_segment_views(command):
        index = _command_token_index(segment)
        if index is None:
            continue
        # Read at the ORIGINAL token: peeling past `su`/`env` would discard the command option.
        for option in _STRING_COMMAND_OPTIONS.get(_executable_name(segment[index]), ()):
            yield from _iter_option_values(segment, index, option)
        index = _executed_command_index(segment)
        if index is None:
            continue
        shell = _executable_name(segment[index])
        if shell not in _SHELL_EXECUTABLES:
            continue
        arguments = segment[index + 1 :]
        payload_index, command_string, _ = _shell_options_end(arguments, shell)
        if command_string:
            if payload_index < len(arguments):
                yield arguments[payload_index]
            continue
        # Kept from the plain `-c` read, fail closed: a `-c` word past the script operand
        # (`bash run.sh -c 'x'`) still has its next word scanned.
        for arg_index, argument in enumerate(arguments[:-1]):
            if argument in _SHELL_COMMAND_FLAGS:
                yield arguments[arg_index + 1]
                break


# --- referenced-script reading ----------------------------------------------------------------

def _has_binary_magic(data: bytes) -> bool:
    """True when *data* starts with a known compiled-binary signature. Deliberately narrower than
    "contains a NUL": ``bash`` still executes a NUL-bearing script, so a padded script must not
    bypass the scan. A shebang always wins (interpreted, never binary). Extensions are not
    consulted: a suffixless script must still be scanned and fail closed if oversized."""
    if data.startswith(b"#!"):
        return False
    return data.startswith(_BINARY_MAGICS)


def _read_referenced_script(
    path: Path, *, max_bytes: Optional[int] = None
) -> tuple[Optional[str], bool]:
    """Read a referenced script without racing SQLite connection lifecycle.

    The registry check must cover the complete ``open``/``read``/``close``
    sequence. A separate ``has_live_connection`` check would leave a race in
    which another thread opens SQLite after the check but before this function
    closes its descriptor, cancelling that connection's POSIX locks.
    """
    from hermes_cli.sqlite_safe_read import LiveConnectionError, offline_file_access

    try:
        with offline_file_access(path, what="read referenced script"):
            return _read_referenced_script_unlocked(path, max_bytes=max_bytes)
    except LiveConnectionError:
        return None, True
    except (OSError, ValueError):
        # Invalid path values, including embedded NULs, are not scripts.
        return None, False


def _read_referenced_script_unlocked(
    path: Path, *, max_bytes: Optional[int] = None
) -> tuple[Optional[str], bool]:
    """Return ``(text, unsafe)`` using bounded, regular-file-only reads.

    Shared choke point for every local script read, so the cloud-placeholder refusal lives here: a
    FileProvider path is never opened — not even to check hydration — because an evicted
    placeholder's ``open()`` can hang preflight. Lexical check: direct paths; resolved: symlinks.
    ``max_bytes`` lowers the per-file cap to what the calling walk can still afford.

    See #88052.
    """
    byte_limit = _capped_read_limit(max_bytes)
    if _on_cloud_path(path):
        return None, True
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except (OSError, ValueError):
        # OSError: unreadable/missing/over-long. ValueError: embedded NUL in *path*. Either is
        # "nothing to scan" — never crash the guard.
        return None, False
    try:
        # ValueError: an embedded NUL byte in *path* itself — a binary's decoded bytes tokenized into a
        # bogus script path by the recursion (#77703).
        metadata = os.fstat(descriptor)
        # Directories are not scripts. Docker Desktop writes ``fpath=(~/.docker/completions …)`` into
        # ``~/.zshrc``; the walk then treats that dir as a referenced script and used to fail-closed,
        # blocking ``source ~/.zshrc`` (#86753). Devices/sockets stay fail-closed.
        if not stat.S_ISREG(metadata.st_mode):
            # Directories are not scripts (`fpath=(~/.docker/completions …)` in ~/.zshrc must not
            # block `source ~/.zshrc`). Devices/sockets stay fail-closed.
            return None, not stat.S_ISDIR(metadata.st_mode)
        # Sniff a small prefix first: compiled binaries are never shell scripts, so skip them
        # WITHOUT reading the rest or feeding decoded garbage into the recursion.
        # Deliberately NOT keyed on the mere presence of a NUL byte (#77927): bash executes a text script
        # straight past an embedded NUL, so NUL-bearing text must fall through to the magic-number check +
        # NUL-strip below.
        data = os.read(descriptor, _BINARY_SNIFF_BYTES)
        if _has_binary_magic(data):
            return None, False
        # A regular file whose size already exceeds the cap fails closed without reading it (the
        # walk budget can be far below 1 MiB).
        if metadata.st_size > byte_limit:
            return None, True
        # Read the remainder (bounded); loop because os.read may return short.
        while len(data) <= byte_limit:
            chunk = os.read(descriptor, byte_limit + 1 - len(data))
            if not chunk:
                break
            data += chunk
    except OSError:
        return None, False
    finally:
        os.close(descriptor)
    if _has_binary_magic(data):
        return None, False
    # Size check BEFORE NUL stripping: stripping shrinks the buffer and would let an oversized file
    # slip under the threshold past this fail-closed branch.
    if len(data) > byte_limit:
        return None, True
    if b"\x00" in data:
        data = data.replace(b"\x00", b"")
    return data.decode("utf-8", errors="replace"), False


def _sanitize_remote_script_text(
    text: Optional[str], *, max_bytes: Optional[int] = None
) -> tuple[Optional[str], bool]:
    """Apply the local-read contract to text from an untrusted ``read_remote_script`` callback: NUL
    means binary (nothing to scan, checked first); oversized fails closed. Size compares re-encoded
    *bytes* (matching the ``head -c`` wire bound): a >1 MiB multibyte file truncated at the byte cap
    decodes to fewer chars, and a char count would scan instead of failing.

    The recursion boundary must not trust its callbacks: any backend (SSH, Modal, Daytona, or a future one)
    can hand back raw binary bytes decoded as text, or arbitrarily large output. Enforced here rather than
    inside each callback so the guarantee holds for every callback, not just the ones we hardened. See
    #76762, #77703.
    """
    if not text or "\x00" in text:
        return None, False
    byte_limit = _capped_read_limit(max_bytes)
    if len(text) > byte_limit:
        return None, True  # chars <= bytes: over the cap without encoding
    if len(text.encode("utf-8", errors="replace")) > byte_limit:
        return None, True
    return text, False


def _read_script_for_scanning(script_path: str) -> tuple[str, Optional[str]]:
    """``(text, refusal)``: read a cron script with the bounded scanner. Non-regular/oversized/live
    SQLite inputs fail closed with a NAMED *refusal* (never a lifecycle-shaped verdict); missing or
    unreadable paths stay empty so scheduler validation reports them."""
    resolved = _resolve_script_path(script_path)
    if resolved is None:
        return "", None
    script_text, unsafe = _read_referenced_script(resolved)
    if unsafe:
        return "", _unreadable_reason(resolved)
    return script_text or "", None


# --- recursive walk ---------------------------------------------------------------------------

def _walk_candidates(
    command: str, walk_command: str, *, cwd: Optional[str], executed: bool,
    budget: _LifecycleScanBudget,
) -> Iterator[tuple[Path, bool, bool]]:
    """``(path, executed, python_source)`` for every path *command* references, executed
    candidates first.

    Paths named only inside a masked body are still READ: an interpreter body that hands
    `/x/restart.sh` to os.system() executes it. Only the fail-closed verdicts (cloud placeholder,
    oversized/binary, budget) stay restricted to the executed view — a mere data mention must not
    trip them. Executed candidates come first so a mention never starves a real script's budget. A
    path in interpreter call syntax (`expanduser("~/x")`, `connect('/d/x.db')`) is a string
    literal the shell never runs: a mention too, read and scanned but never a fail-closed verdict,
    unless an alias may turn its `NAME (` into a subshell (`alias T=' '; T ( bash big.sh )`). A
    `(` led by a known session alias name is never a call (the executed view). Otherwise it is
    decided lazily, when the entry is reached: the executed candidates before it (a sourced
    script may define an alias, making every name a possible alias) have been walked by then.

    A file run as ``python3 <file>`` (masked or not) comes last, as ``python_source``: regex-scanned
    only, never walked as shell, and never failed closed. A file the scan cannot read (oversized, a
    stream such as ``/dev/stdin``, a device) ran before this scan existed, so only a lifecycle
    command actually found in it blocks.
    """
    names = budget.alias_names or frozenset()
    entries = [
        (path, executed, False)
        for path in _iter_referenced_shell_scripts(walk_command, cwd=cwd, alias_names=names)
    ]
    entries += [
        (path, executed, True)
        for path in _iter_referenced_shell_scripts(
            walk_command, cwd=cwd, call_arguments=True, alias_names=names)
    ]
    if walk_command != command:
        entries += [
            (path, False, False)
            for call_arguments in (False, True)
            for path in _iter_referenced_shell_scripts(
                command, cwd=cwd, call_arguments=call_arguments, alias_names=names)
        ]
    for path, candidate_executed, call_argument in entries:
        yield path, candidate_executed and (budget.alias_names is None or not call_argument), False
    # Python candidates are never executed (fail-closed) entries, so the unmasked text, a superset
    # of the masked view, finds them all.
    for path in _iter_python_interpreter_scripts(command, cwd=cwd):
        yield path, False, True


def _contains_unsafe_gateway_action(
    command: str, *, cwd: Optional[str], depth: int, visited: set[object], budget: _LifecycleScanBudget,
    read_remote_script: Optional[_ReadRemoteScriptFn] = None, executed: bool = True,
    source: Optional[str] = None, covered_quote_scope: Optional[tuple] = None,
    variable_source: bool = True, preserve_cwd: bool = False,
) -> bool:
    """``executed=False`` means *command* is the content of a file that is only MENTIONED in inert
    (masked) text: it is still scanned for a literal lifecycle command, but "could not scan" (budget,
    depth, size, device, live SQLite, cloud) is "nothing to scan" there, never a block (#113944).
    *source* names where *command* came from (a quoted script path), for budget refusals only."""
    # Charge BEFORE _direct_lifecycle_scan: every scan in it tokenizes with shlex.
    if not budget.charge_text(command):
        return _budget_exhausted(budget, "text", depth, source=source) if executed else False
    if _direct_lifecycle_scan(command, mask_heredocs=budget.mask_heredocs):
        return True
    budget.note_aliases(command)
    if depth >= _MAX_REFERENCED_SCRIPT_DEPTH:
        return _depth_limit_reached(budget, depth, executed=executed)

    def recurse(
        text: str, cwd: Optional[str], executed: bool, source: Optional[str], python_source: bool = False,
        nested: bool = False, covered_quotes: Optional[tuple] = None, variable_source: bool = True,
        preserve_source_cwd: bool = preserve_cwd,
    ) -> bool:
        if python_source:
            return _python_source_is_unsafe(text, budget)
        with shell_scope(budget, nested):
            return _contains_unsafe_gateway_action(
                text, cwd=cwd, depth=depth + 1, visited=visited, budget=budget,
                read_remote_script=read_remote_script, executed=executed, source=source,
                covered_quote_scope=covered_quotes, variable_source=variable_source,
                preserve_cwd=preserve_source_cwd,
            )

    # The walks below must see the same masked view `_direct_lifecycle_scan` sees (#110422): a
    # path or `sh -c` payload inside a provably-inert heredoc body is never shell-executed, and an
    # oversized data file mentioned there otherwise fails closed as a "script".
    from tools.shell_heredoc import strip_inert_heredoc_bodies

    walk_command = (strip_inert_heredoc_bodies(command, nested=True) if budget.mask_heredocs
                    else command)

    # A nested startup-file shell's input is walked first, with unknown alias names (nit 1).
    from cron.lifecycle_guard_nested_shells import (
        collect_nested_shell_inputs, refuse_unresolved_stdin, shell_scope, unvisited_scopes)

    from cron.lifecycle_guard_output import iter_printed_shell_payloads, quote_scope

    session_payloads = list(itertools.chain(_iter_shell_command_payloads(walk_command),
        (text for text, _raw in iter_printed_shell_payloads(
            walk_command, include_quoted=covered_quote_scope != quote_scope(cwd, executed, budget)))))
    nested_texts, nested_scripts, unresolved = collect_nested_shell_inputs(
        command, walk_command, cwd=cwd, budget=budget, session_payloads=session_payloads)
    if refuse_unresolved_stdin(budget, unresolved, executed=executed):
        return True
    from cron.lifecycle_guard_startup import scan_shell_inputs, script_working_directory

    if scan_shell_inputs(
            walk_command, nested_texts, cwd=cwd, executed=executed, budget=budget,
            covered_scope=covered_quote_scope, recurse=recurse, source=source, variable_source=variable_source):
        return True

    # A nested shell's script may be a path the ordinary candidates never name: a stdin file
    # redirected into `su -l bob`, `doas -s`, `sudo -i` or `ssh host` (review 5903452583 B1). It
    # comes from the executed view, so it is executed, and is walked first in both scopes.
    nested_only = ((path, executed, False) for path in sorted(nested_scripts))
    for script_path, candidate_executed, python_source in itertools.chain(nested_only, _walk_candidates(
            command, walk_command, cwd=cwd, executed=executed, budget=budget)):
        # Do not touch a FileProvider path even to discover whether the file is hydrated.
        if _on_cloud_path(script_path):
            if candidate_executed:
                return _refuse_unreadable(
                    budget, script_path,
                    f"`{script_path}` lives on a cloud-synced path (iCloud Drive / "
                    "~/Library/CloudStorage) that the guard refuses to open",
                )
            continue
        resolved = _resolve_lenient(script_path)
        scopes = unvisited_scopes(visited, budget, resolved, python_source=python_source,
                                  nested=resolved in nested_scripts, executed=candidate_executed,
                                  working_directory=str(cwd or Path.cwd()) if preserve_cwd else None)
        if not scopes:
            continue
        if not budget.charge_path():
            if candidate_executed:
                return _budget_exhausted(budget, "paths", depth)
            continue  # an alias may make a later call-argument entry executed
        visited.update(itertools.chain.from_iterable((key, key[:2]) for _nested, key in scopes))
        # Never read more than the walk can still afford to tokenize; a file larger than the
        # remainder fails closed exactly like an oversized one.
        script_text, unsafe = _read_referenced_script(script_path, max_bytes=budget.bytes_remaining)
        if unsafe:
            if candidate_executed:
                return _refuse_unreadable(budget, script_path, _unreadable_reason(script_path))
            continue
        if script_text is None and read_remote_script is not None:
            # Local path missing; the remote backend's output crosses the same trust boundary as a
            # local read — sanitize identically (binary skip + size fail-closed).
            if not budget.charge_remote_read():
                if candidate_executed:
                    return _budget_exhausted(budget, "remote reads", depth)
                continue
            script_text, unsafe = _sanitize_remote_script_text(
                read_remote_script(str(script_path)), max_bytes=budget.bytes_remaining
            )
            if unsafe:
                if candidate_executed:
                    return _refuse_unreadable(
                        budget, script_path,
                        f"`{script_path}` read from the backend exceeds the scan cap "
                        f"({_MAX_REFERENCED_SCRIPT_BYTES} bytes) or the remaining walk budget",
                    )
                continue
        if not script_text:
            continue
        # Startup sources retain the invoking shell's cwd; legacy script walks use the script directory.
        if any(recurse(
            script_text, script_working_directory(resolved, cwd, preserve_cwd), candidate_executed,
            f"`{script_path}`", python_source, nested=nested,
        ) for nested, _key in scopes):
            return True
    return False


def scan_gateway_lifecycle(
    command: str, *, cwd: Optional[str] = None,
    read_remote_script: Optional[_ReadRemoteScriptFn] = None,
    source: Optional[str] = None,
    separator_lines: int = 0,
    session_aliases: Optional[frozenset[str]] = frozenset(),
) -> tuple[bool, Optional[str]]:
    """``(unsafe, refusal)``: *refusal* names a non-lifecycle reason the walk failed closed (budget,
    size, device, live SQLite, cloud) so callers can tell the model; ``None`` when the verdict is a
    real lifecycle command or the command is allowed. *source* names the root text in a budget
    refusal (default "the command"); *separator_lines* counts newlines the caller added to join it,
    left out of a refusal's reported line count (the budget still charges them).

    *session_aliases* are the alias names the shell running *command* already holds before its
    first line, outside the walked text: the terminal backend sources a login snapshot holding
    ``alias -p`` and ``shopt -s expand_aliases`` (``tools/environments/base_session_env.py``).
    ``None`` means it may hold aliases with unknown names. A session alias name in the words before
    a ``(`` makes ``NAME ( … )`` possibly a keyword-led subshell (``alias t='time '``); unknown names
    make every such ``(`` one. A session alias that may rename a heredoc
    consumer, wrapper or shell (``alias python3=bash``) leaves every heredoc body visible.

    Total by construction: never raises. Direct scans are pure string ops; the referenced-script
    walk (filesystem, remote backends, shlex on decoded bytes) is best-effort defense-in-depth — an
    unexpected failure is logged and treated as "walk found nothing".

    This is the contract #76762 established ("a guarded path must never crash the guard") enforced at the
    boundary instead of per-syscall: a guard crash propagates out of ``tools/terminal_tool.py`` and breaks
    every terminal command until the gateway restarts (#77780, #78256), which is strictly worse than either
    verdict.
    """
    budget = _LifecycleScanBudget(session_aliases, separator_lines=separator_lines)
    try:
        unsafe = _contains_unsafe_gateway_action(
            command, cwd=cwd, depth=0, visited=set(), budget=budget,
            read_remote_script=read_remote_script, source=source,
        )
        return unsafe, budget.refusal if unsafe else None
    except (SubstitutionNestingLimit, RecursionError):
        # Frame walkers can exhaust Python's stack before the substitution budget.
        # Retrying the direct scanner would hit the same parser limit again.
        return True, 'the substitution nesting limit was reached'
    except Exception:
        logger.warning(
            "lifecycle guard referenced-script walk failed; "
            "falling back to direct-scan verdict",
            exc_info=True,
        )
        try:
            return _direct_lifecycle_scan(command, mask_heredocs=budget.mask_heredocs), None
        except Exception:
            # If even the data-argument masker fails, fall to raw regex + submit scan: stay total.
            return (contains_gateway_lifecycle_command(command, mask_heredocs=budget.mask_heredocs)
                    or contains_launchctl_submit_command(command)), None


def contains_gateway_lifecycle_command_or_referenced_script(
    command: str, *, cwd: Optional[str] = None,
    read_remote_script: Optional[_ReadRemoteScriptFn] = None,
) -> bool:
    """Detect lifecycle/submit commands, including bounded nested scripts (see
    ``scan_gateway_lifecycle`` for the contract)."""
    return scan_gateway_lifecycle(command, cwd=cwd, read_remote_script=read_remote_script)[0]


def check_gateway_lifecycle(prompt: Optional[str], script: Optional[str] = None) -> None:
    """Raise ``GatewayLifecycleBlocked`` if *prompt* or *script* contains a gateway-lifecycle
    command. The script is read from disk and concatenated with the prompt so a command cannot slip
    through by being split across the two. Callers let the ``ValueError``-shaped exception
    propagate."""
    combined = prompt or ""
    python_script = False
    refusal: Optional[str] = None
    source: Optional[str] = None
    # An empty prompt still gets the join newline below: charged, but not a line of the script.
    separator_lines = 0
    if script:
        source = f"the job prompt plus `{script}`" if prompt else f"`{script}`"
        resolved_script = _resolve_script_path(script)
        # Attribute the refusal correctly: not a lifecycle command, but a cloud path never opened.
        if resolved_script is not None and _on_cloud_path(resolved_script):
            raise GatewayLifecycleBlocked(
                # Attribute the refusal correctly: the script is not known to contain a lifecycle command —
                # it lives on a cloud-synced FileProvider path (iCloud Drive / ~/Library/CloudStorage) that
                # the guard refuses to open because an evicted placeholder can hang preflight indefinitely
                # (#88052). Fail closed with the real reason instead of implying a dangerous lifecycle
                # command.
                "Blocked: the cron script lives on a cloud-synced path "
                "(iCloud Drive / ~/Library/CloudStorage). Opening an "
                "evicted FileProvider placeholder can hang the guard's "
                "preflight scan indefinitely, so it is refused without "
                "being read. Move the script to a local, non-cloud path "
                "(e.g. ~/.hermes/scripts/) and recreate the job."
            )
        python_script = resolved_script is not None and resolved_script.suffix == ".py"
        script_text, refusal = _read_script_for_scanning(script)
        if script_text:
            separator_lines = 0 if combined else 1
            combined = f"{combined}\n{script_text}"

    if refusal:
        unsafe = True
    elif python_script:
        # Python runs via the interpreter, never a POSIX shell, and the shell reference walk is a
        # false-positive generator on Python sources (pathlib "/" resolves to the filesystem root).
        # The regex still scans the full text; non-regular/oversized files fail closed above (named refusal).
        # The data-exemption masker tokenizes with shlex, so it is charged against the walk budget.
        # The direct command regex below still scans the full text, so a literal `hermes gateway restart`
        # embedded in a .py script is still blocked. See #77131, #78398.
        budget = _LifecycleScanBudget(separator_lines=separator_lines)
        if not budget.charge_text(combined):
            unsafe = _budget_exhausted(budget, "text", 0, source=source)
            refusal = budget.refusal
        else:
            unsafe = _lifecycle_command_scan_with_data_exemption(combined)
    else:
        unsafe, refusal = scan_gateway_lifecycle(
            combined, cwd=_resolve_script_directory(script) if script else None, source=source,
            separator_lines=separator_lines,
        )
    if unsafe and refusal:
        raise GatewayLifecycleBlocked(
            f"Blocked: the lifecycle guard could not scan this cron job or referenced script: {refusal}. "
            "Nothing in the job is known to contain a gateway lifecycle command, but a script "
            f"the job executes must be scannable (a regular text file {lifecycle_scan_budget_summary()}) "
            "before it can run."
        )
    if unsafe:
        raise GatewayLifecycleBlocked(
            "Blocked: cron job contains a gateway lifecycle command or persistent "
            "launchctl submit operation. This is blocked to prevent agent-driven "
            "SIGTERM-respawn loops under launchd/systemd supervision "
            "(#30719). Run `hermes gateway restart` from a shell outside "
            "the running gateway instead."
        )
