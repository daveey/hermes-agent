"""An asynchronous AND/OR list cannot close the parent's descriptors."""
from __future__ import annotations

import shlex
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle


@pytest.mark.parametrize("tail", [
    "&& true & wait;", "|| false & wait;", "&& true || false & wait;",
    "|| false && true & wait;", "&&\n true & wait;", "||\n false & wait;",
    "&& true | cat & wait;", "&& true |& cat & wait;",
    "&& { true; true; } & wait;", "|| { false; false; } & wait;",
    "&& (true; true) & wait;", "&& { true;\n true; } & wait;",
    "&& if true; then true; fi & wait;",
    "&& true & wait\n", "&& true & wait; true & wait;",
])
@pytest.mark.parametrize("clean", [False, True])
def test_async_list_preserves_parent(tmp_path, tail, clean):
    script = tmp_path / "r.sh"
    script.write_text("printf CLEAN" if clean else "hermes gateway " + "restart")
    fake = tmp_path / "hermes"
    fake.write_text("#!/bin/sh\nprintf MARKER")
    fake.chmod(0o700)
    command = f"exec 3<{shlex.quote(str(script))}; exec 3<&- {tail} bash <&3"
    assert scan_gateway_lifecycle(command) == (not clean, None)
    # Keep scanner coverage on Bash 3, which cannot execute the |& grammar.
    if "|&" in tail:
        from tests.cron.bash_execution import require_modern_system_bash

        require_modern_system_bash()
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", f"PATH={shlex.quote(str(tmp_path))}:$PATH; " + command],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ("CLEAN" if clean else "MARKER")


@pytest.mark.parametrize("tail", ["&& true;", "|| false;", "&& true || false;", "&& true; true & wait;"])
def test_foreground_close_is_still_certain(tmp_path, tail):
    script = tmp_path / "r.sh"
    script.write_text("hermes gateway " + "restart")
    command = f"exec 3<{shlex.quote(str(script))}; exec 3<&- {tail} bash <&3"
    assert scan_gateway_lifecycle(command) == (False, None)


@pytest.mark.parametrize("redirect", ["", "&>out", "&>>out"])
def test_stderr_descriptor_combined_output_rebind(tmp_path, monkeypatch, redirect):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "r.sh").write_text("hermes gateway " + "restart")
    command = "exec 2<r.sh; " + (f"exec {redirect}; " if redirect else "") + "bash <&2"
    assert scan_gateway_lifecycle(command) == (not bool(redirect), None)


@pytest.mark.parametrize("prefix", ["function f {", "coproc named {", "coproc {"])
def test_definition_prefix_consumes_name_before_body(prefix):
    from cron.lifecycle_guard import _REDIRECTION
    from cron.lifecycle_guard_fds import _command_start

    nesting = []
    words = prefix.split() + ["exec", "3<&-"]
    start = _command_start(words, nesting, _REDIRECTION)
    assert words[start:] == ["exec", "3<&-"]
