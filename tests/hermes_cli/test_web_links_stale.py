"""Real ASGI warm-stale reads; optional independently installed user-steps router."""
import importlib.util
import os
from pathlib import Path
import sqlite3
import threading
import time
from unittest.mock import AsyncMock

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from hermes_cli import kanban_session_activity as activity
from hermes_cli.kanban_links_refresh import ScopeRefresher
from tests.waiting import wait_until


@pytest.fixture(params=['ledger', 'unreadable-replica'])
def reader_mode(request):
    return request.param


@pytest.fixture(params=['core', 'user-steps'])
def linked_client(request, monkeypatch, tmp_path, reader_mode):
    # A fresh process-lifetime owner keeps parametrizations from sharing cooldowns.
    monkeypatch.setattr(activity, '_links_refresher',
                        ScopeRefresher(lambda scope: activity._refresh_links_scope(scope), 16))
    from hermes_cli import web_server as ws
    from hermes_cli import web_server_dashboard as dashboard
    from hermes_cli import kanban_db_sync

    endpoint = '/api/session-kanban-tasks'
    if request.param == 'user-steps':
        path = Path(os.environ.get('HERMES_TEST_USER_STEPS_API',
                                   str(Path.home() / 'code/hermes-mods/plugins/user-steps/dashboard/plugin_api.py')))
        if not path.is_file():
            pytest.skip('independently shipped user-steps router not installed')
        spec = importlib.util.spec_from_file_location('_stale_links_user_steps', path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        router = module.create_router()
        # Outbox recovery is unrelated to this read. Keep the actual handler and
        # enabled-plugin middleware, but do not start the plugin's outbox writer.
        overrides = dict(ws.app.dependency_overrides)
        for dep in router.dependencies:
            overrides[dep.dependency] = lambda: None
        monkeypatch.setattr(ws.app, 'dependency_overrides', overrides)
        prefixed = APIRouter()
        prefixed.include_router(router, prefix='/api/plugins/user-steps')
        monkeypatch.setattr(ws.app.router, 'routes', list(prefixed.routes) + list(ws.app.router.routes))
        monkeypatch.setattr(ws, '_get_dashboard_plugins', lambda: [{'name': 'user-steps', 'source': 'user'}])
        monkeypatch.setattr(dashboard, '_plugin_enable_sets_here', lambda: ({'user-steps'}, set()))
        monkeypatch.setattr(ws, 'forward_secondary_plugin_request', AsyncMock(return_value=None))
        endpoint = '/api/plugins/user-steps/session-cards'
    monkeypatch.setattr(activity, '_board_db_paths', list)
    monkeypatch.setattr(kanban_db_sync, 'sync_config', dict)
    if reader_mode == 'unreadable-replica':
        from types import SimpleNamespace
        from hermes_cli import kanban_sync_client as sync
        monkeypatch.setattr(kanban_db_sync, 'sync_config', lambda: {'links_reader': 'replica'})
        monkeypatch.setattr(sync, 'load_identity', lambda: SimpleNamespace(gateway='origin'))
        monkeypatch.setattr(sync, 'board_paths', lambda: [('work', tmp_path / 'board.db')])
        monkeypatch.setattr(sync, 'shadow_path', lambda _: tmp_path / 'missing.db')
    monkeypatch.setattr(activity, '_origin_scope', lambda: ('http',))
    monkeypatch.setattr(activity, '_links_fingerprint', lambda: ('first',))
    activity._reset_cache_for_tests()
    client = TestClient(ws.app, raise_server_exceptions=False)
    client.headers[ws._SESSION_HEADER_NAME] = ws._SESSION_TOKEN
    yield client, endpoint
    wait_until(lambda: not activity._links_refresher.busy, what='HTTP refresh cleanup')
    activity._reset_cache_for_tests()
    client.close()


def test_http_warm_stale_does_not_wait_for_fingerprint(linked_client, monkeypatch):
    client, endpoint = linked_client
    card = dict(id='prior', key='owner/default/work/prior', board='work', session='s-chat',
                status='running', unfinished=True, gateway='owner', profile='default')
    monkeypatch.setattr(activity, '_project_remote_links', lambda: [card])
    first = client.get(endpoint, params={'ids': 's-chat'})
    assert first.status_code == 200, first.text
    assert first.json()['tasks'][0]['id'] == 'prior'
    # A handler may request links more than once while warming (cards + rows),
    # already starting a refresh. Exercise the *next eligible* stale read rather
    # than requiring a second start inside the new three-second floor.
    wait_until(lambda: not activity._links_refresher.busy, what='HTTP warm refresh')
    monkeypatch.setattr(activity._links_refresher, '_clock', lambda: time.monotonic() + 3)
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    responses = []

    def fingerprint():
        entered.set()
        assert release.wait(5)
        return ('changed',)

    def read():
        responses.append(client.get(endpoint, params={'ids': 's-chat'}))
        returned.set()

    monkeypatch.setattr(activity, '_links_fingerprint', fingerprint)
    thread = threading.Thread(target=read)
    thread.start()
    try:
        assert entered.wait(2)
        assert returned.wait(2), 'HTTP warm-stale read blocked on fingerprint'
        assert responses[0].status_code == 200
        assert responses[0].json()['tasks'] == first.json()['tasks']
    finally:
        release.set()
        thread.join(5)


def test_http_cold_projection_failure_is_not_empty_success(linked_client, monkeypatch):
    client, endpoint = linked_client

    def fail():
        raise sqlite3.OperationalError('no such table: origin_receipts')

    monkeypatch.setattr(activity, '_project_remote_links', fail)
    assert client.get(endpoint, params={'ids': 's-chat'}).status_code == 500
    assert not activity._links_cache
