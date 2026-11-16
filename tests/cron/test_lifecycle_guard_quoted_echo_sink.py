"""Literal quoted parentheses are not command-position substitution anchors."""

import shlex
import shutil
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.shell_heredoc import output_reaches_shell, strip_inert_heredoc_bodies


@pytest.mark.parametrize("prefix", ["", "revision ", "${v:-revision} "])
@pytest.mark.parametrize("parens", ["(", "(("])
@pytest.mark.parametrize("producer", ["$(printf revision)", "`printf revision`"])
def test_quoted_parentheses_are_data(tmp_path, prefix, parens, producer):
    suffix = f'echo "{prefix}{parens}{producer})"\n'
    assert not output_reaches_shell(suffix)
    big = tmp_path / "large-data"
    big.write_text("# padding\n" * 150000, encoding="utf-8")
    body = f'print({str(big)!r})\n'
    script = "python3 <<'EOF'\n" + body + "EOF\n" + suffix
    assert body not in strip_inert_heredoc_bodies(script)
    path = tmp_path / "script.sh"
    path.write_text(script, encoding="utf-8")
    unsafe, reason = scan_gateway_lifecycle(f"bash -l {shlex.quote(str(path))}")
    assert not unsafe, reason


@pytest.mark.parametrize("shape", [
    'echo "revision (SUB)"',
    'echo "revision ((SUB))"',
    '( SUB )',
    '"SUB"',
    'echo "revision (ok)"; SUB',
    'echo "$( SUB )"',
    'echo "revision (ok)"; eval "SUB"',
])
@pytest.mark.parametrize("substitution", ["$(printf %s PAYLOAD)", "`printf %s PAYLOAD`"])
def test_generated_real_bash_output_execution(tmp_path, shape, substitution):
    marker = tmp_path / "executed"
    payload = tmp_path / "payload"
    payload.write_text("#!/bin/bash\n: > " + shlex.quote(str(marker)) + "\n", encoding="utf-8")
    payload.chmod(0o700)
    command = shape.replace("SUB", substitution.replace("PAYLOAD", shlex.quote(str(payload))))
    bash = shutil.which("bash")
    assert bash is not None
    result = subprocess.run([bash, "--noprofile", "--norc", "-c", command],
                            capture_output=True, text=True, encoding="utf-8", timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    executes = not shape.startswith('echo "revision (SUB)') and not shape.startswith('echo "revision ((SUB))')
    assert marker.exists() is executes
    assert output_reaches_shell(command) is executes


@pytest.mark.parametrize("prefix", [
    "cat <<'E'\n\"\nE\n",
    "cat <<E\n\"\nE\n",
    "echo $'escaped\\\'quote'\n",
])
def test_quote_like_data_does_not_mask_real_anchor(prefix):
    assert output_reaches_shell(prefix + '( $(printf payload) )')


def test_arithmetic_output_remains_a_sink():
    assert output_reaches_shell('echo "($(( $(printf payload) )))"')


@pytest.mark.parametrize("command", [
    'bash -c "bash <(printf payload)"',
    'echo "bash <(printf payload)"',  # preserve the existing broad direct-sink policy
    'unknown "revision ($(printf revision))"',
    './"($(printf payload))"',
    'echo "$(echo \"revision ($(printf revision))\")"',
    'echo `echo "revision ($(printf revision))"`',
    'cat <(echo "revision ($(printf revision))")',
    'cat <<EOF\necho "revision ($(printf revision))"',
    'cat <<\'EOF\necho "revision ($(printf revision))"',
])
def test_uncertain_or_nested_context_keeps_original_sink(command):
    assert output_reaches_shell(command)


def test_quoted_parentheses_in_command_word_execute(tmp_path):
    marker = tmp_path / "executed"
    payload = tmp_path / "(payload)"
    payload.write_text("#!/bin/bash\n: > " + shlex.quote(str(marker)) + "\n", encoding="utf-8")
    payload.chmod(0o700)
    command = './"($(printf payload))"'
    bash = shutil.which("bash")
    assert bash is not None
    subprocess.run([bash, "--noprofile", "--norc", "-c", command], cwd=tmp_path,
                   capture_output=True, timeout=10, check=True)
    assert marker.exists()
    assert output_reaches_shell(command)

