"""``python3 <file>`` runs the file, so the guard scans it (regex + data masking, no shell walk).

The referenced-script walk followed ``<file>`` and ``bash <file>`` but not an interpreter run, so a
runner with an appended lifecycle line was refused as ``/path/runner`` and allowed as
``python3 /path/runner``. Interpreter-run files are scanned like a ``.py`` cron script in
``check_gateway_lifecycle``: the regex with data-sink masking, never the shell reference walk,
which is a false-positive generator on Python sources (#77131).
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

import pytest

from cron import lifecycle_guard
from cron.lifecycle_guard import scan_gateway_lifecycle

# Assembled so this file's own text never carries the literal command.
LIFECYCLE = " ".join(["hermes", "gateway", "re" + "start"])


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def runner(tmp_path) -> str:
    """A Python runner with an appended lifecycle line (shell-quoted path)."""
    path = _write(tmp_path / "runner.py", f"import os\nprint(1)\nos.system({LIFECYCLE!r})\n")
    return shlex.quote(str(path))


@pytest.mark.parametrize(
    "template",
    [
        "python3 {p}",
        "python {p}",
        "python3.12 {p} --flag value",
        "Python3 {p}",
        "/usr/bin/python3 {p}",
        "python3 -u {p}",
        "python3 -uB {p}",
        "python3 -W ignore {p}",
        "python3 -Wignore {p}",
        "python3 -X dev {p}",
        "python3 -uX dev {p}",
        "python3 -Ximportprofile {p}",
        "python3 --check-hash-based-pycs never {p}",
        "python3 -I --verbose {p}",
        "python3 -- {p}",
        "/usr/bin/env python3 {p}",
        "env FOO=1 python3 {p}",
        "sudo -u root python3 -u {p}",
        "timeout 60 python3 {p}",
        "cd /tmp && python3 {p} arg",
    ],
)
def test_interpreter_run_file_with_lifecycle_command_is_refused(runner, template):
    assert scan_gateway_lifecycle(template.format(p=runner)) == (True, None)


@pytest.mark.parametrize(
    "template",
    [
        "python3 -c 'print(1)' {p}",
        "python3 -uc 'print(1)' {p}",
        "python3 -cprint {p}",
        "python3 -m http.server {p}",
        "python3 -mhttp.server {p}",
        "python3 - {p}",
        "python3 -- - {p}",
        "pythonista {p}",
        "echo python3 {p}",
    ],
)
def test_non_file_interpreter_runs_do_not_read_the_operand(runner, template):
    """``-c``/``-m``/stdin run no file: the trailing operand is an argument, not code."""
    assert scan_gateway_lifecycle(template.format(p=runner)) == (False, None)


def test_bare_wrapper_has_no_interpreter_operand():
    assert list(lifecycle_guard._iter_python_interpreter_scripts("env")) == []
    assert list(lifecycle_guard._iter_python_interpreter_scripts("python3")) == []
    assert list(lifecycle_guard._iter_python_interpreter_scripts("python3 -u")) == []


def test_operand_after_double_dash_is_the_script_even_if_dashed(tmp_path):
    _write(tmp_path / "-r.py", f"import os\nos.system({LIFECYCLE!r})\n")
    assert scan_gateway_lifecycle("python3 -- -r.py", cwd=str(tmp_path)) == (True, None)


def test_dash_operand_is_stdin_not_a_file_named_dash(tmp_path):
    _write(tmp_path / "-", f"{LIFECYCLE}\n")
    assert scan_gateway_lifecycle("python3 - < /dev/null", cwd=str(tmp_path)) == (False, None)
    assert scan_gateway_lifecycle("python3 -- - < /dev/null", cwd=str(tmp_path)) == (False, None)


@pytest.mark.parametrize(
    "command",
    [
        "python3 /dev/stdin <<'EOF'\nprint(1)\nEOF",
        "token-broker scopes | python3 /dev/stdin",
        "PYTHONPATH=/x python3 /dev/null",
    ],
)
def test_stream_operand_is_not_a_script_file(command):
    """Seen in replayed hub commands: a stream is not a file to scan, and must not fail closed."""
    assert scan_gateway_lifecycle(command) == (False, None)


def test_fifo_operand_is_skipped_without_opening(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)  # opening it would block until a writer appears
    assert scan_gateway_lifecycle(f"python3 {shlex.quote(str(fifo))}") == (False, None)


def test_shell_run_of_a_device_still_fails_closed():
    """Only the interpreter-run branch treats a stream as nothing to scan."""
    unsafe, refusal = scan_gateway_lifecycle("bash /dev/null")
    assert unsafe is True and refusal is not None


def test_python_source_is_not_shell_walked(tmp_path):
    """A path named in Python source is not a shell invocation (#77131): no walk into it."""
    helper = _write(tmp_path / "restart.sh", f"#!/bin/sh\n{LIFECYCLE}\n")
    source = _write(
        tmp_path / "job.py",
        f"from pathlib import Path\nroot = Path('/') / 'tmp'\n'{helper}'\n",
    )
    assert scan_gateway_lifecycle(f"python3 {shlex.quote(str(source))}") == (False, None)
    # The same file run as a shell script is walked into the helper.
    assert scan_gateway_lifecycle(f"bash {shlex.quote(str(source))}")[0] is True


def test_python_source_data_sink_mention_is_masked(tmp_path):
    source = _write(
        tmp_path / "audit.py",
        f'"""Usage notes:\n    grep -c \'{LIFECYCLE}\' gateway.log\n"""\nprint(1)\n',
    )
    assert scan_gateway_lifecycle(f"python3 {shlex.quote(str(source))}") == (False, None)


def test_unscannable_python_runner_is_allowed_not_refused(monkeypatch, tmp_path):
    """Replayed hub commands ran `python3 ~/.hermes/sync-runner/hermes-sync` (17,882 lines, over the
    walk caps). It ran before this scan existed: an unscannable file is nothing to scan, never a
    refusal; only a lifecycle command actually found blocks."""
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_LINES", 50)
    source = _write(tmp_path / "big.py", "print(1)\n" * 60 + f"# {LIFECYCLE}\n")
    assert scan_gateway_lifecycle(f"python3 {shlex.quote(str(source))}") == (False, None)
    # The same file run directly is still walked and still fails closed.
    unsafe, refusal = scan_gateway_lifecycle(f"bash {shlex.quote(str(source))}")
    assert unsafe is True and refusal is not None and str(source) in refusal


def test_python_scan_is_charged_against_the_walk_budget(monkeypatch, tmp_path):
    """The masker tokenizes: a fitting Python file spends budget like any scanned text."""
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_LINES", 30)
    source = shlex.quote(str(_write(tmp_path / "mid.py", "print(1)\n" * 20)))
    script = shlex.quote(str(_write(tmp_path / "s.sh", "echo ok\n" * 15)))
    unsafe, refusal = scan_gateway_lifecycle(f"bash {script}; python3 {source}; bash {script}")
    assert (unsafe, refusal) == (False, None)
    nested = shlex.quote(str(_write(tmp_path / "n.sh", f"python3 {source}\n")))
    unsafe, refusal = scan_gateway_lifecycle(f"bash {nested}; bash {script}")
    assert unsafe is True and refusal is not None and "s.sh" in refusal


def test_oversized_python_file_only_mentioned_in_inert_heredoc_is_allowed(monkeypatch, tmp_path):
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_LINES", 50)
    source = _write(tmp_path / "big.py", "print(1)\n" * 60)
    command = f"python3 - <<'PY'\npython3 {source}\nPY"
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


def test_python_file_only_mentioned_in_inert_heredoc_is_still_scanned(runner, tmp_path):
    command = f"python3 - <<'PY'\npython3 {runner}\nPY"
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path))[0] is True


def test_python_scan_does_not_hide_a_later_shell_walk_of_the_same_file(tmp_path):
    """A Python regex scan finds nothing in the file itself; ``bash`` of it must still walk on."""
    inner = _write(tmp_path / "inner.sh", f"#!/bin/sh\n{LIFECYCLE}\n")
    outer = _write(tmp_path / "outer", f"#!/bin/sh\nbash {shlex.quote(str(inner))}\n")
    quoted = shlex.quote(str(outer))
    # The nested `python3 outer` is scanned (depth 1) before the top-level `bash outer` is walked.
    first = _write(tmp_path / "first.sh", f"#!/bin/sh\npython3 {quoted}\n")
    command = f"bash {shlex.quote(str(first))}; bash {quoted}"
    assert scan_gateway_lifecycle(command)[0] is True


def test_shell_walked_file_is_not_rescanned_as_python(tmp_path):
    """A shell walk subsumes the Python scan of the same file: shell text read as Python would
    lose the shell walk's data exemptions (a quoted heredoc fed to `cat` is printed, not run)."""
    body = f"#!/bin/sh\ncat <<'EOF'\n{LIFECYCLE}\nEOF\n"
    script = shlex.quote(str(_write(tmp_path / "ok.sh", body)))
    assert scan_gateway_lifecycle(f"bash {script}") == (False, None)
    assert scan_gateway_lifecycle(f"bash {script}; python3 {script}") == (False, None)


def test_shell_walked_file_is_not_charged_again_as_python(monkeypatch, tmp_path):
    """The Python rescan of an already-walked file would spend budget a later script needs."""
    monkeypatch.setattr(lifecycle_guard, "_MAX_LIFECYCLE_SCAN_LINES", 50)
    script = shlex.quote(str(_write(tmp_path / "a.sh", "echo ok\n" * 20)))
    outer = shlex.quote(str(_write(tmp_path / "outer.sh", f"bash {script}; python3 {script}\n")))
    later = shlex.quote(str(_write(tmp_path / "later.sh", "echo ok\n" * 15)))
    assert scan_gateway_lifecycle(f"bash {outer}; bash {later}") == (False, None)


def test_remote_backend_python_file_is_scanned(tmp_path):
    missing = tmp_path / "remote_only.py"

    def read_remote(path: str):
        return f"import os\nos.system({LIFECYCLE!r})\n" if path == str(missing) else None

    assert scan_gateway_lifecycle(
        f"python3 {shlex.quote(str(missing))}", read_remote_script=read_remote,
    )[0] is True
