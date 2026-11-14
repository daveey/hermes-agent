"""Real Bash regressions for phantom parameters and arithmetic quote contexts."""

import shutil
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.shell_arith_sink import substitution_in_arithmetic
from tools.shell_heredoc import output_reaches_shell


SHAPES = [
    "echo $${; a[0]=1; a[$(echo 'b[$(bash SCRIPT)]')]=1",
    "echo $${; (true); [[ 1 -eq $(echo 'b[$(bash SCRIPT)]') ]]",
    "echo $${\n(true)\na[$(echo 'b[$(bash SCRIPT)]')]=1",
    "x=$(( 1 + `echo 'b[$(bash SCRIPT)]'` ))",
    "unset v; x=$(( ${v:-`echo 'b[$(bash SCRIPT)]'`} ))",
    "x=$(( '0+$(bash SCRIPT)' ))",
    "unset v; x=$(( ${v:-'0+$(bash SCRIPT)'} ))",
    "a['$(bash SCRIPT)']=1",
    # Bash finds subscript and (( )) closers past quoted delimiters; a quoted
    # `]` or `)` must not close the frame and leave a phantom command quote.
    r"declare -A h; h[']']=1; a[$(echo b[\$\(bash SCRIPT\)])]=1; echo \'",
    r"declare -A h; echo ${h[']']}; a[$(echo b[\$\(bash SCRIPT\)])]=1; echo \'",
    r"declare -A h; h['x]y']=1; a[$(echo b[\$\(bash SCRIPT\)])]=1; echo \'",
    r"(( ')' )); a[$(echo b[\$\(bash SCRIPT\)])]=1; echo \'",
    "(( ')' ))\n" r"a[$(echo b[\$\(bash SCRIPT\)])]=1; echo \'",
]


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("shape", SHAPES)
def test_executed_arithmetic_substitutions_are_sinks(tmp_path, shape):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash required for execution proof")
    marker = tmp_path / "ran"
    script = tmp_path / "payload.sh"
    script.write_text(f"touch '{marker}'\nprintf 0\n", encoding="utf-8")
    command = shape.replace("SCRIPT", str(script))
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-c", command], cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True,
        encoding="utf-8", timeout=10, check=False,
    )
    # Invalid arithmetic can error only AFTER the command substitution has run.
    assert marker.exists(), (result.returncode, result.stderr)
    assert substitution_in_arithmetic(command, date_exempt=True)
    assert output_reaches_shell(command)
    script.write_text("# padding\n" * 150000, encoding="utf-8")
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe, refusal
    assert refusal and "larger than the scan cap" in refusal


@pytest.mark.parametrize("command", [
    "x=$(( 1 + `printf 0` ))",
    "unset v; x=$(( ${v:-`printf 0`} ))",
])
def test_backtick_output_is_an_arithmetic_sink(command):
    assert substitution_in_arithmetic(command, date_exempt=True)


@pytest.mark.parametrize("command", [
    "echo '$${; a[$(echo x)]=1'",
    "echo 'x=$(( `echo x` ))'",
    "echo 'x=$(( 0+$(echo x) ))'",
    "echo `printf 0`",
    "[[ 1 -eq 1 && x == '$(printf 0)' ]]",
    "let 'a=1;b=2'; echo $(printf 0)",
    "let '${v:-$(printf 0)}'",
    "x=$(( $(date '+%s') ))",
    r"echo \$\{; echo $(printf 0)",
    "echo $$$${; echo $(printf 0)",
    "echo $$'${'; echo $(printf 0)",
    "declare -A h; h[']']=1; echo $(printf 0)",
    "(( ')' + 0 )); echo $(printf 0)",
    "x=$(( a[')'] )); echo $(printf 0)",
])
def test_literal_or_nonarithmetic_substitutions_remain_data(command):
    assert not substitution_in_arithmetic(command, date_exempt=True)
