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
    value = "$(echo 'b[$(bash " + str(big) + ")]')"
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


@pytest.mark.parametrize("parameter,prelude", [
    ("a[0]:-", "unset a;"), ("a[0]:=", "unset a;"),
    ("a[0]:+", "a=(1);"), ("a[0]-", "unset a;"),
    ("a[0]=", "unset a;"), ("a[0]+", "a=(1);"),
    ("v/1/", "v=1;"), ("v//1/", "v=1;"),
    ("v/#1/", "v=1;"), ("v/%1/", "v=1;"),
    ("a[0]/1/", "a=(1);"),
    ("v/${w:-1}/", "v=1; unset w;"),
    ('v/"1"/', "v=1;"),
])
def test_subscript_and_replacement_output_executes(tmp_path, parameter, prelude):
    marker = tmp_path / "marker"
    big = tmp_path / "big.sh"
    big.write_text("# padding\n" * 130000 + f"printf RAN > {marker}\n", encoding="utf-8")
    value = "$(echo 'b[$(bash " + str(big) + ")]')"
    command = prelude + "x=$(( ${" + parameter + value + "} ))"
    result = subprocess.run(
        ["/bin/bash"], input=command, text=True, encoding="utf-8", capture_output=True,
        timeout=20, check=False, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="utf-8") == "RAN"
    assert substitution_in_arithmetic(command, date_exempt=True)
    assert output_reaches_shell(command)
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe and refusal and "larger than the scan cap" in refusal


@pytest.mark.parametrize("command", [
    "a=(1); x=${a[0]:-$(echo data)}",
    "v=1; x=${v/1/$(echo data)}",
    "v=1; x=$(( ${v//$(printf unused)/0} ))",
    "v=1; x=$(( ${v/${w:-$(printf unused)}/0} ))",
    r"v=1; x=$(( ${v/unused\/$(printf unused)/0} ))",
    'v=1; x=$(( ${v/"unused/$(printf unused)"/0} ))',
    "v=1; x=$(( ${v/unused/1} )); y=$(echo data)",
    "a=(1); x=$(( ${a[0]:-1} )); y=$(echo data)",
])
def test_replacement_pattern_and_subscript_controls(command, tmp_path):
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
