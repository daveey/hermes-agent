"""Cross-product proof of quote removal and independent substitution scopes."""

import shutil
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.shell_arith_sink import substitution_in_arithmetic
from tools.shell_heredoc import output_reaches_shell


NAMES = ["a", '"a"', "'a'", "$'a'", '""a""']
WRAPPERS = [
    "{}", "if {}; then :; fi", "! {}", "time {}", "X=1 {}",
    "command -p {}", "{{ {}; }}", "( {} )", "echo $({})",
    'echo "$({})"', "echo `{}`", "for i in 1; do {}; done",
    'x="$({})"', 'echo "$(echo "$({})")"',
]
BUILTINS = ['unset {name}"[{producer}]"', 'printf -v {name}"[{producer}]" %s 1']


def _command(name, wrapper, builtin, producer):
    return "a=(1); " + wrapper.format(builtin.format(name=name, producer=producer))


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("wrapper", WRAPPERS)
@pytest.mark.parametrize("builtin", BUILTINS)
def test_quoted_name_scope_cross_product(tmp_path, name, wrapper, builtin):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is required for recursive index execution proof")
    payload = tmp_path / "big.sh"
    payload.write_text("# padding\n" * 140000 + "printf ran > marker\n", encoding="utf-8")
    command = _command(name, wrapper, builtin, "$(echo 'b[$(bash ./big.sh)]')")
    result = subprocess.run(
        [bash, "--noprofile", "--norc"], input=command + "\n", text=True,
        encoding="utf-8", capture_output=True, timeout=10, check=False,
        cwd=tmp_path, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    # Bash 3 lacks printf array destinations. The scanner must still classify it.
    if not ("printf" in builtin and "version 3." in subprocess.run(
        [bash, "--version"], capture_output=True, text=True, encoding="utf-8",
        timeout=10, check=False,
    ).stdout):
        assert (tmp_path / "marker").read_text(encoding="utf-8") == "ran", result.stderr
    assert output_reaches_shell(command)
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe and refusal and "larger than the scan cap" in refusal
    date_command = _command(name, wrapper, builtin, "$(date +%s)")
    assert not substitution_in_arithmetic(date_command, date_exempt=True)
    assert substitution_in_arithmetic(date_command, date_exempt=False)


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_nested_echo_names_remain_data(name, wrapper):
    command = _command(name, wrapper, 'echo unset {name}"[{producer}]"', "$(value)")
    assert not output_reaches_shell(command)
