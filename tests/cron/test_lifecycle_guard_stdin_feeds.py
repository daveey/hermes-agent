"""Fail-open gaps left after the ``uv run`` / stdin-feed scan (PR #2233 review 5892368091).

1. ``uv run`` options that take a value but were not in ``_UV_VALUE_OPTIONS`` (``-w``,
   ``--upgrade-group``, the ``--*-package`` family, ``--python-preference``) made the value read
   as the command, so the ``.py`` that ran was never opened. An option the table does not know
   also scans a ``.py`` right after it (fail closed).
2. Stdin feeds the segment walk did not see: a leading redirection (``< f.py python3``), one
   glued to the command word (``python3<f.py``), ``cat``'s own stdin (``cat < f.py | python3``)
   and a pipe into a group (``cat f.py | (python3)``, ``| { …; }``).
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
    sh = tmp_path / "restart.sh"
    sh.write_text(f"#!/bin/sh\necho hi\n{LIFECYCLE}\n", encoding="utf-8")
    sh.chmod(0o755)
    py = tmp_path / "runner.py"
    py.write_text(f"import os\nos.system({LIFECYCLE!r})\n", encoding="utf-8")
    clean = tmp_path / "clean.py"
    clean.write_text("import sys\nprint(sys.stdin.read())\n", encoding="utf-8")
    clean_sh = tmp_path / "clean.sh"
    clean_sh.write_text("echo ok\n", encoding="utf-8")
    return {name: shlex.quote(str(path)) for name, path in
            {"sh": sh, "py": py, "clean": clean, "clean_sh": clean_sh, "dir": tmp_path}.items()}


@pytest.mark.parametrize(
    "template",
    [
        "uv run -w requests {py}",
        "uv run -w requests -w rich {py}",
        "uv run --upgrade-group dev {py}",
        "uv run --config-settings-package pkg:k=v {py}",
        "uv run --exclude-newer-package pkg=2024-01-01 {py}",
        "uv run --no-build-isolation-package pkg {py}",
        "uv run --no-editable-package pkg {py}",
        "uv run --no-sources-package pkg {py}",
        "uv run --prerelease-package pkg {py}",
        "uv run --python-preference managed {py}",
        "uv --python-preference managed run {py}",
        "uv run -w requests python {py}",
        "uv run -qw requests {py}",
        "uv run -wrequests {py}",
        "uv run --with=requests {py}",
        "uv run -w requests sudo python3 {py}",
        # An option the table does not know: the `.py` after it is scanned too (fail closed).
        "uv run --some-future-option value {py}",
        "uv --some-future-global value run {py}",
        "uv run --some-future-option value bash {sh}",
        "uv run --some-future-option value sudo python3 {py}",
        # `uv run -` runs the Python program on its stdin.
        "uv run - < {py}",
        "uv run --no-project - < {py}",
        "cat {py} | uv run -",
        "< {py} uv run -",
    ],
)
def test_uv_run_value_options_do_not_hide_the_script(files, template):
    assert scan_gateway_lifecycle(template.format(**files)) == (True, None)


@pytest.mark.parametrize(
    "template",
    [
        # Leading redirection: the command word comes after it.
        "< {py} python3",
        "<{py} python3",
        "0< {py} python3",
        "< {sh} bash",
        "FOO=1 < {py} python3",
        "> /dev/null < {sh} bash",
        "sudo < {sh} bash",
        "if < {sh} bash; then :; fi",
        # Redirection glued to the command word (`<` is not a control character).
        "python3<{py}",
        "bash<{sh}",
        "sudo bash<{sh}",
        "FOO=1 python3<{py}",
        "bash -c 'python3<{py}'",
        # cat's own stdin is what it writes to the pipe.
        "cat < {py} | python3",
        "cat - < {py} | python3",
        "< {py} cat | python3",
        "cat < {sh} | bash",
        # A pipe into a group feeds every command in it.
        "cat {py} | (python3)",
        "cat {py} |(python3)",
        "cat {sh} | (bash)",
        "cat {sh} | (true; bash)",
        "cat {sh} | (true && (bash))",
        "cat {py} | {{ python3; }}",
        "cat {sh} | {{ true; bash; }}",
        # `}` closes the group only in command position; `echo }` prints it.
        "cat {sh} | {{ true; echo }}; bash; }}",
    ],
)
def test_hidden_stdin_feeds_are_scanned(files, template):
    assert scan_gateway_lifecycle(template.format(**files)) == (True, None)


@pytest.mark.parametrize(
    "template",
    [
        "uv run -w requests {clean}",
        "uv run - < {clean}",
        "uv run -w requests - 2> {py}",
        "uv run --python-preference managed {clean}",
        "uv run --some-future-option {py}-not-there pytest",
        "uv run --some-future-option value",
        "< {py} python3 {clean}",
        "< {py} cat",
        "python3<{clean}",
        "cat {py} | (python3 {clean})",
        "(cat {py}); (python3 {clean})",
        "(cat {py}); python3",
        "(cat {py} | grep x); bash {clean_sh}",
        "cat {sh} | (grep restart)",
        "cat {sh} | (true); bash",
        "cat {sh} | {{ grep x; }}; bash",
        "cat {sh} | (grep x) && bash",
        "bash -c 'cat<{sh}'",
        "echo '<' {sh}",
        "grep -c '<' {py}",
    ],
)
def test_mentions_and_data_stay_allowed(files, template):
    assert scan_gateway_lifecycle(template.format(**files)) == (False, None)


def _uv_module():
    # Imported per test so the scan cases above still run (and fail one by one) on a base
    # that predates the module.
    from cron import lifecycle_guard_uv

    return lifecycle_guard_uv


def test_uv_value_options_cover_uv_help_run():
    """Every value option ``uv help run`` (uv 0.13.7) lists, plus the hidden
    ``--python-preference``."""
    lifecycle_guard_uv = _uv_module()
    for option in ("-w", "--with", "--upgrade-group", "--config-settings-package",
                   "--exclude-newer-package", "--no-build-isolation-package",
                   "--no-editable-package", "--no-sources-package", "--prerelease-package",
                   "--python-preference", "--no-binary-package", "--no-build-package"):
        assert option in lifecycle_guard_uv._UV_VALUE_OPTIONS, option
    # Flags never consume the command.
    for flag in ("-m", "--module", "-s", "--script", "--no-project", "-q", "--isolated"):
        assert flag not in lifecycle_guard_uv._UV_VALUE_OPTIONS, flag


def test_uv_option_arity():
    arity = _uv_module()._uv_option_arity
    assert arity("-w") == arity("--with") == arity("-qw") == "value"
    assert arity("--with=x") == arity("-wx") == arity("-qv") == arity("--no-project") == "flag"
    assert arity("--frobnicate") == arity("-Z") == arity("-qZ") == "unknown"


def test_uv_run_readings():
    indices = _uv_module()._uv_run_command_indices
    # Unknown options are read as flags first (the old reading), then as taking a value.
    assert indices(["uv", "run", "--frob", "x", "f.py"], 0) == [3, 4]
    assert indices(["uv", "--frob", "x", "run", "f.py"], 0) == [4]
    assert indices(["uv", "run", "--", "-x.py"], 0) == [3]
    assert indices(["uv", "pip", "install", "f.py"], 0) == []
    # `pip` may be the unknown option's value, so that reading still finds the run.
    assert indices(["uv", "--frob", "pip", "run", "f.py"], 0) == [4]
    assert indices(["uv", "-q", "pip", "run", "f.py"], 0) == []
    # Bounded: a run of unknown options does not explode.
    assert len(indices(["uv", "run", *(["--frob"] * 40), "f.py"], 0)) <= 16
    # A reading that runs off the end is no alternate command.
    alternates = lifecycle_guard._uv_alternate_command_indices
    assert alternates(["uv", "run", "--frob", "value"]) == []
    assert alternates(["uv", "run", "--frob", "x", "sudo", "f.py"]) == [5]


def test_hoisted_redirection_keeps_arguments():
    hoist = lifecycle_guard._hoist_command_redirections
    assert hoist(["<", "f.py", "python3"]) == ["python3", "<", "f.py"]
    # Leading redirects precede later argv/redirects, preserving binding order.
    assert hoist(["A=1", "<f", "bash", "-x"]) == ["A=1", "bash", "<f", "-x"]
    assert hoist(["python3<f.py", "-u"]) == ["python3", "<f.py", "-u"]
    assert hoist(["sudo", "<", "f.sh", "bash"]) == ["sudo", "bash", "<", "f.sh"]
    # Arguments are never split: a glued `<` there may be quoted program text.
    assert hoist(["bash", "-c", "python3<f.py"]) == ["bash", "-c", "python3<f.py"]
    assert hoist(["echo", "<", "x"]) == ["echo", "<", "x"]
    assert hoist(["<"]) == ["<"]
