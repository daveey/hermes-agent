"""Bash name operands and recursively evaluated assignment values must be scanned."""
import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from cron.lifecycle_guard_arith_words import iter_arithmetic_word_payloads
from tests.cron.test_lifecycle_guard_arith_escaped_words import _run_bash


SHAPES = [
    "test -v 'a[$(SCRIPT)]'",
    r"test -v a[\$\(SCRIPT\)]",
    "[ -v 'a[$(SCRIPT)]' ]",
    "printf -v 'a[$(SCRIPT)]' 1",
    r"printf -v a[\$\(SCRIPT\)] 1",
    "read 'a[$(SCRIPT)]' <<< 1",
    "x='a[$(SCRIPT)]'; echo $((x))",
    r"x=a[\$\(SCRIPT\)]; echo $((x))",
    "x='a[$(SCRIPT)]'; (( x ))",
    r"x=a[\`SCRIPT\`]; (( x ))",
    "x='a[$(SCRIPT)]'; let y=x",
    "x='a[$(SCRIPT)]'; [[ x -eq 0 ]]",
    r"declare -i x; x=a[\$\(SCRIPT\)]",
    r"declare -a a=([0]=1); unset a[\$\(SCRIPT\)]",
    r"builtin printf -v a[\$\(SCRIPT\)] 1",
    r"command test -v a[\$\(SCRIPT\)]",
    r"if test -v a[\$\(SCRIPT\)]; then :; fi",
    r"read -r a[\$\(SCRIPT\)] <<< 1",
    r"x=a['$'\(SCRIPT\)]; y=x; echo $((y))",
    "declare x='a[$(SCRIPT)]'; echo $((x))",
]


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("shape", SHAPES)
def test_name_and_indirection_scripts_are_scanned(tmp_path, shape):
    command = shape.replace("SCRIPT", str(tmp_path / "payload.sh"))
    ran, script = _run_bash(tmp_path, command)
    assert ran, "Bash must execute the harmless marker script"
    script.write_text("hermes gateway restart\n", encoding="utf-8")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (True, None)
    script.write_text("echo hello\n", encoding="utf-8")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("shape", [
    r"x=a[\$\(SCRIPT\)]; echo x",
    r"echo a[\$\(SCRIPT\)]",
    r"printf '%s' a[\$\(SCRIPT\)]",
    r"[[ -v a[\$\(SCRIPT\)] ]]",
    r"test -n a[\$\(SCRIPT\)]",
    r"echo 'x=a[$(SCRIPT)]'; echo $((1))",
    r"printf '%s' 'x=a[$(SCRIPT)]; ((x))'",
])
def test_inert_name_words_stay_allowed(tmp_path, shape):
    command = shape.replace("SCRIPT", str(tmp_path / "payload.sh"))
    ran, script = _run_bash(tmp_path, command)
    assert not ran
    script.write_text("hermes gateway restart\n", encoding="utf-8")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


@pytest.mark.parametrize("prefix", ["mapfile -t", "readarray -t", "wait -p"])
def test_other_name_builtins_conservatively_recover_payload(prefix):
    # Bash versions differ in accepting subscripted names here; fail towards
    # reading the referenced file, without refusing clean contents.
    assert list(iter_arithmetic_word_payloads(prefix + r" a[\$\(S\)]")) == ["a[$(S)]"]


@pytest.mark.parametrize("command", [
    r"wait a[\$\(S\)]", r"printf '%s' a[\$\(S\)]", r"test -n a[\$\(S\)]",
])
def test_non_name_modes_do_not_recover_payload(command):
    assert list(iter_arithmetic_word_payloads(command)) == []
