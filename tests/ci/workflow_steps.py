"""Local receipts for the few safe routing steps, NOT a hosted workflow runner.

Execute lane/selection scripts; replace only pytest/Playwright launch boundaries
with argv receipts. No setup actions, installs, E2E tests or native hosts run here.
"""
from __future__ import annotations

import fnmatch
import functools
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from hermes_platform.resolver import LookupContext, locate_command
from tests.ci import _gha_expr as gha


# Intercept launch/activation boundaries, not selection logic. The unit lane
# need not have the upgrade job's venv; the Desktop monitor must not outlive us.
_RECEIPTS = r'''
record() {
  "$REPLAY_PYTHON" -c 'import json, os, sys; open(os.environ["REPLAY_CALLS"], "a").write(json.dumps(sys.argv[1:]) + "\n")' "$@"
}
source() { [[ "$1" == .venv/bin/activate ]]; }
scripts/run_tests.sh() { record pytest "$@"; }
pnpm() { record pnpm "$@"; }
xvfb-run() { shift 2; "$@"; }
sleep() { return 1; }
'''


# Workflows use Blacksmith's drop-in checkout (same inputs, cached on its runners;
# it also works on the remaining GitHub-hosted jobs); a few still use actions/checkout.
CHECKOUT_ACTIONS = ("actions/checkout@", "useblacksmith/checkout@")


def is_checkout(step: dict) -> bool:
    return str(step.get("uses", "")).startswith(CHECKOUT_ACTIONS)


def required(step: dict, ctx: dict) -> None:
    label = step.get("name", step.get("id", "required step"))
    assert gha.condition(step.get("if"), ctx), f"{label}: disabled"
    assert not gha.truthy(gha.render(step.get("continue-on-error", False), ctx)), f"{label}: advisory"


def _interpreter_dirs(root: Path) -> tuple[str, ...]:
    """The replay interpreter's dir, plus shims for the names setup-python puts on PATH.

    Steps call ``python3`` (Linux runners) or ``python`` (Windows runners); a
    Windows venv only ships ``python.exe``, so a missing name would otherwise
    hit an unrelated host interpreter on os.defpath, or none at all.
    """
    interpreter = Path(sys.executable).parent
    shims = root / "bin"
    shims.mkdir()
    for name in ("python", "python3"):
        if not locate_command(name, LookupContext(path=str(interpreter))).found:
            shim = shims / name
            shim.write_text('#!/bin/sh\nexec "$REPLAY_PYTHON" "$@"\n', encoding="utf-8")
            shim.chmod(0o755)
    return str(interpreter), str(shims)


#: Where a bash 4+ lives when PATH's is too old: macOS ships /bin/bash 3.2 and the
#: replayed steps use ``mapfile`` (bash 4+), so Homebrew's (Apple Silicon, then Intel).
BASH4_KNOWN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


def _bash_major(bash: str) -> int | None:
    """``BASH_VERSINFO[0]`` of ``bash``, or None when it does not run or answer."""
    try:
        probe = subprocess.run(
            [bash, "--noprofile", "--norc", "-c", 'echo "${BASH_VERSINFO[0]}"'],
            stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace",
            timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    answer = probe.stdout.strip()
    return int(answer) if probe.returncode == 0 and answer.isdigit() else None


def find_replay_bash(ctx: LookupContext | None = None,
                     known_dirs: tuple[str, ...] | None = None) -> tuple[str | None, str]:
    """(first bash 4+: PATH's, else one in ``known_dirs``; or None, what was found).

    The steps run in CI under Linux's bash 5 and Git for Windows' bash 5; only a
    stock Mac has a bash too old for them, which used to fail every replay with
    ``mapfile: command not found``.
    """
    known_dirs = BASH4_KNOWN_DIRS if known_dirs is None else known_dirs
    seen = []
    for candidate in locate_command("bash", ctx, known_dirs=known_dirs).present:
        if candidate.value in (path for path, _ in seen):
            continue
        major = _bash_major(candidate.value)
        if major is not None and major >= 4:
            return candidate.value, ""
        seen.append((candidate.value, major))
    found = ", ".join(f"{path} (bash {major if major is not None else 'unknown version'})"
                      for path, major in seen) or "no bash at all"
    return None, (f"replayed workflow steps need bash >= 4 (they use mapfile); found {found}; "
                  f"install one on PATH or in {' or '.join(known_dirs)}")


@functools.cache
def _replay_bash_cached(path: str | None, known_dirs: tuple[str, ...]) -> tuple[str | None, str]:
    return find_replay_bash(LookupContext(path=path), known_dirs)


def replay_bash() -> str:
    """The bash to replay steps with, or skip the test saying which (too old) bash was found.

    One probe per (PATH, known dirs): steps are replayed hundreds of times per file.
    """
    bash, reason = _replay_bash_cached(os.environ.get("PATH"), BASH4_KNOWN_DIRS)
    if bash is None:
        # Lazy: steps are also replayed from bare interpreters with no pytest installed.
        try:
            import pytest
        except ImportError:
            raise AssertionError(reason) from None
        pytest.skip(reason)
    return bash


def _run(step: dict, ctx: dict, cwd: Path | None = None, *, receipt: bool = False) -> tuple[dict, list]:
    required(step, ctx)
    assert step.get("shell", "bash") == "bash", "only safe Bash selection steps are replayed"
    with tempfile.TemporaryDirectory(prefix="workflow-replay-", ignore_cleanup_errors=True) as directory:
        root = Path(directory)
        output, calls = root / "outputs", root / "calls"
        output.touch()
        calls.touch()
        bash = replay_bash()
        # Steps call git too. Linux reaches it through os.defpath; on Windows the bash found
        # first may be Git's usr\bin (no git.exe), so git's own dir rides along like bash's.
        git = locate_command("git").command[0]
        env = {
            "PATH": os.pathsep.join((*_interpreter_dirs(root), os.defpath, str(Path(bash).parent),
                                     str(Path(git).parent))),
            "HOME": directory, "RUNNER_TEMP": directory, "GITHUB_OUTPUT": str(output),
            "REPLAY_PYTHON": sys.executable, "REPLAY_CALLS": str(calls),
            **{k: gha.to_string(gha.render(v, ctx)) for k, v in step.get("env", {}).items()},
        }
        script = root / "step.sh"
        script.write_text((_RECEIPTS if receipt else "") + gha.render(step["run"], ctx), encoding="utf-8")
        # Files, not pipes: on Windows a timed-out run() kills bash and then calls communicate()
        # with no timeout, which blocks for good while any descendant (Git's bash.exe launcher
        # spawns usr/bin/bash.exe) still holds the pipe; the test file then died at the runner's
        # 300 s cap with no output. With files the 30 s timeout raises and names the step.
        out_path, err_path = root / "stdout", root / "stderr"
        with out_path.open("wb") as out_f, err_path.open("wb") as err_f:
            try:
                returncode = subprocess.run(
                    [bash, "--noprofile", "--norc", "-eo", "pipefail", str(script)],
                    cwd=cwd or root, env=env, stdin=subprocess.DEVNULL,
                    stdout=out_f, stderr=err_f, timeout=30,
                    check=False,
                ).returncode
            except subprocess.TimeoutExpired:
                returncode = None
        stdout = out_path.read_text(encoding="utf-8", errors="replace")
        stderr = err_path.read_text(encoding="utf-8", errors="replace")
        # workflow_steps is not a test module, so pytest does not rewrite this assert: the
        # message is all a CI failure shows. A child that dies without a word (a Windows
        # NTSTATUS exit, a process killed from outside) must still name its exit code.
        assert returncode is not None, (
            f"replayed step timed out after 30 s under {bash}\n--- stdout ---\n{stdout}--- stderr ---\n{stderr}")
        assert returncode == 0, (
            f"replayed step exited {returncode} (0x{returncode & 0xFFFFFFFF:08X}) "
            f"under {bash}\n--- stdout ---\n{stdout}--- stderr ---\n{stderr}")
        return (dict(line.split("=", 1) for line in output.read_text(encoding="utf-8-sig").splitlines()),
                [json.loads(line) for line in calls.read_text(encoding="utf-8-sig").splitlines()])


def outputs(step: dict, ctx: dict, cwd: Path | None = None) -> dict:
    return _run(step, ctx, cwd)[0]


def selected_files(step: dict, ctx: dict, repo: Path) -> set[str]:
    """Receipt the real command and nonempty selected files, not native execution."""
    cwd = repo / step.get("working-directory", ".")
    _, calls = _run(step, ctx, cwd, receipt=True)
    assert len(calls) == 1, f"expected one test invocation, got {calls}"
    command, *args = calls[0]
    if command == "pnpm":
        assert args[:4] == ["exec", "playwright", "test", "-c"], args
        assert (cwd / args[4]).is_file(), args
        paths = args[5:]
        assert paths and all(p.endswith(".spec.ts") for p in paths), args
        ignores = []
    else:
        assert command == "pytest", calls
        # Only the runner options used by these steps are supported; fail closed
        # when a new filter could change the selected files.
        paths, ignores = [], []
        i = 0
        while i < len(args):
            arg = args[i]
            if arg == "--files":
                i += 1
                paths.extend(args[i].split(os.pathsep))
            elif arg == "-m":
                i += 1  # native marker evaluation belongs to the native runner
                assert args[i] in ("platforms and integration", "platforms and not integration"), args
            elif arg.startswith("--ignore-glob="):
                ignores.append(arg.split("=", 1)[1])
            elif arg in ("--", "--include-integration", "-v", "--tb=short", "-rA"):
                pass
            else:
                assert arg.startswith("tests/"), f"unsupported runner argument: {arg}"
                paths.append(arg)
            i += 1
    assert paths, "test command selected no paths"
    files = set()
    for rel in paths:
        path = cwd / rel
        assert path.exists(), f"selected test path does not exist: {path}"
        candidates = path.rglob("test_*.py") if path.is_dir() else [path]
        for candidate in candidates:
            name = candidate.relative_to(repo).as_posix()
            if not any(fnmatch.fnmatch(name, pattern) for pattern in ignores):
                assert candidate.stat().st_size > 0, f"empty test file: {name}"
                files.add(name)
    assert files, "test command selected no files"
    return files
