"""Managed Chrome entry paths use owner leases, never ambient/shared daemons."""
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tools import browser_use_cli as cli


@pytest.fixture
def managed(tmp_path, monkeypatch):
    calls = []
    owners = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.path, payload))
            if self.path.endswith('/acquire'):
                owner = tuple(payload['owner'])
                cap = owners.setdefault(owner, 'cap-' + str(len(owners)))
                result = dict(capability=cap, request_id='request-' + str(len(calls)),
                              daemon_name='lease-' + cap, endpoint=endpoint + '/lease/' + cap)
            else:
                result = {'ok': True}
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    endpoint = 'http://127.0.0.1:' + str(server.server_port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = {'browser': {'cdp_url': endpoint, 'managed_agent': {
        'endpoint': endpoint, 'installation_id': 'test-installation'}}}
    (tmp_path / 'config.yaml').write_text(json.dumps(config))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.delenv('BROWSER_CDP_URL', raising=False)
    monkeypatch.setattr(cli, '_base_subprocess_env', dict)
    monkeypatch.setattr(cli, '_find_cli', lambda: ['unused-test-cli'])
    monkeypatch.setattr(cli, '_attach_vault_supervisor', lambda *args: None)
    monkeypatch.setattr(cli, '_workspace_dir', lambda task: None)
    monkeypatch.setattr(cli, '_find_screenshot', lambda *args: None)
    monkeypatch.setattr(cli, '_code_url_verdict', lambda code: cli._CodeUrlVerdict('', False))
    # Raw discovery must not precede acquisition, even when idle.
    monkeypatch.setattr('tools.browser_tool_cdp._get_cdp_override',
                        lambda: pytest.fail('unscoped discovery'))
    executions = []

    def run(cmd, code, env, timeout):
        executions.append((code, dict(env), timeout))
        return subprocess.CompletedProcess(cmd, 0, 'ok', '')

    monkeypatch.setattr(cli, '_run_cli_killing_process_group', run)
    yield endpoint, calls, executions
    from tools import browser_agent_lifecycle as lifecycle
    lifecycle.release_all()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.mark.parametrize('local', [False, True])
def test_managed_entry_acquires_before_dispatch_and_completes(managed, local):
    endpoint, calls, executions = managed
    result = json.loads(cli.browser_exec('print(1)', task_id='owner-a', local=local, timeout_s=900))
    assert result['success']
    assert [path for path, _ in calls] == ['/lifecycle/acquire', '/lifecycle/complete']
    assert calls[0][1]['owner'][1:] == ['owner-a', 'default']
    assert calls[0][1]['timeout_s'] == 900
    assert executions[0][1]['BU_CDP_URL'].startswith(endpoint + '/lease/')
    assert executions[0][1]['BU_NAME'].startswith('lease-')
    assert executions[0][1]['BH_RUNTIME_DIR_SHARED'] == '1'
    assert executions[0][1]['BH_TMP_DIR_SHARED'] == '1'


def test_cleanup_releases_only_tasks_own_capabilities(managed):
    from tools.browser_tool_lifecycle import cleanup_browser
    _, calls, executions = managed
    for task in ('owner-a', 'owner-b'):
        for session in ('', 'same'):
            assert json.loads(cli.browser_exec('print(1)', task_id=task, session=session))['success']
    assert len({env['BU_NAME'] for _, env, _ in executions}) == 4
    caps = [payload['capability'] for path, payload in calls if path.endswith('/complete')]
    cleanup_browser('owner-a')
    assert [p['capability'] for path, p in calls if path.endswith('/release')] == caps[:2]
    assert json.loads(cli.browser_exec('print(2)', task_id='owner-b', session='same'))['success']
    assert executions[-1][1]['BU_NAME'] == executions[-2][1]['BU_NAME']


def test_failed_execution_completes_request(managed, monkeypatch):
    _, calls, _ = managed

    def fail(*args):
        raise subprocess.TimeoutExpired('test', 10)

    monkeypatch.setattr(cli, '_run_cli_killing_process_group', fail)
    assert 'timed out' in json.loads(cli.browser_exec('print(1)', task_id='owner-a'))['error']
    assert calls[-1][0] == '/lifecycle/complete'


def test_route_reset_does_not_release_other_sessions_but_shutdown_does(managed):
    from tools.browser_tool_lifecycle import cleanup_all_browsers
    _, calls, executions = managed
    cli.browser_exec('print(1)', task_id='owner-a')
    cli.browser_exec('print(1)', task_id='owner-b')
    cleanup_all_browsers()
    assert not [p for path, p in calls if path.endswith('/release')]
    assert len(executions) == 2  # no --reload for a managed sibling daemon
    cleanup_all_browsers(process_shutdown=True)
    assert len([p for path, p in calls if path.endswith('/release')]) == 2


def test_profile_isolation_and_emergency_release_without_legacy_sessions(managed, tmp_path, monkeypatch):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools import browser_tool as bt
    from tools import browser_tool_lifecycle as lifecycle
    _, calls, executions = managed
    cli.browser_exec('print(1)', task_id='same-task')
    other = tmp_path / 'other'
    other.mkdir()
    (other / 'config.yaml').write_text((tmp_path / 'config.yaml').read_text())
    token = set_hermes_home_override(str(other))
    try:
        cli.browser_exec('print(1)', task_id='same-task')
        lifecycle.cleanup_browser('same-task')
    finally:
        reset_hermes_home_override(token)
    caps = [p['capability'] for path, p in calls if path.endswith('/complete')]
    assert executions[0][1]['BU_NAME'] != executions[1][1]['BU_NAME']
    assert [p['capability'] for path, p in calls if path.endswith('/release')] == [caps[1]]
    monkeypatch.setattr(bt, '_cleanup_done', False)
    monkeypatch.setattr(lifecycle._real_profile, '_terminate_real_profile_chrome', lambda: None)
    monkeypatch.setattr(lifecycle, '_stop_all_lightpanda', lambda: None)
    monkeypatch.setattr(lifecycle, '_reap_orphaned_browser_sessions', lambda: None)
    lifecycle._emergency_cleanup_all_sessions()
    assert [p['capability'] for path, p in calls if path.endswith('/release')] == [caps[1], caps[0]]


def test_managed_setup_failure_and_missing_owner_never_dispatch(managed, monkeypatch):
    from tools import browser_agent_lifecycle as lifecycle
    _, calls, executions = managed
    assert 'identity' in json.loads(cli.browser_exec('print(1)'))['error']
    assert calls == executions == []

    def refused(*args, **kwargs):
        raise lifecycle.ManagedBrowserError('deliberately disabled')

    monkeypatch.setattr(lifecycle, '_post', refused)
    assert 'disabled' in json.loads(cli.browser_exec('print(1)', task_id='owner'))['error']
    assert executions == []


def test_unrelated_override_does_not_use_managed_control(managed, monkeypatch):
    _, calls, executions = managed
    monkeypatch.setattr(cli, '_base_subprocess_env', lambda: {'BU_CDP_URL': 'http://127.0.0.1:19001'})
    assert json.loads(cli.browser_exec('print(1)', task_id='owner'))['success']
    assert not calls
    assert executions[0][1]['BU_CDP_URL'] == 'http://127.0.0.1:19001'


def test_concurrent_default_and_duplicate_named_sessions_are_distinct(managed):
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context
    _, _, executions = managed
    owners = [('owner-a', ''), ('owner-b', ''), ('owner-a', 'default'), ('owner-b', 'default')]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(copy_context().run, cli.browser_exec, 'print(1)', session=session, task_id=task)
                   for task, session in owners]
        assert all(json.loads(future.result())['success'] for future in futures)
    assert len({env['BU_NAME'] for _, env, _ in executions}) == 4
