"""Arithmetic re-evaluation of compound indices and builtin variable names."""

import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tests.cron.test_lifecycle_guard_arith_sink import _big_log
from tools.shell_arith_sink import substitution_in_arithmetic
from tools.shell_heredoc import output_reaches_shell


PRIOR_EXPANSION_SHAPES = [
    'a=(1); unset $(case x in x) echo x;; esac) "a[{producer}]"',
    'a=(1); unset $(case x in (x) echo x;; esac) "a[{producer}]"',
    'a=(1); unset $(echo x) "a[{producer}]"',
    'a=(1); unset "$(echo x)" "a[{producer}]"',
    'a=(1); X=$(echo x) unset "a[{producer}]"',
    'a=(1); unset `echo x` "a[{producer}]"',
    'a=(1); unset $(echo "$(echo x)") "a[{producer}]"',
    'a=(1); unset $(echo "; ) unset") "a[{producer}]"',
    'a=(1); unset $((1 + 2)) "a[{producer}]"',
    'a=(1); X=$(echo x) printf -v "a[{producer}]" %s 1',
    'a=(1); unset $(echo x; echo y) "a[{producer}]"',
    'a=(1); unset <(echo x) "a[{producer}]"',
    'a=(1); unset ${z:-$(echo y)} "a[{producer}]"',
    'a=(1); unset 2>$(echo /dev/null) "a[{producer}]"',
    'a=(1); unset >$(echo /dev/null) "a[{producer}]"',
]


QUOTE_SCOPE_SHAPES = [
    'a=(1); unset "a""[{producer}]"',
    'a=(1); unset a"[{producer}]"',
    'a=(1); printf -v "a""[{producer}]" %s 1',
    'a=(1); echo "$(unset "a[{producer}]")"',
    'a=(1); echo "$(printf -v "a[{producer}]" %s 1)"',
    'a=(1); echo "$(echo "$(unset "a""[{producer}]")")"',
]


SHAPES = [
    "a=([{producer}]=1)",
    "declare -a a=([{producer}]=1)",
    "a+=([{producer}]=2)",
    'a=(1 2 3); unset "a[{producer}]"',
    'a=(1); printf -v "a[{producer}]" %s 1',
    'read "a[{producer}]" <<< 1',
    'declare "a[{producer}]=1"',
    'f() { local "a[{producer}]=1"; }; f',
    'typeset "a[{producer}]=1"',
    '[[ -v "a[{producer}]" ]]',
    'test -v "a[{producer}]"',
    '[ -v "a[{producer}]" ]',
    'a=(1); if unset "a[{producer}]"; then :; fi',
    'a=(1); ! unset "a[{producer}]"',
    'a=(1); X=1 unset "a[{producer}]"',
    'a=(1); command -p unset "a[{producer}]"',
    'a=(1); time unset "a[{producer}]"',
    'a=(1); for i in 1; do unset "a[{producer}]"; done',
    'a=(1); if printf -v "a[{producer}]" 1; then :; fi',
    'a=(1); time -p -- command -p builtin unset "a[{producer}]"',
    'a=(1); while unset "a[{producer}]"; do break; done',
    'a=(1); until unset "a[{producer}]"; do break; done',
    'a=(1); if :; then unset "a[{producer}]"; fi',
    'a=(1); if false; then :; elif unset "a[{producer}]"; then :; fi',
    'a=(1); if false; then :; else unset "a[{producer}]"; fi',
    'a=(1); unset 2>&1 "a[{producer}]"',
    'a=(1); >/dev/null unset "a[{producer}]"',
    'a=(1); command -p 2>/dev/null unset "a[{producer}]"',
    'a=(1); unset \\\n "a[{producer}]"',
    'a=(1); printf -v"a[{producer}]" %s 1',
] + QUOTE_SCOPE_SHAPES + PRIOR_EXPANSION_SHAPES


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("producer", ["$(echo 'b[$(bash BIG)]')", "`echo 'b[$(bash BIG)]'`"])
def test_subscript_producer_is_sink(tmp_path, shape, producer):
    command = shape.replace("{producer}", producer).replace("BIG", str(_big_log(tmp_path)))
    assert output_reaches_shell(command)
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe
    assert refusal and "larger than the scan cap" in refusal


@pytest.mark.parametrize("shape", SHAPES)
def test_numeric_date_subscript_is_not_sink(shape):
    # output_reaches_shell conservatively disables the date exemption for any
    # declare/typeset command (possible imported function); preserve that policy.
    command = shape.replace("{producer}", "$(date +%s)")
    assert not substitution_in_arithmetic(command, date_exempt=True)
    assert substitution_in_arithmetic(command, date_exempt=False)


@pytest.mark.parametrize("shape", SHAPES)
def test_backtick_without_dollar_substitution_is_sink(shape):
    assert substitution_in_arithmetic(shape.replace("{producer}", "`value`"), date_exempt=True)


@pytest.mark.parametrize("command", [
    'echo "<("; echo "a[$(value)]"',
    '# $(\necho "a[$(value)]"',
    'echo $(echo x) "a[$(value)]"',
    'printf %s "$(echo x)" "a[$(value)]"',
    'command -v unset "$(echo x)" "a[$(value)]"',
    'unset $(echo x) >"a[$(value)]"',
    'echo "a""[$(value)]"',
    'echo "$(echo unset "a[$(value)]")"',
    'echo "$(echo "$(echo unset "a[$(value)]")")"',
    'echo "$(unset x)"; echo "a[$(value)]"',
    'echo "a[$(value)]"',
    'echo unset "a[$(value)]"',
    'if echo unset "a[$(value)]"; then :; fi',
    'X=1 command -p echo unset "a[$(value)]"',
    'echo ";" unset "a[$(value)]"',
    'echo "|" unset "a[$(value)]"',
    'command -v unset "a[$(value)]"',
    'command -V unset "a[$(value)]"',
    'printf -v x %s "a[$(value)]"',
    'unset x > "a[$(value)]"',
    '>out echo unset "a[$(value)]"',
    'echo 2>&1 unset "a[$(value)]"',
    'unset a; echo "a[$(value)]"',
    'a=(1 2); echo "a[$(value)]"',
    'printf %s "a[$(value)]"',
    'echo "a=([$(value)]=1)"',
    "echo 'unset a[$(value)]'",
    'x=$(( $(date +%s) + 2700 ))',
])
def test_data_and_watchers_remain_non_sinks(command):
    assert not output_reaches_shell(command)


@pytest.mark.parametrize("shape", SHAPES[:3] + SHAPES[12:19] + QUOTE_SCOPE_SHAPES + PRIOR_EXPANSION_SHAPES)
def test_real_bash_runs_recursive_index_payload(tmp_path, shape):
    from tests.cron.bash_execution import require_modern_system_bash

    require_modern_system_bash()
    # Execute only an isolated marker, never the user's lifecycle commands.
    payload = tmp_path / "big.sh"
    payload.write_text("# padding\n" * 140000 + "printf ran > marker\n", encoding="utf-8")
    producer = "$(echo 'b[$(bash ./big.sh)]')"
    command = shape.replace("{producer}", producer)
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc"], input=command + "\n", text=True,
        encoding="utf-8", capture_output=True, timeout=10, check=False,
        cwd=tmp_path, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert (tmp_path / "marker").read_text(encoding="utf-8") == "ran", result.stderr


@pytest.mark.parametrize("shape", SHAPES[:3] + SHAPES[12:19] + QUOTE_SCOPE_SHAPES + PRIOR_EXPANSION_SHAPES)
def test_recursive_index_scan_cap_without_execution_gate(tmp_path, shape):
    payload = tmp_path / "big.sh"
    payload.write_text("# padding\n" * 140000 + "printf ran > marker\n", encoding="utf-8")
    command = shape.replace("{producer}", "$(echo 'b[$(bash ./big.sh)]')")
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe and refusal and "larger than the scan cap" in refusal
