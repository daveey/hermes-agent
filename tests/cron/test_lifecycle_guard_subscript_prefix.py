"""Prior expansions cannot erase the builtin owning a later arithmetic name."""

import shutil
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tests.cron.test_lifecycle_guard_subscript_quotes import NAMES, WRAPPERS
from tools.shell_arith_sink import substitution_in_arithmetic
from tools.shell_heredoc import output_reaches_shell

PRIOR = [
    "x", "$(echo x)", "$(echo x; echo y)", "$((1))", "<(echo x)",
    "${z:-$(echo y)}", "`echo x`", '"$(echo x)"', '"$((1))"',
    '"${z:-$(echo y)}"', '"`echo x`"', '"<(echo x)"',
    "$(echo \"$(echo x)\")", '2>$(echo /dev/null)', '2>"$(echo /dev/null)"',
    '"$(echo x; echo y)"',
]
BUILTINS = [
    'unset {prior} "a[{producer}]"',
    'X={prior} printf -v "a[{producer}]" %s 1',
    'X={prior} read "a[{producer}]" <<< 1',
    'X={prior} declare "a[{producer}]=1"',
    'X={prior} test -v "a[{producer}]"',
]


def write_marker_payload(tmp_path_factory):
    payload = tmp_path_factory.mktemp("prefix-payload") / "big.sh"
    payload.write_text("#" + "padding" * 200000 + "\nprintf ran > marker\n")
    return payload


@pytest.fixture(scope="module")
def marker_payload(tmp_path_factory):
    return write_marker_payload(tmp_path_factory)


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("prior", PRIOR)
@pytest.mark.parametrize("builtin", BUILTINS)
@pytest.mark.parametrize("wrapper", WRAPPERS[0::4])
def test_prior_expansion_cross_product(tmp_path, marker_payload, name, prior, builtin, wrapper):
    """Wrapper shard 0 of 4; shards 1-3 live in ..._prefix_shard{1,2,3}.py (per-file time cap)."""
    check_prior_expansion(tmp_path, marker_payload, name, prior, builtin, wrapper)


def check_prior_expansion(tmp_path, marker_payload, name, prior, builtin, wrapper):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash required for execution evidence")
    (tmp_path / "big.sh").symlink_to(marker_payload)
    builtin = builtin.replace('"a[', name + '"[')
    if prior.startswith("2>"):
        builtin = builtin.replace("X={prior}", "{prior}")
    inner = builtin.format(prior=prior, producer="$(echo 'b[$(bash ./big.sh)]')")
    if "`" in wrapper:
        inner = inner.replace("`", "\\`")  # Bash requires escaped nested backticks.
    command = "a=(1); " + wrapper.format(inner)
    subprocess.run(
        [bash, "--noprofile", "--norc"], input=command + "\n", text=True,
        capture_output=True, timeout=10, check=False, cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    # Older Bash, or a backtick wrapper around a backtick prior, can reject a
    # command. Every actual execution must be refused; static policy is stronger.
    if (tmp_path / "marker").exists():
        unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
        assert unsafe and refusal and "larger than the scan cap" in refusal
    assert output_reaches_shell(command)


@pytest.mark.parametrize("prior", PRIOR)
@pytest.mark.parametrize("command", ['echo {prior}', 'printf %s {prior}', 'cat {prior}', 'grep x {prior}'])
def test_proven_data_commands_with_prior_expansions(prior, command):
    assert not output_reaches_shell(command.format(prior=prior) + ' "a[$(value)]"')


@pytest.mark.parametrize("command", [
    'unknown x "a[$(value)]"',
    '$(echo unset) "a[$(value)]"',
    '$consumer "a[$(value)]"',
    'command "a[$(value)]"',
])
def test_unknown_command_fails_closed(command):
    assert substitution_in_arithmetic(command, date_exempt=True)
