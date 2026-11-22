"""Referenced scripts must be keyed by the scope actually used to scan them."""
import pytest

from cron import lifecycle_guard as guard


@pytest.mark.parametrize('declaration', ["alias ll='ls -l'", 'declare -i count',
                                         "alias ll='ls -l'\ndeclare -i count"])
@pytest.mark.parametrize('wrapper', ['if [ "$(nice)" -lt 10 ]; then true; fi\n',
                                     'echo "$(true)"; ', 'true; '])
def test_substitution_wrapper_does_not_charge_script_twice(tmp_path, monkeypatch, declaration, wrapper):
    script = tmp_path / 'helper.sh'
    script.write_text(declaration + '\n' + '# padding\n' * 10)
    monkeypatch.setattr(guard, '_MAX_LIFECYCLE_SCAN_LINES', 24)
    command = f'{wrapper}{script} eta'
    assert guard.scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


def test_effective_visit_retains_distinct_execution_and_later_context(tmp_path):
    from cron.lifecycle_guard_nested_shells import mark_effective_visit, unvisited_scopes

    budget = guard._LifecycleScanBudget()
    path = tmp_path / 'helper.sh'
    visited = set()
    mark_effective_visit(':', visited, budget, (path, None), executed=False)
    assert unvisited_scopes(visited, budget, path, python_source=False, nested=False, executed=True)
    mark_effective_visit(':', visited, budget, (path, None), executed=True)
    assert not unvisited_scopes(visited, budget, path, python_source=False, nested=False)
    budget.integer_context = True
    assert unvisited_scopes(visited, budget, path, python_source=False, nested=False)
    budget.integer_context = False
    budget.alias_names = None
    assert unvisited_scopes(visited, budget, path, python_source=False, nested=False)


def test_effective_visit_retains_distinct_working_directory(tmp_path):
    caller = tmp_path / 'a'
    scripts = caller / 'b'
    scripts.mkdir(parents=True)
    (scripts / 'x.sh').write_text("alias ll='ls -l'\n./helper.sh\n")
    (caller / 'helper.sh').write_text('true\n')
    (scripts / 'helper.sh').write_text('hermes gateway restart\n')
    assert guard.scan_gateway_lifecycle(
        'BASH_ENV=b/x.sh bash -c true; b/x.sh', cwd=str(caller)) == (True, None)
