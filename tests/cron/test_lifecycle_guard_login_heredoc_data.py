"""Literal unquoted heredoc data is not a startup shell's alias subshell."""

import itertools
import os
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.shell_heredoc import strip_inert_heredoc_bodies


@pytest.mark.parametrize("body", [
    'python3 - <<EOF\nimport sqlite3\nc = sqlite3.connect("DB")\nEOF\n',
    'cat <<EOF\nf(DB)\nEOF\n',
])
@pytest.mark.parametrize("launcher", ["bash -l", "bash", "sh", "source"])
@pytest.mark.parametrize("delimiter", ["EOF", "'EOF'"])
def test_login_script_literal_heredoc_data(tmp_path, body, launcher, delimiter):
    db = tmp_path / "kanban.db"
    db.write_bytes(b"x" * 2100000)
    script = tmp_path / "close.sh"
    script.write_text(body.replace("DB", str(db)).replace("<<EOF", f"<<{delimiter}"))
    assert scan_gateway_lifecycle(f"{launcher} {script}", cwd=str(tmp_path)) == (False, None)


@pytest.mark.parametrize("prefix,opener,body,suffix", [
    ("shopt -s expand_aliases\nalias cat=bash\n", "cat <<EOF", 'hermes gateway restart', ""),
    ("alias cat=bash\n", "cat <<EOF", 'hermes gateway restart', ""),
    ("alias cat=bash\n", "cat <<EOF", 'bash DANGER', ""),
    ("", "cat <<EOF", '$(hermes gateway restart)', ""),
    ("", "cat <<EOF", '`hermes gateway restart`', ""),
    ("", "cat <<EOF | bash", 'hermes gateway "restart"', ""),
    ("", "cat <<EOF > later.sh", 'hermes gateway "restart"', "bash later.sh\n"),
    ("", "cat <<EOF > later.sh", 'bash DANGER', "bash later.sh\n"),
])
def test_login_heredoc_execution_stays_refused(tmp_path, prefix, opener, body, suffix):
    danger = tmp_path / "danger.sh"
    danger.write_text('hermes gateway "restart"\n')
    script = tmp_path / "close.sh"
    script.write_text(f"{prefix}{opener}\n{body}\nEOF\n{suffix}".replace("DANGER", str(danger)))
    assert scan_gateway_lifecycle(f"bash -l {script}", cwd=str(tmp_path))[0]


@pytest.mark.parametrize("body", ["$VALUE", "`printf RUN`", "line\\\ncontinued", "literal"])
@pytest.mark.parametrize("nested", [False, True])
def test_literal_mask_boundary(body, nested):
    text = f"cat <<EOF\n{body}\nEOF\n"
    masked = strip_inert_heredoc_bodies(text, nested=nested)
    assert (masked != text) is (nested and body == "literal")


def test_every_body_must_be_literal():
    text = "cat <<EOF 3<<SECOND\nliteral\nEOF\n$VALUE\nSECOND\n"
    assert strip_inert_heredoc_bodies(text, nested=True) == text


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("shell,consumer,shape", itertools.product(
    ["bash -l", "bash --login", "env bash -l", "timeout 5 bash -l", "bash -O expand_aliases -l"],
    ["cat", "command cat", "env cat", "timeout 5 cat"],
    ["substitution", "backtick", "pipe", "file"],
))
def test_generated_real_bash_execution_is_refused(tmp_path, shell, consumer, shape):
    """Real harmless execution oracle; the fake hermes never touches a gateway."""
    fake = tmp_path / "hermes"
    fake.write_text('#!/bin/sh\nprintf RUN\\n\n')
    fake.chmod(0o755)
    # Exercise wrapper peeling, not GNU timeout (absent on stock macOS).
    # The real Bash oracle remains bounded by subprocess.run(timeout=10).
    timeout = tmp_path / "timeout"
    timeout.write_text('#!/bin/sh\n[ "$1" = 5 ] || exit 2\nshift\nexec "$@"\n')
    timeout.chmod(0o755)
    danger = tmp_path / "danger.sh"
    danger.write_text('hermes gateway "restart"\n')
    payload = f"bash {danger}"
    opener, suffix = f"{consumer} <<EOF", ""
    if shape == "substitution":
        payload = f"$({payload})"
    elif shape == "backtick":
        payload = f"`{payload}`"
    elif shape == "pipe":
        opener += " | bash"
    else:
        opener += f" > {tmp_path / 'later.sh'}"
        suffix = f"bash {tmp_path / 'later.sh'}\n"
    script = tmp_path / "close.sh"
    script.write_text(f"{opener}\n{payload}\nEOF\n{suffix}")
    unsafe_before_execution = scan_gateway_lifecycle(f"{shell} {script}", cwd=str(tmp_path))[0]
    # Outer startup files are not needed for the marker; execute the same script
    # under isolated bash while testing each login invocation through the guard.
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", str(script)],
        env={"PATH": f"{tmp_path}{os.pathsep}{os.defpath}", "HOME": str(tmp_path)}, cwd=tmp_path,
        capture_output=True, text=True, encoding="utf-8", timeout=10, check=True,
    )
    assert "RUN" in result.stdout
    assert unsafe_before_execution
