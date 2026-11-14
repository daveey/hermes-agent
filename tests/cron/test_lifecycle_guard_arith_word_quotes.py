"""Real Bash regressions: quoted subscripts in ``let`` / ``[[ … -eq … ]]`` operands.

Bash removes the shell quotes of those operands, then evaluates the remaining text as
arithmetic, and an array subscript in it runs its command substitutions. Apostrophes,
ANSI-C strings, double-quoted ``\\$(`` and backslash escapes all hide the opener from the
shell but not from the evaluator (t_01a1dace884ce45d).
"""

import shutil
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.shell_arith_sink import substitution_in_arithmetic
from tools.shell_heredoc import output_reaches_shell

# SCRIPT is an executable payload that touches a marker and prints 0.
SINKS = [
    "let 'a[$(SCRIPT)]=1'",
    "let 'x=a[$(SCRIPT)]'",
    "declare -A h; h[k]=1; let 'h[$(SCRIPT)]+=1'",
    "[[ 1 -eq 'a[$(SCRIPT)]' ]]",
    "[[ 'a[$(SCRIPT)]' -eq 1 ]]",
    "let '1+a[$(SCRIPT)]'",
    "let 'a[`SCRIPT`]=1'",
    "let 'a[$((1+$(SCRIPT)))]=1'",
    "let 'a[$((SCRIPT) )]=1'",
    "let $'a[$(SCRIPT)]=1'",
    r"let $'a[\x24(SCRIPT)]=1'",
    r"let $'a\x5b\x24(SCRIPT)]=1'",
    "let 'x=a[''$(SCRIPT)]'",
    "let 'x=a[ ''$(SCRIPT)]'",
    r'let "a[\$(SCRIPT)]=1"',
    r'let "a[\`SCRIPT\`]=1"',
    r'unset v; let "${v:-a[\$(SCRIPT)]}"',
    r"let x=a[\`SCRIPT\`]",
    r"let x=a[\$\(SCRIPT\)]",
    r"let x=a\[\$\(SCRIPT\)\]",
    r"let x=a[$\(SCRIPT\)]",
    r"let x=a['$'\(SCRIPT\)]",
    r"let x=a[' '\$\(SCRIPT\)]",
    r"[[ 1 -eq a\[\$\(SCRIPT\)\] ]]",
]

# Bash runs none of these: no subscript holds the substitution after quote removal.
DATA = [
    "let '${v:-$(SCRIPT)}'",
    "let '$(SCRIPT)'",
    "let 'x=$(( $(SCRIPT) ))'",
    "[[ 1 -eq 1 && x == '$(SCRIPT)' ]]",
    "[[ 1 -eq '$(SCRIPT)' && 1 -eq a[1] ]]",
    "let 'a[1]=1'; echo '[$(SCRIPT)]'",
    "let 'a[1]=1' && echo '$(SCRIPT)'",
    "[[ 1 -eq 1 ]] && echo 'a[$(SCRIPT)]'",
    "let 'a[$i]=1'; echo '$(SCRIPT)'",
    "let 'a[$(date +%s)]=1'",
]


def _run(tmp_path, shape):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash required for execution proof")
    marker = tmp_path / "ran"
    script = tmp_path / "payload.sh"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\nprintf 0\n", encoding="utf-8")
    script.chmod(0o755)
    command = shape.replace("SCRIPT", str(script))
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-c", command], cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True,
        encoding="utf-8", timeout=10, check=False,
    )
    return command, marker.exists(), result


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("shape", SINKS)
def test_quoted_word_arithmetic_subscripts_are_sinks(tmp_path, shape):
    command, ran, result = _run(tmp_path, shape)
    # Invalid arithmetic can error only AFTER the command substitution has run.
    assert ran, (result.returncode, result.stderr)
    assert substitution_in_arithmetic(command, date_exempt=True)
    assert output_reaches_shell(command)


# The guard does not yet follow a script behind an escaped backtick inside a
# double-quoted `let` operand (t_01a1db0c38d72914); the scanner above already flags
# that sink.
_GUARD_GAPS = {
    r'let "a[\`SCRIPT\`]=1"': "t_01a1db0c38d72914",
}


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("shape", [
    pytest.param(shape, marks=pytest.mark.xfail(strict=True, reason=_GUARD_GAPS[shape]))
    if shape in _GUARD_GAPS else shape
    for shape in SINKS
])
def test_lifecycle_guard_scans_the_quoted_subscript_payload(tmp_path, shape):
    script = tmp_path / "payload.sh"
    script.write_text("# padding\n" * 150000, encoding="utf-8")  # past the scan cap
    script.chmod(0o755)
    unsafe, refusal = scan_gateway_lifecycle(
        shape.replace("SCRIPT", str(script)), cwd=str(tmp_path))
    assert unsafe, refusal
    assert refusal and "larger than the scan cap" in refusal


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("shape", DATA)
def test_word_arithmetic_without_a_quoted_subscript_stays_data(tmp_path, shape):
    command, ran, result = _run(tmp_path, shape)
    assert not ran, (result.returncode, result.stderr)
    assert not substitution_in_arithmetic(command, date_exempt=True)
