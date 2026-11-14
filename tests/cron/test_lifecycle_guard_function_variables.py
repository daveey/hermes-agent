"""Real Bash differentials for variable-indirect function execution."""
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle


@pytest.mark.parametrize("body,value", [
    ('$P', 'bash big.sh'), ('eval "$P"', 'bash big.sh'), ('bash "$P"', 'big.sh'),
])
@pytest.mark.parametrize("invocation", ['f', 'x=$(f +%s)', 'x=$((1 - $(f +%s)))'])
@pytest.mark.parametrize("sourced", [False, True])
@pytest.mark.parametrize("name", ['f', 'date'])
def test_function_variable_executes(tmp_path, body, value, invocation, sourced, name):
    invocation = invocation.replace('f', name)
    # A shell function replaces the real Hermes executable: no service is touched.
    (tmp_path / 'big.sh').write_text('#' + 'x' * (1024 * 1024) + '\nhermes gateway restart\n')
    definition = f'{name}() {{ {body}; }};'
    if sourced:
        (tmp_path / 'lib.sh').write_text(definition)
        definition = '. ./lib.sh;'
    command = f'{definition} export P=\'{value}\'; {invocation}'
    proof = subprocess.run(
        ['bash', '--noprofile', '--norc', '-c',
         'hermes() { printf RUN > marker; printf 0; }; export -f hermes; ' + command],
        cwd=tmp_path, capture_output=True, timeout=10, check=False,
    )
    assert proof.returncode == 0, proof.stderr
    assert (tmp_path / 'marker').read_text() == 'RUN'
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe
    assert refusal and 'cap' in refusal


@pytest.mark.parametrize("value", ['bash big.sh', 'big.sh'])
def test_redirect_variable_is_not_executed(tmp_path, value):
    (tmp_path / 'big.sh').write_text('x' * (1024 * 1024 + 1))
    command = f'gh() {{ command gh "$@" >"$log"; }}; log=\'{value}\'; gh --version'
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


@pytest.mark.parametrize('body', ['$P', '${P}', 'eval "$P"', 'bash "$P"'])
@pytest.mark.parametrize('binding', ['P', 'export P'])
def test_sourced_bindings_pair_with_previously_seen_function(tmp_path, body, binding):
    (tmp_path / 'big.sh').write_text('#' + 'x' * (1024 * 1024 + 1))
    value = 'big.sh' if body == 'bash "$P"' else 'bash big.sh'
    (tmp_path / 'values.sh').write_text(f"{binding}='{value}'\n")
    command = f'f() {{ {body}; }}; . ./values.sh; f'
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe and refusal and 'cap' in refusal


def test_variable_state_does_not_leak_between_scans(tmp_path):
    (tmp_path / 'big.sh').write_text('#' + 'x' * (1024 * 1024 + 1))
    assert scan_gateway_lifecycle("P='bash big.sh'", cwd=str(tmp_path)) == (False, None)
    assert scan_gateway_lifecycle('f() { $P; }; f', cwd=str(tmp_path)) == (False, None)
