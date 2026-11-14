"""Exercise actual foreground boundaries, not only the mutation helper."""
import asyncio

import pytest

from hermes_cli import kanban_annotation_host as host
from hermes_cli import kanban_annotation_mutation as mutation
from hermes_cli import kanban_db_connect as db
from tests.hermes_cli import test_kanban_annotation_own_commit as helpers

world = helpers.world


def test_complete_without_lifecycle_hook(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from tests.hermes_cli import kanban_sync_support as support
    home = support.kanban_home(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(host, 'enabled', lambda: True)
    monkeypatch.setattr(host, 'kick', calls.append)
    with db.connect_closing(home / 'kanban.db') as conn:
        tid = kb.create_task(conn, title='annotation own completion', assignee='worker')
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer='worker')
        with mutation.foreground(('home', 'profile')):
            assert kb.complete_task(conn, tid, summary='complete', fire_lifecycle_hook=False)
        assert conn.execute('SELECT status FROM tasks WHERE id=?', (tid,)).fetchone()[0] == 'done'
    assert calls == [('home', 'profile')]


@pytest.mark.parametrize('method,path,expected', [
    ('POST', '/api/plugins/kanban/tasks/t1/complete', True),
    ('PATCH', '/api/plugins/kanban/tasks/t1', True),
    ('DELETE', '/api/plugins/kanban/links', True),
    ('POST', '/api/plugins/kanban/dispatch', False),
    ('GET', '/api/plugins/kanban/tasks', False),
])
def test_real_request_gate_commits_in_scope(world, monkeypatch, method, path, expected):
    from starlette.requests import Request
    from hermes_cli import kanban_session_activity as ksa
    from hermes_cli import web_server as web
    conn, calls = world
    monkeypatch.setattr(ksa, '_origin_scope', lambda: ('home', 'request'))
    monkeypatch.setattr(web, '_has_valid_session_token', lambda r: False)
    monkeypatch.setattr(web, '_has_valid_query_token', lambda *a: False)
    request = Request({'type': 'http', 'method': method, 'path': path, 'headers': [], 'query_string': b'',
                       'app': web.app, 'server': ('test', 80)})
    async def route(req):
        with db.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done'")
        return 'response'
    assert asyncio.run(web._plugin_api_runtime_gate(request, route)) == 'response'
    assert calls == ([('home', 'request')] if expected else [])
    assert mutation._foreground.get() is None


def test_tool_handler_carries_foreground_scope(world, monkeypatch):
    from tools import kanban_tools as tools
    from hermes_cli import kanban_session_activity as ksa
    conn, calls = world
    monkeypatch.setattr(ksa, '_origin_scope', lambda: ('home', 'tool'))
    monkeypatch.setattr(tools, '_remote_lease_refusal', lambda *a: None)
    @tools._kanban_handler('kanban_link')
    def link(args):
        with db.write_txn(conn):
            conn.execute("INSERT INTO task_links VALUES('t1', 't2')")
        return {'ok': True}
    assert link({}) == {'ok': True}
    assert calls == [('home', 'tool')]
    assert mutation._foreground.get() is None


def test_hosted_asgi_uses_profile_custody(world, monkeypatch, tmp_path):
    from fastapi import FastAPI
    from hermes_cli.plugin_host_child import HostRuntime
    from hermes_cli.plugin_host_wire import decode
    from hermes_cli import kanban_session_activity as ksa
    conn, calls = world
    monkeypatch.setattr(ksa, '_origin_scope', lambda: ('secondary-home', 'secondary'))
    app = FastAPI()
    @app.post('/links')
    async def link():
        with db.write_txn(conn):
            conn.execute("INSERT INTO task_links VALUES('t1', 't2')")
        return {'ok': True}
    runtime = HostRuntime.__new__(HostRuntime)
    runtime.asgi_apps = {str(tmp_path / 'api.py'): app}
    runtime._run = asyncio.run
    result = decode(runtime.op_asgi({'dashboard_dir': str(tmp_path), 'api_file': 'api.py',
                                    'plugin': 'kanban', 'method': 'POST', 'path': '/links',
                                    'body': None, 'authenticated': True}))
    assert result['status'] == 200
    assert calls == [('secondary-home', 'secondary')]
    assert mutation._foreground.get() is None
