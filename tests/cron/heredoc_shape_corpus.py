"""Generated shape corpus for the lifecycle guard's nested-heredoc masking.

Each case feeds a quoted heredoc to a stdin shell built from three dimensions (shell option
words with ``-s`` before, after or bundled; a stdin-passing wrapper; a nesting form). The shell's
body holds a nested ``python3 - <<'PY'`` whose own body is a walk-only gateway restart (a quoted
``bash -c`` payload only the referenced-script walk sees). That body is Python, and so inert,
only when the outer shell reads its stdin verbatim. Two payloads try to make it shell:

- ``startup``: ``$HOME``'s startup files and ``./rc`` rewrite stdin (``python3`` -> ``bash``),
  which login, interactive and ``--rcfile`` shells source;
- ``history``: ``timeout !!:1-3 python3 -`` becomes ``timeout 5 bash -s python3 -`` once
  history expansion is on.

A second family (``s_position == "session"``) runs top-level commands in a shell that already
holds aliases defined outside the walked text, the way the terminal backend sources its login
snapshot (``alias -p`` + ``shopt -s expand_aliases``) before ``eval``-ing the command:
keyword-prefix aliases (``alias t='time '``) turn ``t ( bash ./big.sh )`` into a subshell that
runs an unscannable script, and ``alias python3=bash`` runs a "Python" heredoc body as shell. The
guard is told the session's alias names (``scan_gateway_lifecycle(session_aliases=…)``).

A third family (``s_position == "sink"``) hands a data consumer's OUTPUT to a shell (``python3 -
<<'PY' | bash``, ``f | sudo bash``, ``eval "$(…)"``, ``. <(…)``, …) under every stdin wrapper and
nesting form: the printed ``bash ./big.sh`` (over the scan cap) or restart is shell code there.

A fourth family (``s_position == "redirect"``) puts a redirection word before the command word,
between a wrapper and its program, or among the program's operands: bash's ``{NAME}``
descriptor-variable forms (``{fd}>/dev/null bash ./restart.sh``) and numeric ``>&``/``>|`` words.
Each runs ``./restart.sh`` (refused whenever bash ran it) or a clean Python file (must stay
allowed: ``report`` counts those refusals as ``false_refusals``).

Real bash runs every case with a fake ``hermes`` that writes a marker. The property the test
asserts: when bash ran the payload and origin/main refuses the command, the tree under test
refuses it too; a sink case must be refused whenever bash ran it. origin/main's verdicts are
checked in (``main_verdicts.json``) with its sha.

Startup-file payloads need bash to source the test's ``$HOME`` files; ``startup_shell_prefix``
finds an argv prefix that makes it do so on this host (none, or bubblewrap masking the system
startup files), else those rows run unproven and the tests that need them skip.

CLI, run from the repo root with the tree under test first on PYTHONPATH:
    python3 tests/cron/heredoc_shape_corpus.py main-verdicts OUT.json   # regenerate the fixture
    python3 tests/cron/heredoc_shape_corpus.py report [MAIN_VERDICTS]   # count violations
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import itertools
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

SEED = 20261113
RANDOM_CASES = 600
# Cases the test runs (a covering, seeded sample of the full product): about a minute on a
# 4-vCPU runner. ``report --full`` runs every case.
SAMPLE_SIZE = 2400
SHELLS = ("bash", "sh")
# Allowlisted option words first, then every word that changes how stdin is read or sources a file.
OPTIONS = (
    "-e", "-u", "-x", "-o pipefail", "-o errexit", "--norc", "--noprofile", "-p",
    "-i", "-l", "--login", "-o history", "-o histexpand", "-o history -o histexpand",
    "-H -o history", "-O extglob", "-O expand_aliases", "-H", "-k", "-o keyword", "-o vi",
    "+o interactive-comments", "--rcfile ./rc", "--init-file ./rc", "-",
)
S_BUNDLES = ("-si", "-is", "-sl", "-ls", "-se", "-es")
WRAPPERS = ("", "env X=1 ", "timeout 5 ", "nice ", "nohup ", "command ", "stdbuf -o0 ")
NESTINGS = ("plain", "function", "subshell", "ssh", "outer")
_INNER = "python3 - <<'PY'\nbash -c 'hermes gateway \"restart\"'\nPY\n"
PAYLOADS = {
    "startup": _INNER,
    "history": ": 5 bash -s\ntimeout !!:1-3 " + _INNER,
    "wrapped": "set -u\ntimeout 5 env A=1 " + _INNER,
    "function": "q() { " + _INNER + "}\nq\n",
}
# The six real-Bash rows of review 5887281618 (outer opener, nesting, payload), kept verbatim.
REVIEW_ROWS = (
    ("bash -s -i", "plain", "startup"),
    ("bash -s -l", "plain", "startup"),
    ("bash -o history -o histexpand -s", "plain", "history"),
    ("bash -s -o history -o histexpand", "plain", "history"),
    ("bash -s -H -o history", "plain", "history"),
    ("bash -o history -o histexpand -s", "ssh", "history"),
)
# Aliases the command's shell holds before its first line (the backend's login snapshot).
SESSIONS = {
    "none": "",
    "time": "alias t='time '",  # review 5888336995 nit 2, verbatim
    "blank": "alias t=' '",
    "bang": "alias t='! '",
    "python": "alias python3=bash",
    "benign": "alias ll='ls -l'",
}
SESSION_PAYLOADS = {
    "alias-subshell": "t ( bash ./big.sh )\n",
    "alias-source": "t ( . ./big.sh )\n",
    "consumer": _INNER,
    "consumer-function": "q() { " + _INNER + "}\nq\n",
    "consumer-wrapped": "timeout 5 env A=1 " + _INNER,
}
SESSION_FORMS = ("plain", "function", "subshell")
# Third family (``s_position == "sink"``, review 5888336995 nit 3): a data consumer's OUTPUT is
# handed to a shell, so the line it prints is shell code. Consumers print one line; sinks pipe,
# ``eval``, process-substitute or source that output; nestings wrap the consumer. ``big`` prints
# ``bash ./big.sh`` (over the scan cap: the walk must fail closed); ``direct`` prints the restart.
# Review 5890784121: ``python-apostrophe`` holds a stray ``'`` in a double-quoted literal before
# the print (it must not shift the single-quote pairing; it is not printed, or bash would stop on
# the unterminated quote and never run the line); ``printf``/``echo`` are one-line producers
# with no heredoc (body ``None``): an escaped ``\n`` and unquoted words; ``echo-n`` puts an
# unprinted option word before the line (it must not be read as the executed command).
SINK_CONSUMERS = {
    "python": ("python3 - <<'PY'", "print('{line}')\n"),
    "cat": ("cat <<'PY'", "{line}\n"),
    "python-apostrophe": ("python3 - <<'PY'", "s = \"it's\"\nprint('{line}')\n"),
    "printf": ("printf '{line}\\n'", None),
    "echo": ("echo {line}", None),
    "echo-n": ("echo -n {line}", None),
    "printf-format": ("printf '%s ' {line}", None),  # the format is not printed as written
}
SINK_LINES = {"big": "bash ./big.sh", "direct": 'hermes gateway "restart"'}
SINKS = {
    "| bash": ("pipe", "bash"),
    "| sh -s": ("pipe", "sh -s"),
    "| sudo bash": ("pipe", "sudo bash"),
    "| ssh h bash": ("pipe", "ssh h bash"),
    'eval "$(…)"': ("wrap", 'eval "$( ', ' )"'),
    "bash <(…)": ("wrap", "bash <( ", " )"),
    ". <(…)": ("wrap", ". <( ", " )"),
}
SINK_NESTINGS = ("plain", "function", "function-keyword", "subshell", "group", "outer", "ssh")
# Fourth family (``s_position == "redirect"``, card t_01a1d5f876488f96): a redirection word where
# the command word is read or among its operands, ``{NAME}`` descriptor-variable forms first
# (``{fd}>/dev/null bash ./restart.sh`` allocates a descriptor into $fd and runs bash), then numeric
# ``>&``/``>|`` controls, whose ``&``/``|`` the tokenizer once read as a control operator. Places:
# before the wrapper, between wrapper and program, or after the program. Targets: three that run
# ``./restart.sh`` (operand, stdin, ``bash -s`` stdin) and ``clean``, a Python file that must stay
# allowed.
REDIRECT_PREFIXES = (
    "{fd}>/dev/null", "{fd}>&2", "{fd}>& 2", "{fd}> /dev/null", "{fd}>>./redir.log",
    "{fd}<./restart.sh", "{fd}<>./redir.log", "{fd}>|./redir.log", "{fd}<<<x", "{fd}<&0",
    "{a}>/dev/null {b}>&2", "{a[1]}>/dev/null", "{m[k]}>&2",
    # A subscript the tokenizer cuts at a space, glued to its operator (one bash word), and a
    # `{NAME[` head with no `]}>` tail: an ordinary word that must not end its command.
    "{a[`echo 1`]}>&2", "{a[x", "{a[1 2]}",
    "2>&1", ">&2", "2>|./redir.log",
)
# Prefixes above that are ordinary words, not redirections: bash runs ./restart.sh only when one
# is an operand of `bash -s` (elsewhere it is the command word or the script operand).
REDIRECT_PLAIN_WORDS = frozenset({"{a[x", "{a[1 2]}"})
# Substitution-bearing words exercise the same independent places/wrappers/nestings/targets.
SUBSTITUTION_PREFIXES = (
    '2>$(echo /dev/null)', '2>/dev/$(echo null)', '2>&$((1+1))',
    '{a[$((1+1))]}>/dev/null', '{a[$(echo 1)]}>/dev/null',
    '{a[`echo 1`]}>/dev/null', '2>`echo /dev/null`',
    '2>$(echo "$(echo /dev/null)")',
)
REDIRECT_PLACES = ("lead", "wrapped", "operand")
REDIRECT_TARGETS = {
    "operand": ("bash", "./restart.sh"),
    "stdin": ("bash", "< ./restart.sh"),
    "stdin-s": ("bash -s", "< ./restart.sh"),
    "clean": ("python3", "./clean.py"),
}
REDIRECT_NESTINGS = ("plain", "function", "subshell", "outer")
RESTART_SCRIPT = 'hermes gateway "restart"\n'
# Over the 1 MiB scan cap, so a walk that executes it fails closed; bash runs it to the end.
BIG_SCRIPT = "# padding\n" * 130000 + 'hermes gateway "restart"\n'
# Startup file: record that it ran, keep the fake bin on PATH, then rewrite the rest of stdin.
REWRITE_STARTUP = (
    'printf "ran HOME=%s\\n" "$HOME" >> "$FAKE_BIN/startup-ran"\n'
    'PATH="$FAKE_BIN:$PATH"\nunset BASH_ENV\nexec 0< <(sed s/python3/bash/)\n'
)
# ``.profile`` (what dash and a POSIX-mode bash read; dash cannot swap its own script fd)
# instead puts a ``python3`` that is really bash first on PATH.
REWRITE_STARTUP_POSIX = (
    'printf "ran HOME=%s\\n" "$HOME" >> "$FAKE_BIN/startup-ran"\n'
    'PATH="$FAKE_BIN/evil:$FAKE_BIN:$PATH"\nunset BASH_ENV\n'
)
FIXTURE = Path(__file__).parent / "fixtures" / "lifecycle_guard_heredoc" / "main_verdicts.json"


@dataclass(frozen=True)
class Case:
    key: str
    command: str
    shell: str
    words: tuple[str, ...]
    s_position: str  # "before" / "after" (option words), "bundled", "none", "review", "session"
    wrapper: str
    nesting: str
    payload: str
    session: str = ""  # a SESSIONS key (session cases only): aliases the shell already holds
    sink: str = ""  # a SINKS key (sink cases only): what runs the consumer's output
    place: str = ""  # a REDIRECT_PLACES entry (redirect cases only): where the redirection sits


def build_sink_command(sink: str, wrapper: str, nesting: str, consumer: str, line: str) -> str:
    """A consumer heredoc (behind *wrapper*) printing *line*, nested per *nesting*, whose output
    *sink* hands to a shell."""
    if nesting in ("outer", "ssh"):
        inner = build_sink_command(sink, wrapper, "plain", consumer, line)
        opener = "bash -s <<'OUTER'" if nesting == "outer" else "ssh h 'bash -s' <<'OUTER'"
        return f"{opener}\n{inner}OUTER\n"
    opener, body = SINK_CONSUMERS[consumer]
    if body is None:  # a one-line producer: no heredoc body
        opener, body = wrapper + opener.format(line=SINK_LINES[line]), ""
    else:
        opener, body = wrapper + opener, body.format(line=SINK_LINES[line]) + "PY\n"
    prelude = ""
    if nesting == "function":
        prelude, head, tail = f"f() {{\n{opener}\n{body}}}\n", "f", "\n"
    elif nesting == "function-keyword":  # the producer on the `function NAME {` line itself
        prelude, head, tail = f"function f {{ {opener}\n{body}}}\n", "f", "\n"
    elif nesting in ("subshell", "group"):
        start, end = ("(", ")") if nesting == "subshell" else ("{", "}")
        head, tail = f"{start}\n{opener}\n{body}{end}", "\n"
    else:
        head, tail = opener, "\n" + body
    kind, *parts = SINKS[sink]
    if kind == "pipe":
        return f"{prelude}{head} | {parts[0]}{tail}"
    return f"{prelude}{parts[0]}{head}{tail}{parts[1]}\n"


def build_redirect_command(prefix: str, place: str, wrapper: str, nesting: str, target: str) -> str:
    """*target*'s program run behind *wrapper* with the redirection *prefix* at *place*."""
    program, rest = REDIRECT_TARGETS[target]
    line = {
        "lead": f"{prefix} {wrapper}{program} {rest}",
        "wrapped": f"{wrapper}{prefix} {program} {rest}",
        "operand": f"{wrapper}{program} {prefix} {rest}",
    }[place]
    return {
        "plain": f"{line}\n",
        "function": f"f() {{\n{line}\n}}\nf\n",
        "subshell": f"(\n{line}\n)\n",
        "outer": f"bash -s <<'OUTER'\n{line}\nOUTER\n",
    }[nesting]


def session_alias_names(session: str) -> frozenset[str]:
    """Alias names a SESSIONS snapshot defines (what the backend reports to the guard)."""
    return frozenset(re.findall(r"alias ([^=\s]+)=", SESSIONS[session]))


def build_session_command(form: str, payload: str) -> str:
    body = SESSION_PAYLOADS[payload]
    return {"plain": body, "function": f"g() {{\n{body}}}\ng\n", "subshell": f"(\n{body})\n"}[form]


def build_command(shell: str, words: tuple[str, ...], wrapper: str, nesting: str, payload: str) -> str:
    opener = " ".join((wrapper + shell, *words)).strip()
    body = PAYLOADS[payload]
    if nesting == "ssh":
        return f"ssh h '{opener}' <<'R'\n{body}R\n"
    plain = f"{opener} <<'R'\n{body}R\n"
    return {
        "plain": plain,
        "function": f"f() {{\n{plain}}}\nf\n",
        "subshell": f"(\n{plain})\n",
        "outer": f"bash -s <<'OUTER'\n{plain}OUTER\n",
    }[nesting]


def generate_full_corpus() -> list[Case]:
    """Deterministic full corpus: the product of one option word with ``-s`` before or after it x
    wrapper x nesting x payload x shell; bundled ``-s``; ``-s``-less forms; the review rows; then a
    seeded set of two or three option words."""
    cases: dict[str, Case] = {}

    def add(shell, words, s_position, wrapper, nesting, payload):
        command = build_command(shell, tuple(words), wrapper, nesting, payload)
        key = hashlib.sha1(command.encode()).hexdigest()[:16]
        cases.setdefault(key, Case(key, command, shell, tuple(words), s_position, wrapper, nesting, payload))

    for opener, nesting, payload in REVIEW_ROWS:  # first, so the product keeps their label
        shell, *words = opener.split(" ", 1)
        add(shell, tuple(words), "review", "", nesting, payload)
    for shell, wrapper, nesting, payload in itertools.product(SHELLS, WRAPPERS, NESTINGS, PAYLOADS):
        for option in OPTIONS:
            add(shell, ("-s", option), "before", wrapper, nesting, payload)
            add(shell, (option, "-s"), "after", wrapper, nesting, payload)
        for bundle in S_BUNDLES:
            add(shell, (bundle,), "bundled", wrapper, nesting, payload)
    for turn, (shell, option, nesting, payload) in enumerate(
            itertools.product(SHELLS, ("", *OPTIONS), NESTINGS, PAYLOADS)):
        # No -s: a shell with no operand reads stdin too.
        add(shell, (option,) if option else (), "none", WRAPPERS[turn % len(WRAPPERS)], nesting, payload)
    rng = random.Random(SEED)
    for _ in range(RANDOM_CASES):
        options = rng.sample(OPTIONS, rng.randint(2, 3))
        form = rng.choice(("s", "s", "bundle", "none"))
        if form == "none":
            words, position = options, "none"
        else:
            index = rng.randint(0, len(options))
            words = options[:index] + ["-s" if form == "s" else rng.choice(S_BUNDLES)] + options[index:]
            position = "bundled" if form == "bundle" else "mixed"
        add(rng.choice(SHELLS), words, position, rng.choice(WRAPPERS), rng.choice(NESTINGS),
            rng.choice(tuple(PAYLOADS)))
    for session, form, payload in itertools.product(SESSIONS, SESSION_FORMS, SESSION_PAYLOADS):
        command = build_session_command(form, payload)
        # Old keys stay command-only; a session case's key also names its session.
        key = hashlib.sha1(f"session {session}\n{command}".encode()).hexdigest()[:16]
        cases.setdefault(key, Case(key, command, "bash", (), "session", "", form, payload, session))
    for sink, wrapper, nesting, consumer, line in itertools.product(
            SINKS, WRAPPERS, SINK_NESTINGS, SINK_CONSUMERS, SINK_LINES):
        command = build_sink_command(sink, wrapper, nesting, consumer, line)
        key = hashlib.sha1(command.encode()).hexdigest()[:16]
        cases.setdefault(key, Case(key, command, "bash", (), "sink", wrapper, nesting,
                                   f"{consumer}-{line}", sink=sink))
    for prefix, place, wrapper, nesting, target in itertools.product(
            (*REDIRECT_PREFIXES, *SUBSTITUTION_PREFIXES), REDIRECT_PLACES, WRAPPERS,
            REDIRECT_NESTINGS, REDIRECT_TARGETS):
        command = build_redirect_command(prefix, place, wrapper, nesting, target)
        key = hashlib.sha1(command.encode()).hexdigest()[:16]
        cases.setdefault(key, Case(key, command, "bash", (prefix,), "redirect", wrapper, nesting,
                                   target, place=place))
    return list(cases.values())


def _cover_keys(case: Case) -> set[tuple]:
    """Dimension values a case exercises, each with its ``-s`` placement and nesting form (a sink
    case: every sink x nesting x consumer x line, and every wrapper under each sink and nesting)."""
    if case.s_position == "sink":
        return {("sink", case.sink, case.nesting, case.payload),
                ("sink-wrapper", case.sink, case.wrapper, case.nesting)}
    if case.s_position == "redirect":  # every prefix with every place x target, wrapper, nesting
        prefix = case.words[0]
        return {("redirect", prefix, case.place, case.payload),
                ("redirect-wrapper", prefix, case.wrapper), ("redirect-nesting", prefix, case.nesting),
                ("redirect-form", case.wrapper, case.nesting, case.payload)}
    where = (case.s_position, case.nesting)
    words = [word for word in case.words if word != "-s"]
    return ({("option", word, *where) for word in words} | {("wrapper", case.wrapper, *where)}
            | {("shell", case.shell, case.payload, *where)})


def generate_cases(size: int = SAMPLE_SIZE) -> list[Case]:
    """A seeded sample of :func:`generate_full_corpus` that still covers every option word,
    wrapper, shell and payload with ``-s`` before, after, bundled and absent in every nesting
    form, plus every review row and every session case; the rest is filled at random up to *size*."""
    full = generate_full_corpus()
    order = list(full)
    random.Random(SEED).shuffle(order)
    chosen = {case.key: case for case in full if case.s_position in ("review", "session")}
    covered: set[tuple] = set()
    for case in order:
        keys = _cover_keys(case)
        if not keys <= covered:
            chosen.setdefault(case.key, case)
            covered |= keys
    for case in order:
        if len(chosen) >= size:
            break
        chosen.setdefault(case.key, case)
    return [case for case in full if case.key in chosen]


def proof_bash() -> str:
    """Use the requested PATH Bash, including for nested shells in the proof."""
    return shutil.which("bash") or "/bin/bash"


@functools.cache
def proof_bash_version() -> tuple[int, int]:
    result = subprocess.run(
        [proof_bash(), "--noprofile", "--norc", "-c",
         'printf "%s.%s" "${BASH_VERSINFO[0]}" "${BASH_VERSINFO[1]}"'],
        capture_output=True, text=True, encoding="utf-8", timeout=10, check=True,
    )
    major, minor = result.stdout.split(".")
    return int(major), int(minor)


def proof_path(root: Path) -> str:
    return f"{root}:{Path(proof_bash()).parent}:/usr/bin:/bin"


# These fixtures use only `timeout SECONDS COMMAND...`. The subprocess timeout
# remains the hard bound; this function supplies the wrapper's stdin/argv behavior
# on hosts without GNU timeout. Export it for the nested Bash history proofs.
TIMEOUT_PREAMBLE = (
    'if ! command -v timeout >/dev/null 2>&1; then\n'
    'timeout() { shift; "$@"; }; export -f timeout\nfi\n'
)


def prepare(root: Path) -> dict[str, str]:
    """Fake ``hermes``/``ssh`` bin, a ``$HOME`` and ``./rc`` that rewrite stdin, ``./big.sh`` and
    the session snapshots; return the env."""
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    for name in (".bash_profile", ".bashrc"):
        (home / name).write_text(REWRITE_STARTUP)
    (home / ".profile").write_text(REWRITE_STARTUP_POSIX)
    (root / "rc").write_text(REWRITE_STARTUP)
    (root / "big.sh").write_text(BIG_SCRIPT)
    (root / "restart.sh").write_text(RESTART_SCRIPT)
    (root / "clean.py").write_text("print('clean')\n")
    for session, aliases in SESSIONS.items():
        (root / f"session-{session}.sh").write_text(f"{aliases}\nshopt -s expand_aliases\n")
    (root / "evil").mkdir(exist_ok=True)
    evil_python = root / "evil" / "python3"
    evil_python.write_text(f'#!/bin/sh\nexec {shlex.quote(proof_bash())} "$@"\n')
    hermes = root / "hermes"
    hermes.write_text('#!/bin/sh\nprintf called > "$MARKER"\n')
    ssh = root / "ssh"
    # Local SSH transport control; no network access.
    ssh.write_text('#!/bin/sh\nshift\nexec /bin/sh -c "$*"\n')
    sudo = root / "sudo"
    sudo.write_text('#!/bin/sh\nexec "$@"\n')  # no privilege change: runs its operand
    for tool in (hermes, ssh, sudo, evil_python):
        tool.chmod(0o755)
    return {"PATH": proof_path(root), "FAKE_BIN": str(root), "HOME": str(home),
            "BASH_ENV": "", "ENV": str(home / ".profile"), "LANG": "C.UTF-8"}


def bash_input(case: Case) -> str:
    """What the outer bash reads: the command itself, or, for a session case, the backend's own
    shape (``tools/environments/base_session_env._wrap_command_script``): source the snapshot,
    then ``eval`` the command."""
    if not case.session:
        return case.command
    return (f"source ./session-{case.session}.sh >/dev/null 2>&1 || true\n"
            f"eval {shlex.quote(case.command)}\n")


def bash_runs_payload(root: Path, env: dict[str, str], case: Case,
                      prefix: tuple[str, ...] = ()) -> bool:
    """Run *case* in real bash (cwd *root*, under argv *prefix*); True when the fake hermes wrote
    its marker."""
    marker = root / "markers" / case.key
    marker.parent.mkdir(exist_ok=True)
    try:
        subprocess.run([*prefix, proof_bash()], input=TIMEOUT_PREAMBLE + bash_input(case), text=True,
                       capture_output=True, timeout=20, check=False, cwd=root,
                       env={**env, "MARKER": str(marker)})
    except subprocess.TimeoutExpired:
        pass
    return marker.exists() and marker.read_text() == "called"


def guard_refuses(root: Path, case: Case) -> bool:
    """The tree's verdict. A session case passes its alias names when the tree's guard takes them
    (origin/main's does not: it never sees the snapshot, which is main's real behavior)."""
    from cron import lifecycle_guard

    scan = lifecycle_guard.scan_gateway_lifecycle
    if case.session and "session_aliases" in inspect.signature(scan).parameters:
        return scan(case.command, cwd=str(root),
                    session_aliases=session_alias_names(case.session))[0] is True
    return lifecycle_guard.contains_gateway_lifecycle_command_or_referenced_script(
        case.command, cwd=str(root)) is True


def _startup_prefixes():
    """Candidate argv prefixes for ``startup_shell_prefix``: none, then bubblewrap with the system
    startup files masked (a runner image's ``/etc/profile*`` may keep a login bash from sourcing
    ``$HOME`` files; bwrap needs unprivileged user namespaces, which the CI job enables)."""
    yield "plain", ()
    bwrap = shutil.which("bwrap")
    if bwrap:
        args = [bwrap, "--unshare-user", "--dev-bind", "/", "/"]
        for path in ("/etc/profile", "/etc/bash.bashrc", "/etc/bashrc"):
            if os.path.isfile(path):
                args += ["--ro-bind", "/dev/null", path]
        if os.path.isdir("/etc/profile.d"):
            args += ["--tmpfs", "/etc/profile.d"]
        yield "bwrap with /etc/profile* masked", tuple(args)


@functools.cache
def startup_shell_prefix() -> tuple[tuple[str, ...] | None, str]:
    """``(argv prefix, how)`` under which ``HOME=<dir> bash -l -s`` sources ``<dir>/.bash_profile``
    on this host, or ``(None, diagnostics)`` when no candidate does (startup-file execution
    proofs are then unavailable here)."""
    notes = []
    with tempfile.TemporaryDirectory(prefix="heredoc-startup-probe-") as tmp:
        home, ran = Path(tmp) / "home", Path(tmp) / "ran"
        home.mkdir()
        (home / ".bash_profile").write_text('printf ok > "$PROBE"\n')
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "PROBE": str(ran), "LANG": "C.UTF-8"}
        probe = ('shopt -q login_shell && echo login; echo "HOME=$HOME"; '
                 'ls /etc/profile.d 2>/dev/null | tr "\\n" " "\n')
        for how, prefix in _startup_prefixes():
            ran.unlink(missing_ok=True)
            try:
                result = subprocess.run([*prefix, proof_bash(), "-l", "-s"], input=probe, text=True,
                                        capture_output=True, timeout=20, check=False, env=env)
            except (OSError, subprocess.TimeoutExpired) as exc:
                notes.append(f"{how}: {exc}")
                continue
            if ran.exists():
                return prefix, how
            notes.append(f"{how}: rc={result.returncode} out={result.stdout.strip()[-300:]!r} "
                         f"err={result.stderr.strip()[-300:]!r}")
    return None, "; ".join(notes)


def note_ci_skip(test_id: str, reason: str) -> None:
    """Append a skipped execution proof to ``$GITHUB_STEP_SUMMARY`` (run_tests.sh forwards it):
    the per-file runner keeps skip counts but drops reasons and warnings."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as summary:
            summary.write(f"- heredoc execution proof skipped (untested on CI): `{test_id}`: "
                          f"{reason}\n")


def evaluate(root: Path, cases: list[Case]) -> list[tuple[Case, bool, bool]]:
    """``(case, bash ran the payload, guard refuses)`` for every case."""
    env = prepare(root)
    prefix = startup_shell_prefix()[0] or ()
    with ThreadPoolExecutor(max_workers=max(2, min(8, os.cpu_count() or 2))) as pool:
        ran = list(pool.map(lambda case: bash_runs_payload(root, env, case, prefix), cases))
    return [(case, executed, guard_refuses(root, case)) for case, executed in zip(cases, ran)]


def violations(results, main_refuses) -> list[Case]:
    """Cases bash ran, main refuses, and the guard under test allows. A sink or redirect case must
    be refused whenever bash ran it, whatever main says: main allows most of them (the nit-3 gap,
    and card t_01a1d5f876488f96's `{fd}>x bash ./restart.sh`)."""
    return [case for case, executed, refused in results
            if executed and (case.s_position in ("sink", "redirect") or main_refuses(case))
            and not refused]


def false_refusals(results) -> list[Case]:
    """Redirect cases whose program is the clean Python file, refused anyway: a redirection word
    must not turn an allowed command into a refusal."""
    return [case for case, _executed, refused in results
            if case.s_position == "redirect" and case.payload == "clean" and refused]


def corpus_digest(cases: list[Case]) -> str:
    """Identity of a corpus: the checked-in main verdicts are valid only for this exact case set."""
    return hashlib.sha256("\n".join(sorted(case.key for case in cases)).encode()).hexdigest()


def load_main_verdicts(path: Path = FIXTURE):
    """``(fixture, main_refuses(case))`` from the checked-in origin/main verdicts. The fixture lists
    the few keys main allows, so every other full-corpus case is a main refusal."""
    fixture = json.loads(path.read_text())
    allowed = set(fixture["main_allows"])
    return fixture, lambda case: case.key not in allowed


def _main(argv: list[str]) -> int:
    full = generate_full_corpus()
    cases = full if "--full" in argv else generate_cases()
    paths = [arg for arg in argv[1:] if not arg.startswith("--")]
    with tempfile.TemporaryDirectory(prefix="heredoc-corpus-") as tmp:
        root = Path(tmp)
        if argv[:1] == ["main-verdicts"]:
            prepare(root)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                                 check=True, cwd=os.environ.get("CORPUS_MAIN_TREE", ".")).stdout.strip()
            allows = sorted(case.key for case in full if not guard_refuses(root, case))
            Path(paths[0]).write_text(json.dumps(
                {"main_sha": sha, "seed": SEED, "cases": len(full), "corpus_digest": corpus_digest(full),
                 "main_allows": allows}, indent=1, sort_keys=True) + "\n")
            print(f"{len(full)} cases, main {sha}: {len(full) - len(allows)} refused")
            return 0
        _fixture, main_refuses = load_main_verdicts(Path(paths[0]) if paths else FIXTURE)
        results = evaluate(root, cases)
        bad = violations(results, main_refuses)
        clean = false_refusals(results)
        print(f"cases={len(cases)} bash_ran={sum(r[1] for r in results)} "
              f"refused={sum(r[2] for r in results)} violations={len(bad)} "
              f"false_refusals={len(clean)}")
        for case in bad:
            print(f"VIOLATION {case.key} {case.command!r}")
        for case in clean:
            print(f"FALSE-REFUSAL {case.key} {case.command!r}")
        if "--ran" in argv:
            for case, executed, refused in results:
                if executed:
                    print(f"RAN refused={refused} {case.shell} {case.words} {case.wrapper!r} "
                          f"{case.nesting} {case.payload}")
        return 1 if bad or clean else 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
