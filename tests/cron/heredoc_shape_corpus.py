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

Real bash runs every case with a fake ``hermes`` that writes a marker. The property the test
asserts: when bash ran the payload and origin/main refuses the command, the tree under test
refuses it too. origin/main's verdicts are checked in (``main_verdicts.json``) with its sha.

CLI, run from the repo root with the tree under test first on PYTHONPATH:
    python3 tests/cron/heredoc_shape_corpus.py main-verdicts OUT.json   # regenerate the fixture
    python3 tests/cron/heredoc_shape_corpus.py report [MAIN_VERDICTS]   # count violations
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import random
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
    s_position: str  # "before" / "after" (relative to the option words), "bundled", "none"
    wrapper: str
    nesting: str
    payload: str


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
    return list(cases.values())


def _cover_keys(case: Case) -> set[tuple]:
    """Dimension values a case exercises, each with its ``-s`` placement and nesting form."""
    where = (case.s_position, case.nesting)
    words = [word for word in case.words if word != "-s"]
    return ({("option", word, *where) for word in words} | {("wrapper", case.wrapper, *where)}
            | {("shell", case.shell, case.payload, *where)})


def generate_cases(size: int = SAMPLE_SIZE) -> list[Case]:
    """A seeded sample of :func:`generate_full_corpus` that still covers every option word,
    wrapper, shell and payload with ``-s`` before, after, bundled and absent in every nesting
    form, plus every review row; the rest is filled at random up to *size*."""
    full = generate_full_corpus()
    order = list(full)
    random.Random(SEED).shuffle(order)
    chosen = {case.key: case for case in full if case.s_position == "review"}
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


def prepare(root: Path) -> dict[str, str]:
    """Fake ``hermes``/``ssh`` bin, a ``$HOME`` and ``./rc`` that rewrite stdin; return the env."""
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    for name in (".bash_profile", ".bashrc"):
        (home / name).write_text(REWRITE_STARTUP)
    (home / ".profile").write_text(REWRITE_STARTUP_POSIX)
    (root / "rc").write_text(REWRITE_STARTUP)
    (root / "evil").mkdir(exist_ok=True)
    evil_python = root / "evil" / "python3"
    evil_python.write_text('#!/bin/sh\nexec /bin/bash "$@"\n')
    hermes = root / "hermes"
    hermes.write_text('#!/bin/sh\nprintf called > "$MARKER"\n')
    ssh = root / "ssh"
    # Local SSH transport control; no network access.
    ssh.write_text('#!/bin/sh\nshift\nexec /bin/sh -c "$*"\n')
    for tool in (hermes, ssh, evil_python):
        tool.chmod(0o755)
    return {"PATH": f"{root}:/usr/bin:/bin", "FAKE_BIN": str(root), "HOME": str(home),
            "BASH_ENV": "", "ENV": str(home / ".profile"), "LANG": "C.UTF-8"}


def bash_runs_payload(root: Path, env: dict[str, str], case: Case) -> bool:
    """Run *case* in real bash (cwd *root*); True when the fake hermes wrote its marker."""
    marker = root / "markers" / case.key
    marker.parent.mkdir(exist_ok=True)
    try:
        subprocess.run(["/bin/bash"], input=case.command, text=True, capture_output=True,
                       timeout=20, check=False, cwd=root, env={**env, "MARKER": str(marker)})
    except subprocess.TimeoutExpired:
        pass
    return marker.exists() and marker.read_text() == "called"


def guard_refuses(root: Path, case: Case) -> bool:
    from cron.lifecycle_guard import contains_gateway_lifecycle_command_or_referenced_script

    return contains_gateway_lifecycle_command_or_referenced_script(case.command, cwd=str(root)) is True


def evaluate(root: Path, cases: list[Case]) -> list[tuple[Case, bool, bool]]:
    """``(case, bash ran the payload, guard refuses)`` for every case."""
    env = prepare(root)
    with ThreadPoolExecutor(max_workers=max(2, min(8, os.cpu_count() or 2))) as pool:
        ran = list(pool.map(lambda case: bash_runs_payload(root, env, case), cases))
    return [(case, executed, guard_refuses(root, case)) for case, executed in zip(cases, ran)]


def violations(results, main_refuses) -> list[Case]:
    """Cases bash ran, main refuses, and the guard under test allows."""
    return [case for case, executed, refused in results
            if executed and main_refuses(case) and not refused]


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
        print(f"cases={len(cases)} bash_ran={sum(r[1] for r in results)} "
              f"refused={sum(r[2] for r in results)} violations={len(bad)}")
        for case in bad:
            print(f"VIOLATION {case.key} {case.command!r}")
        if "--ran" in argv:
            for case, executed, refused in results:
                if executed:
                    print(f"RAN refused={refused} {case.shell} {case.words} {case.wrapper!r} "
                          f"{case.nesting} {case.payload}")
        return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
