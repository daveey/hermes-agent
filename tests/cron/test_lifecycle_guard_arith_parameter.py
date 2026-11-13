"""Parameter fallback output inherits its surrounding arithmetic evaluation."""

import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.shell_arith_sink import substitution_in_arithmetic
from tools.shell_heredoc import output_reaches_shell


@pytest.mark.parametrize("op,prelude", [
    (":-", "unset v;"), (":=", "unset v;"), (":+", "v=1;"),
    ("-", "unset v;"), ("=", "unset v;"), ("+", "v=1;"),
])
@pytest.mark.parametrize("nested", [False, True])
def test_parameter_output_executes_in_arithmetic(tmp_path, op, prelude, nested):
    marker = tmp_path / "marker"
    big = tmp_path / "big.sh"
    big.write_text("# padding\n" * 130000 + f"printf RAN > {marker}\n", encoding="utf-8")
    value = "$(echo 'a[$(bash " + str(big) + ")]')"
    if nested:
        value = "${w:-" + value + "}"
    command = prelude + "x=$(( 1 + ${v" + op + value + "} ))"
    result = subprocess.run(
        ["/bin/bash"], input=command, text=True, encoding="utf-8", capture_output=True,
        timeout=20, check=False, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="utf-8") == "RAN"
    assert output_reaches_shell(command)
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe
    assert refusal and "larger than the scan cap" in refusal


@pytest.mark.parametrize("command", [
    "x=${v:-$(echo data)}", "x=${v:=$(echo data)}", "x=${v:+$(echo data)}",
    "x=${v:-${w:-$(echo data)}}",
    "x=$(( ${v:-1} )); y=${w:-$(echo data)}",
    "x=$(( ${v:-${w:-1}} )); y=$(echo data)",
    "x=$(( ${v:-$(date +%s)} ))",
    "v=1; x=$(( ${v#[} )); y=$(printf data)",
    "v=1; x=$(( ${v#$(printf unused)} ))",
    "v=1; x=$(( ${v/$(printf unused)/0} ))",
    "v=1; x=$(( ${v:-[} )); y=$(printf data)",
])
def test_parameter_data_and_closed_arithmetic_are_not_sinks(command, tmp_path):
    result = subprocess.run(
        ["/bin/bash"], input=command, text=True, encoding="utf-8", capture_output=True,
        timeout=20, check=False, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    assert not output_reaches_shell(command)


def test_parameter_date_exemption_remains_caller_controlled():
    command = "x=$(( ${v:-${w:-$(date +%s)}} ))"
    assert not substitution_in_arithmetic(command, date_exempt=True)
    assert substitution_in_arithmetic(command, date_exempt=False)


@pytest.mark.parametrize("shape", [
    "(( 1 + {value} ))", "x=$[1 + {value}]", "let x=1+{value}",
    "a[{value}]=1", "echo ${a[{value}]}", "echo ${s:1+{value}}",
    "[[ 1 -eq {value} ]]",
])
def test_other_arithmetic_frames_pass_context_to_parameters(shape):
    assert output_reaches_shell(shape.replace("{value}", "${v:-$(producer)}"))
