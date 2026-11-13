"""Interpreter runs the scan did not read: ``uv run``, ``py``/``pypy3`` and programs fed on stdin.

``python3 f.py`` was scanned, but ``uv run f.py``, ``uv run python f.py``, ``py f.py``,
``pypy3 f.py``, ``python3 < f.py``, ``cat f.py | python3``, ``bash < f.sh`` and
``cat f.sh | bash`` ran the same file unread. Python sources stay regex-scanned only, never
shell-walked (#77131). Reserved words (``do``, ``then``, ``!``, ``{``) are covered by
test_lifecycle_guard_reserved_words.py; the cases here combine them with the new runners.
"""

from __future__ import annotations

import shlex

import pytest

from cron import lifecycle_guard
from cron.lifecycle_guard import scan_gateway_lifecycle

# Assembled so this file's own text never carries the literal command.
LIFECYCLE = " ".join(["hermes", "gateway", "re" + "start"])


@pytest.fixture
def files(tmp_path) -> dict[str, str]:
    """Shell-quoted paths: a shell script and a Python runner that each restart the gateway, and
    clean Python and shell scripts."""
    sh = tmp_path / "restart.sh"
    sh.write_text(f"#!/bin/sh\necho hi\n{LIFECYCLE}\n", encoding="utf-8")
    sh.chmod(0o755)
    py = tmp_path / "runner.py"
    py.write_text(f"import os\nprint(1)\nos.system({LIFECYCLE!r})\n", encoding="utf-8")
    clean = tmp_path / "clean.py"
    clean.write_text("import sys\nprint(sys.stdin.read())\n", encoding="utf-8")
    clean_sh = tmp_path / "clean.sh"
    clean_sh.write_text("echo ok\n", encoding="utf-8")
    return {name: shlex.quote(str(path)) for name, path in
            {"sh": sh, "py": py, "clean": clean, "clean_sh": clean_sh, "dir": tmp_path}.items()}


@pytest.mark.parametrize(
    "template",
    [
        "py {py}",
        "py -3 {py}",
        "pypy3 {py}",
        "pypy {py}",
        "uv run {py}",
        "uv run python {py}",
        "uv run python3 -u {py}",
        "uv run --with requests {py}",
        "uv run --python 3.12 python {py}",
        "uv run -p 3.12 --no-project -- python {py}",
        "uv run --script {py}",
        "uv --directory {dir} run {py}",
        "uv run bash {sh}",
        "uv run {sh}",
        "sudo uv run {py}",
        "if true; then uv run {py}; fi",
        "for i in 1; do pypy3 {py}; done",
    ],
)
def test_other_interpreter_spellings_are_scanned(files, template):
    assert scan_gateway_lifecycle(template.format(**files)) == (True, None)


@pytest.mark.parametrize(
    "template",
    [
        "python3 < {py}",
        "python3 <{py}",
        "python3 0< {py}",
        "python3 -u - < {py}",
        "python3 -- - < {py}",
        "python3 > /dev/null < {py}",
        "cat {py} | python3",
        "cat -- {py} | python3 -",
        "cat {py} |& sudo python3",
        "bash < {sh}",
        "bash <{sh}",
        "bash -s < {sh}",
        "bash -s arg < {sh}",
        "cat {sh} | bash",
        "cat {sh} | sudo bash -s",
        "while true; do bash < {sh}; done",
        "! cat {sh} | bash",
        "bash <> {sh}",
        # Clustered `-o` values are option values, not the script operand (stdin still read).
        "bash -euo pipefail < {sh}",
        "bash -euo pipefail -s < {sh}",
        "cat {sh} | bash -eo pipefail",
    ],
)
def test_program_fed_on_stdin_is_scanned(files, template):
    assert scan_gateway_lifecycle(template.format(**files)) == (True, None)


@pytest.mark.parametrize(
    "template",
    [
        # Mentions, data and non-program stdin: no runner or stdin reading turns these into runs.
        "for f in {sh} {py}; do echo $f; done",
        "while read l; do echo \"$l\"; done < {sh}",
        "python3 {clean} < {py}",
        "python3 -c 'print(1)' < {py}",
        "python3 -m json.tool < {py}",
        "cat {py} | python3 {clean}",
        "cat {sh} | grep restart",
        "cat {sh} || bash",
        "bash -c 'echo hi' < {sh}",
        "bash --posix {clean_sh} < {sh}",
        "ls {py} | python3",
        "cat {py} > /dev/null; python3 {clean}",
        "uv pip install -r {py}",
        "uv tool run ruff check {py}",
        "uvx ruff check {py}",
        "py_compile {py}",
        "pyright {py}",
    ],
)
def test_mentions_and_data_stay_allowed(files, template):
    assert scan_gateway_lifecycle(template.format(**files)) == (False, None)


@pytest.mark.parametrize(
    "template",
    [
        # zsh's `-O` is the valueless CORRECT flag, so the next token is the script (PR #2249 nit).
        "zsh -eO {sh}",
        "zsh -O {sh}",
        "zsh +O {sh}",
        # zsh's `-o NAME` and bash's `-O NAME` still consume their value.
        "zsh -o errexit {sh}",
        "zsh -eo errexit {sh}",
        "bash -eO extglob {sh}",
        # An rc file right before the script operand or `-c`, and one before a trailing
        # value-less `--rcfile`, is still sourced, so still scanned (PR #2388 mutants).
        "bash --rcfile {sh} {clean_sh}",
        "bash --init-file {sh} -c 'echo hi'",
        "bash --rcfile {sh} --rcfile",
    ],
)
def test_shell_specific_option_values_find_the_script(files, template):
    assert scan_gateway_lifecycle(template.format(**files)) == (True, None)


@pytest.mark.parametrize(
    "template",
    [
        # Past the script operand `--rcfile` is the script's own argument, never sourced by the
        # shell (PR #2249 nit).
        "bash {clean_sh} --rcfile /dev/null",
        "bash {clean_sh} --rcfile {sh}",
        "bash -e {clean_sh} --init-file {sh}",
        "bash -c 'echo hi' --rcfile {sh}",
        "bash --rcfile",
    ],
)
def test_rcfile_after_the_script_operand_is_not_sourced(files, template):
    assert scan_gateway_lifecycle(template.format(**files)) == (False, None)


def test_python_source_run_under_uv_is_not_shell_walked(tmp_path, files):
    """A Python file is regex-scanned only (#77131): shell-looking lines in it are not walked, even
    when they name a script that would be refused, or a device that would fail closed."""
    source = tmp_path / "doc.py"
    source.write_text(f"x = 1 / 2\nthen bash {files['sh']}\ndo /dev/zero\n", encoding="utf-8")
    quoted = shlex.quote(str(source))
    assert scan_gateway_lifecycle(f"uv run {quoted}") == (False, None)
    assert scan_gateway_lifecycle(f"uv run --with x {quoted}") == (False, None)
    assert scan_gateway_lifecycle(f"python3 < {quoted}") == (False, None)
    # Run directly, it is a script like any other: shell-walked, so both lines count.
    assert scan_gateway_lifecycle(quoted)[0]
    # A `uv` word that is not `uv run` (here a user name) does not make it Python.
    assert scan_gateway_lifecycle(f"sudo -u uv {quoted}")[0]


def test_shell_command_string_flag_is_not_a_script_path(tmp_path):
    (tmp_path / "-c").write_text(f"{LIFECYCLE}\n", encoding="utf-8")
    assert scan_gateway_lifecycle("bash -c 'echo hi'", cwd=str(tmp_path)) == (False, None)


def test_cat_options_are_not_piped_files(tmp_path, files):
    (tmp_path / "-n").write_text(f"import os\nos.system({LIFECYCLE!r})\n", encoding="utf-8")
    assert scan_gateway_lifecycle(f"cat -n {files['clean']} | python3", cwd=str(tmp_path)) == (
        False, None)


def test_uv_run_peel_bounds():
    index = lifecycle_guard._executed_command_index
    assert index(["uv", "run"]) is None
    assert index(["uv", "run", "--with", "x"]) is None
    assert index(["uv", "run", "x"]) == 2
    assert index(["uv", "-q", "run", "--python", "3.12", "python", "f"]) == 5
    assert index(["uv", "pip", "install"]) == 0
    assert index(["uv", "--version"]) == 0
    # `uv tool run` runs a package's tool (uvx), not a local command.
    assert index(["uv", "tool", "run", "x"]) == 0


def test_stdin_redirect_parsing():
    split = lifecycle_guard._split_redirections
    assert split(["<", "a", "-u"]) == (["-u"], ["a"])
    assert split(["0<a", ">", "out", "2>>", "err", "x"]) == (["x"], ["a"])
    assert split(["1<", "a"]) == ([], [])
    # Heredocs, here-strings and duplications are stripped but are not file feeds; `<>` is one.
    assert split(["<<EOF", "<<", "X", "<<<", "w", "<&3", "<>", "f"]) == ([], ["f"])
    assert split(["<"]) == ([], [])


def test_data_sink_piped_into_python_is_not_masked(files):
    """A grep match piped into an interpreter is code, so its pattern is not masked as data (the
    same rule as a pipe into a shell)."""
    assert scan_gateway_lifecycle(f"grep -h '{LIFECYCLE}' {files['py']} | bash") == (True, None)
    assert scan_gateway_lifecycle(f"grep -h '{LIFECYCLE}' {files['py']} | python3") == (True, None)
    assert scan_gateway_lifecycle(f"grep -h '{LIFECYCLE}' {files['py']} | uv run python") == (
        True, None)
    assert scan_gateway_lifecycle(f"grep -h '{LIFECYCLE}' {files['py']} | wc -l") == (False, None)
    assert scan_gateway_lifecycle(f"grep -h '{LIFECYCLE}' {files['py']} | pyright") == (False, None)


@pytest.mark.parametrize("command", ["bash --rcfile", "bash -O extglob --rcfile"])
def test_trailing_valueless_rcfile_does_not_fail_the_walk(caplog, command):
    """A value-taking option last on the line is walked, not crashed on: the fallback after a
    failed walk is the direct scan, which fails open on a sourced rc file (PR #2388 mutant)."""
    with caplog.at_level("WARNING", logger=lifecycle_guard.logger.name):
        assert scan_gateway_lifecycle(command) == (False, None)
    assert not [r for r in caplog.records if "referenced-script walk failed" in r.getMessage()]
