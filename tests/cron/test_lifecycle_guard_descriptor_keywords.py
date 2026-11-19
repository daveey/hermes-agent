"""Quoted reserved words must not close inherited-descriptor scopes."""
from __future__ import annotations

import shlex
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle


@pytest.mark.parametrize("closer", ['"fi"', "'fi'", r"\fi", "fi''", '"done"', '"}"'])
@pytest.mark.parametrize("clean", [False, True])
def test_literal_closer_preserves_conditional_descriptor(tmp_path, closer, clean):
    script = tmp_path / "r.sh"
    # Fake the executable, never restart a service in a test.
    script.write_text("hermes gateway " + "restart\n" if not clean else "printf CLEAN\n")
    if "done" in closer:
        scope = f"while false; do {closer}; exec 3<&-; done"
    elif "}" in closer:
        scope = f"true || {{ {closer}; exec 3<&-; }}"
    else:
        scope = f"if false; then {closer}; exec 3<&-; fi"
    command = f"exec 3<{shlex.quote(str(script))}; {scope}; bash <&3"
    assert scan_gateway_lifecycle(command) == (not clean, None)
    fake = tmp_path / "hermes"
    fake.write_text("#!/bin/sh\nprintf MARKER\n")
    fake.chmod(0o700)
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", f"PATH={shlex.quote(str(tmp_path))}:$PATH; " + command],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ("CLEAN" if clean else "MARKER")


@pytest.mark.parametrize("opener", ['"if"', "'while'", r"\until", '"{"', '"in"'])
def test_literal_opener_does_not_run_exec(tmp_path, opener):
    script = tmp_path / "r.sh"
    script.write_text("hermes gateway " + "restart\n")
    command = f"exec 3<{shlex.quote(str(script))}; {opener} exec 3<&-; bash <&3"
    assert scan_gateway_lifecycle(command) == (True, None)
    fake = tmp_path / "hermes"
    fake.write_text("#!/bin/sh\nprintf MARKER\n")
    fake.chmod(0o700)
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", f"PATH={shlex.quote(str(tmp_path))}:$PATH; " + command],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "MARKER"


@pytest.mark.parametrize("close", ["exec 3<&- | cat;", "exec 3<&- |& cat;", "exec 3<&- & wait;"])
def test_child_close_keeps_parent_descriptor(tmp_path, close):
    script = tmp_path / "r.sh"
    script.write_text("hermes gateway " + "restart\n")
    command = f"exec 3<{shlex.quote(str(script))}; {close} bash <&3"
    assert scan_gateway_lifecycle(command) == (True, None)
    # Only the execution premise needs modern Bash; the scanner is portable.
    if "|&" in close:
        from tests.cron.bash_execution import require_modern_system_bash

        require_modern_system_bash()
    fake = tmp_path / "hermes"
    fake.write_text("#!/bin/sh\nprintf MARKER\n")
    fake.chmod(0o700)
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", f"PATH={shlex.quote(str(tmp_path))}:$PATH; " + command],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "MARKER"
