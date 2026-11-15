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


def test_mentioned_library_does_not_cover_variable_execution(tmp_path):
    from cron.lifecycle_guard import _LifecycleScanBudget
    from cron.lifecycle_guard_nested_shells import unvisited_scopes

    budget = _LifecycleScanBudget()
    path = tmp_path / 'lib.sh'
    mentioned = unvisited_scopes(set(), budget, path, python_source=False,
                                 nested=False, executed=False)
    visited = {key for _, key in mentioned}
    assert unvisited_scopes(visited, budget, path, python_source=False,
                            nested=False, executed=True)
    assert not unvisited_scopes(visited, budget, path, python_source=False,
                                nested=False, executed=False)


def test_printed_variable_is_not_original_shell_syntax():
    from cron.lifecycle_guard import _LifecycleScanBudget
    from cron.lifecycle_guard_output import iter_recursive_payloads

    payloads = list(iter_recursive_payloads(
        'echo "$log" | bash', [], cwd=None, executed=True,
        budget=_LifecycleScanBudget(), covered_scope=None))
    assert payloads
    assert all(not item[-1] for item in payloads)
    shell = list(iter_recursive_payloads(
        "bash -c 'f() { $P; }; f'", [], cwd=None, executed=True,
        budget=_LifecycleScanBudget(), covered_scope=None))
    assert any(item[0] == 'f() { $P; }; f' and item[-1] for item in shell)


def test_variable_discovery_skips_plain_text_without_tokenizing(monkeypatch):
    from cron import lifecycle_guard
    from cron.lifecycle_guard_variables import VariableCandidates

    def forbidden(_command):
        pytest.fail('plain masked payload must not pay for another tokenizer pass')
    monkeypatch.setattr(lifecycle_guard, '_iter_command_segments', forbidden)
    assert list(VariableCandidates().payloads('plain text\n' * 8000, None)) == []


@pytest.mark.parametrize('executed,variable_source', [(False, True), (True, False)])
def test_discovery_requires_execution_and_syntax(executed, variable_source):
    from types import SimpleNamespace
    from cron.lifecycle_guard_variables import scan_variable_payloads

    def forbidden(*args):
        pytest.fail('non-executable or quoted data reached variable discovery')

    budget = SimpleNamespace(variable_candidates=SimpleNamespace(payloads=forbidden))
    assert not scan_variable_payloads('$P', cwd=None, executed=executed,
                                     variable_source=variable_source, budget=budget,
                                     recurse=forbidden, source=None)


@pytest.mark.parametrize('command', [
    'P="big.sh"; bash > "$P"',
    'P="$(echo hi)"; $P',
    'P="`echo hi`"; $P',
    'P="big.sh"; printf "%s" "$P"',
    'echo P="bash big.sh"; $P',
])
def test_data_does_not_create_execution_candidates(command):
    from cron.lifecycle_guard_variables import VariableCandidates

    assert list(VariableCandidates().payloads(command, None)) == []


def test_eval_candidate_is_the_evaluated_text():
    from cron.lifecycle_guard_variables import VariableCandidates

    assert list(VariableCandidates().payloads(
        'P="bash big.sh"; eval "$P"', None)) == [('bash big.sh', None)]


def test_reassignment_retains_all_execution_candidates(tmp_path):
    (tmp_path / 'big.sh').write_text('#' + 'x' * (1024 * 1024 + 1))
    unsafe, refusal = scan_gateway_lifecycle(
        'P=":"; P="bash big.sh"; $P', cwd=str(tmp_path))
    assert unsafe and refusal and 'cap' in refusal


def test_assignment_argument_is_not_a_binding(tmp_path):
    (tmp_path / 'big.sh').write_text('#' + 'x' * (1024 * 1024 + 1))
    assert scan_gateway_lifecycle(
        'echo P="bash big.sh"; $P', cwd=str(tmp_path)) == (False, None)
