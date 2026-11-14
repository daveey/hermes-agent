"""Reaching definitions must not erase inherited or unknown shell stdin."""

import os
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle


@pytest.mark.parametrize("command", [
    "bash --noprofile -l < $S; S=/dev/null",
    "(S=/dev/null); bash --noprofile -l < $S",
    "false && S=/dev/null; bash --noprofile -l < $S",
    "if false; then S=/dev/null; fi; bash --noprofile -l < $S",
    "f() { S=/dev/null; }; bash --noprofile -l < $S",
    "S=/dev/null; read S; bash --noprofile -l < $S",
    "S=/dev/null; unset S; bash --noprofile -l < $S",
    "S=/dev/null; S+=suffix; bash --noprofile -l < $S",
    "S=/dev/null; eval 'S=$OTHER'; bash --noprofile -l < $S",
    "S=/dev/null; . update.sh; bash --noprofile -l < $S",
    "declare -n S=T; T=/dev/null; S=input.sh; bash --noprofile -l < $T",
    "S=/dev/null; declare -n R=S; R=input.sh; bash --noprofile -l < $S",
    "D=/dev/null; S=$D; D=input.sh; bash --noprofile -l < $D",
])
def test_uncertain_variable_definition_cannot_clear_stdin(tmp_path, monkeypatch, command):
    (tmp_path / "huge.sh").write_text("# padding\n" * 130000)
    (tmp_path / "input.sh").write_text("t ( bash huge.sh )\n")
    monkeypatch.setenv("S", "input.sh")
    unsafe, refusal = scan_gateway_lifecycle(
        command, cwd=str(tmp_path), session_aliases=frozenset({"zz"}))
    assert unsafe and refusal is not None


@pytest.mark.parametrize("command", [
    "S=/dev/null; bash --noprofile -l < $S",
    "export S=/dev/null; bash --noprofile -l < ${S}",
    "D=/dev/null; S=$D; D=input.sh; bash --noprofile -l < $S",
    "su -l bob < /dev/null",
    "bash --noprofile -l <&-",
    "cat < $S; S=/dev/null",
])
def test_proven_variable_and_inert_controls(tmp_path, monkeypatch, command):
    monkeypatch.setenv("S", "input.sh")
    assert scan_gateway_lifecycle(
        command, cwd=str(tmp_path), session_aliases=frozenset({"zz"})) == (False, None)


@pytest.mark.parametrize("command", [
    "bash --noprofile -l <&3; cat 3</dev/null",
    "cat 3</dev/null; bash --noprofile -l <&3",
    "(exec 3</dev/null); bash --noprofile -l <&3",
    "false && exec 3</dev/null; bash --noprofile -l <&3",
    "f() { exec 3</dev/null; }; bash --noprofile -l <&3",
    "bash --noprofile -l <&3 3</dev/null",
    "exec 3</dev/null; eval 'exec 3<other'; bash --noprofile -l <&3",
    "bash --noprofile -l < /dev/fd/3; cat 3</dev/null",
])
def test_unproven_descriptor_definition_retains_unknown(tmp_path, command):
    unsafe, refusal = scan_gateway_lifecycle(
        command, cwd=str(tmp_path), session_aliases=frozenset({"zz"}))
    assert unsafe and refusal is not None and "cannot resolve" in refusal


@pytest.mark.parametrize("command", [
    "exec 3</dev/null; su -l bob <&3",
    "su -l bob 3</dev/null <&3",
    "exec 3</dev/null; su -l bob < /dev/fd/3",
    "S=/dev/null; exec 3<$S; S=input.sh; su -l bob <&3",
    "exec 3<&-; su -l bob <&3",
])
def test_proven_descriptor_controls(tmp_path, command):
    assert scan_gateway_lifecycle(
        command, cwd=str(tmp_path), session_aliases=frozenset({"zz"})) == (False, None)


@pytest.mark.parametrize("command", [
    "bash --noprofile -l < $S",
    "'S=/dev/null'; bash --noprofile -l < $S",
    '"S=/dev/null"; bash --noprofile -l < $S',
    "A=$S; export A=/dev/null S=$A; bash --noprofile -l < $S",
    "A=$S T=$A A=/dev/null; bash --noprofile -l < $T",
    "readonly S; export S=/dev/null; bash --noprofile -l < $S",
    "readonly S=$S; export S=/dev/null; bash --noprofile -l < $S",
    "bash --noprofile -l < $S; S=/dev/null",
    "(S=/dev/null); bash --noprofile -l < $S",
    "false && S=/dev/null; bash --noprofile -l < $S",
    "f() { S=/dev/null; }; bash --noprofile -l < $S",
    "bash --noprofile -l <&3",
    "bash --noprofile -l <&3; cat 3</dev/null",
    "cat 3</dev/null; bash --noprofile -l <&3",
    "(exec 3</dev/null); bash --noprofile -l <&3",
])
def test_real_bash_inherited_source_oracle(tmp_path, monkeypatch, command):
    """Known-executing anchors prevent a vacuous conservative scanner matrix."""
    marker = tmp_path / "marker.sh"
    marker.write_text("printf 'REVIEW_STDIN_EXECUTED\\n'\n")
    env = dict(os.environ, S=str(marker), HOME=str(tmp_path))
    # Only harmless marker bytes are executed. The oversize scanner fixture is
    # separate and is never supplied to Bash.
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", 'exec 3<"$S"; ' + command],
        env=env, cwd=tmp_path, input="", capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "REVIEW_STDIN_EXECUTED\n"
    (tmp_path / "huge.sh").write_text("# padding\n" * 130000)
    (tmp_path / "input.sh").write_text("t ( bash huge.sh )\n")
    monkeypatch.setenv("S", "input.sh")
    unsafe, refusal = scan_gateway_lifecycle(
        command, cwd=str(tmp_path), session_aliases=frozenset({"zz"}))
    assert unsafe and refusal is not None
