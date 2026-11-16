"""Bash 3.2 must execute before selected PRs can pass the required gate."""
import pytest

from scripts.ci.required_results import evaluate_gate
from tests.ci import _gha_expr as gha
from tests.ci.test_macos_state_routing import context, detect, workflow


@pytest.mark.parametrize('path,selected', [
    ('tests/cron/test_lifecycle_guard_arith_lexical.py', True),
    ('tests/cron/test_lifecycle_guard_arith_let_word.py', True),
    ('tests/cron/test_lifecycle_guard_name_indirection.py', True),
    ('tests/cron/test_lifecycle_guard_heredoc_walk.py', True),
    ('cron/lifecycle_guard.py', True),
    ('cron/lifecycle_guard_arithmetic.py', True),
    ('tools/shell_let_word.py', True),
    ('tools/terminal_tool_guards.py', True),
    ('tests/conftest.py', True),
    ('scripts/run_tests.sh', True),
    ('pyproject.toml', True),
    ('.github/workflows/macos-bash32.yml', True),
    ('tests/cron/heredoc_shape_corpus.py', True),
    ('tests/cron/fixtures/lifecycle_guard_heredoc/example.sh', True),
    ('tools/environments/local.py', True),
    ('tools/environments/base_session_env.py', True),
    ('tools/approval.py', True),
    ('tools/approval_detection.py', True),
    ('tools/code_execution_tool.py', True),
    ('tools/terminal_tool.py', True),
    ('tools/process_registry.py', True),
    ('hermes_cli/sqlite_safe_read.py', True),
    ('hermes_cli/profiles.py', True),
    ('hermes_constants.py', True),
    ('scripts/_activation.sh', True),
    ('scripts/tool_store_pins.py', True),
    ('setup-hermes.sh', True),
    ('scripts/ci/setup_toolchain.py', True),
    ('scripts/ci/pm_cache_log.py', True),
    ('scripts/ci/bash32_guard.py', True),
    ('tests/ci/test_macos_bash32_routing.py', True),
    ('README.md', False),
    ('agent/run.py', False),
    ('.github/workflows/docs-search-monitor.yml', False),
])
@pytest.mark.parametrize('fork', [False, True])
def test_real_selection_wiring_and_required_consumer(path, selected, fork):
    ctx = context(fork=fork)
    outputs = detect([path], ctx)
    assert outputs['macos_bash32'] == gha.to_string(selected)
    ctx['needs'] = {'detect': {'result': 'success', 'outputs': outputs}}
    ci = workflow('ci.yaml')['jobs']
    caller = ci['macos-bash32']
    assert gha.condition(caller['if'], ctx) is (selected and not fork)
    assert 'macos-bash32' in ci['all-checks-pass']['needs']
    # Other independently selected lanes have already passed in this fixture.
    for name in ('tests', 'hermes-state-macos', 'native-memory-macos', 'remote-backends',
                 'catalog-tests', 'e2e-desktop-update'):
        ctx['needs'][name] = {'result': 'success'}
    for result in ('success', 'failure', 'cancelled', 'skipped'):
        ctx['needs']['macos-bash32'] = {'result': result}
        assert evaluate_gate(ctx['needs'])['ok'] is (
            result == 'success' or (result == 'skipped' and not selected)
        )
    if selected:
        del ctx['needs']['macos-bash32']
        assert not evaluate_gate(ctx['needs'])['ok']


def test_room_only_reusable_lane_keeps_scheduled_login_lane_separate():
    child = workflow('macos-bash32.yml')
    assert 'workflow_call' in child['on']
    job = child['jobs']['bash32']
    assert set(job['runs-on']) == {'self-hosted', 'macOS', 'hermes-mac'}
    for fork in (False, True):
        assert gha.condition(job['if'], context(fork=fork)) is (not fork)
    scheduled = workflow('macos-os-tests.yml')
    assert 'pull_request' not in scheduled['on']
    assert 'hermes-mac-session' in scheduled['jobs']['macos-login-session-tests']['runs-on']


@pytest.mark.parametrize('system,resolved,version,accepted', [
    ('Darwin', '/bin/bash', '3.2', True),
    ('Linux', '/bin/bash', '3.2', False),
    ('Darwin', '/opt/homebrew/bin/bash', '3.2', False),
    ('Darwin', '/bin/bash', '5.3', False),
    ('Darwin', None, '3.2', False),
])
def test_pytest_session_enforces_native_bash_after_bootstrap(monkeypatch, system, resolved, version, accepted):
    from scripts.ci import bash32_guard

    monkeypatch.setattr(bash32_guard.platform, 'system', lambda: system)
    monkeypatch.setattr(bash32_guard.shutil, 'which', lambda name: resolved)
    calls = []

    def check_output(argv, **kwargs):
        calls.append(argv)
        return version

    monkeypatch.setattr(bash32_guard.subprocess, 'check_output', check_output)
    if accepted:
        bash32_guard.pytest_sessionstart(None)
        assert calls and calls[0][0] == '/bin/bash'
    else:
        with pytest.raises(pytest.UsageError, match='Bash 3.2'):
            bash32_guard.pytest_sessionstart(None)


def test_lane_loads_interpreter_guard_in_every_pytest_worker():
    step = workflow('macos-bash32.yml')['jobs']['bash32']['steps'][-1]
    assert '-p scripts.ci.bash32_guard' in step['run']


def test_main_push_does_not_duplicate_pr_lane():
    assert detect([], context('push', 'refs/heads/main'))['macos_bash32'] == 'false'
    assert detect([], context('workflow_dispatch', 'refs/heads/main'))['macos_bash32'] == 'true'
