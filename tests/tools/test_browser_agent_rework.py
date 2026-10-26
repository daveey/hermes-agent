"""Managed cleanup and unrelated-owner concurrency contracts."""
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor

from tools import browser_agent_lifecycle as lifecycle
from tools import browser_use_cli as cli


def test_slow_acquire_does_not_block_another_owner(monkeypatch):
    entered, resume, other_done = threading.Event(), threading.Event(), threading.Event()
    config = {'endpoint': 'http://127.0.0.1:19001', 'installation_id': 'test'}

    def post(config, action, **payload):
        if action != 'acquire':
            return {}
        task = payload['owner'][1]
        if task == 'slow':
            entered.set()
            assert resume.wait(5)
        return {'capability': task, 'request_id': task, 'daemon_name': 'lease-' + task,
                'endpoint': config['endpoint'] + '/lease/' + task}

    monkeypatch.setattr(lifecycle, '_post', post)
    monkeypatch.setattr(cli, '_find_cli', lambda: None)

    def run(task):
        with lifecycle.request_lease(config, task, '', 30, {}):
            if task == 'fast':
                other_done.set()

    with ThreadPoolExecutor(2) as pool:
        slow = pool.submit(run, 'slow')
        assert entered.wait(2)
        fast = pool.submit(run, 'fast')
        try:
            assert other_done.wait(2), 'unrelated owner serialized behind network acquire'
        finally:
            resume.set()
            slow.result()
            fast.result()
            lifecycle.release_all()


def test_cleanup_defers_release_and_owned_daemon_stop_until_request_finishes(monkeypatch):
    calls = []
    config = {'endpoint': 'http://127.0.0.1:19001', 'installation_id': 'test'}

    def post(config, action, **payload):
        calls.append(action)
        return {'capability': 'owner', 'request_id': 'req', 'daemon_name': 'lease-owner',
                'endpoint': config['endpoint'] + '/lease/owner'}

    monkeypatch.setattr(lifecycle, '_post', post)
    monkeypatch.setattr(cli, '_find_cli', lambda: ['test-harness'])
    stopped = []
    def run(cmd, code, env, timeout):
        stopped.append((cmd, env))
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(cli, '_run_cli_killing_process_group', run)
    env = {'BH_RUNTIME_DIR': '/isolated/runtime'}
    with lifecycle.request_lease(config, 'owner', '', 30, env):
        lifecycle.release_task('owner')
        assert calls == ['acquire']
        assert stopped == []
    assert calls == ['acquire', 'complete', 'release']
    assert stopped[0][0] == ['test-harness', '--reload']
    assert stopped[0][1]['BU_NAME'] == 'lease-owner'
    assert stopped[0][1]['BH_RUNTIME_DIR'] == '/isolated/runtime'
    assert stopped[0][1]['BH_RUNTIME_DIR_SHARED'] == '1'


def test_named_sessions_keep_separate_vault_supervisors(monkeypatch):
    config = {'endpoint': 'http://127.0.0.1:19001', 'installation_id': 'test'}

    def post(config, action, **payload):
        cap = payload['owner'][2].replace(':', '-') if action == 'acquire' else 'unused'
        return {'capability': cap, 'request_id': 'req', 'daemon_name': 'lease-' + cap,
                'endpoint': config['endpoint'] + '/lease/' + cap}

    monkeypatch.setattr(lifecycle, '_post', post)
    monkeypatch.setattr(cli, '_find_cli', lambda: None)
    keys = []
    try:
        for session in ('first', 'second', 'first'):
            with lifecycle.request_lease(config, 'vault-task', session, 30, {}):
                keys.append(lifecycle.supervisor_task('vault-task'))
        assert keys[0] != keys[1]
        assert keys[0] == keys[2]
    finally:
        lifecycle.release_all()


def test_expired_capability_rotation_retires_old_daemon(monkeypatch):
    config = {'endpoint': 'http://127.0.0.1:19001', 'installation_id': 'test'}
    grants = iter(('old', 'new'))
    stopped = []

    def post(config, action, **payload):
        if action != 'acquire':
            return {}
        cap = next(grants)
        return dict(capability=cap, request_id='req', daemon_name='lease-' + cap,
                    endpoint=config['endpoint'] + '/lease/' + cap)

    monkeypatch.setattr(lifecycle, '_post', post)
    monkeypatch.setattr(cli, '_find_cli', lambda: ['test-harness'])
    def run(cmd, code, env, timeout):
        stopped.append(env['BU_NAME'])
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(cli, '_run_cli_killing_process_group', run)
    try:
        for _ in range(2):
            with lifecycle.request_lease(config, 'rotating-owner', '', 30, {}):
                pass
        assert stopped == ['lease-old']
    finally:
        lifecycle.release_all()
    assert stopped == ['lease-old', 'lease-new']


def test_managed_vault_exception_never_falls_back_to_unscoped_eval(monkeypatch):
    from unittest.mock import Mock
    from tools import browser_vault_tool as vault
    from tools import browser_tool_session
    from tools.browser_supervisor import SUPERVISOR_REGISTRY
    monkeypatch.setattr(lifecycle, 'supervisor_task', lambda task: 'managed-scope')
    supervisor = Mock()
    supervisor.evaluate_runtime.side_effect = RuntimeError('connection disappeared')
    monkeypatch.setattr(SUPERVISOR_REGISTRY, 'get', lambda task: supervisor)
    raw = Mock(side_effect=AssertionError('unscoped fallback'))
    monkeypatch.setattr(browser_tool_session, '_run_browser_command', raw)
    result = vault._eval_js('task', 'document.title')
    assert result['success'] is False
    raw.assert_not_called()


def test_failed_daemon_stop_retries_before_owner_reuse(monkeypatch):
    from unittest.mock import Mock
    config = {'endpoint': 'http://127.0.0.1:19001', 'installation_id': 'test'}
    grant = dict(capability='retry', request_id='req', daemon_name='lease-retry',
                 endpoint=config['endpoint'] + '/lease/retry')
    monkeypatch.setattr(lifecycle, '_post', lambda *args, **kwargs: grant)
    monkeypatch.setattr(cli, '_find_cli', lambda: ['test-harness'])
    stop = Mock(side_effect=[subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0),
                             subprocess.CompletedProcess([], 0)])
    monkeypatch.setattr(cli, '_run_cli_killing_process_group', stop)
    try:
        with lifecycle.request_lease(config, 'retry-owner', '', 30, {}):
            pass
        lifecycle.release_task('retry-owner')
        with lifecycle.request_lease(config, 'retry-owner', '', 30, {}):
            assert stop.call_count == 2
    finally:
        lifecycle.release_all()
