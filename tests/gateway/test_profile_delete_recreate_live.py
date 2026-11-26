"""Real gateway + shared serve + isolated plugin-host delete/recreate, without restart."""
import asyncio
import json
import os
import re
import subprocess
import sys

import httpx
import pytest

from tests.gateway import test_multiplex_hot_serve_live as live
from tests.waiting import wait_until

peer, mux = live.peer, live.mux
pytestmark = [pytest.mark.platforms('linux'), pytest.mark.spawns_gateway_lookalike]

PLUGIN_API = '''
import os
import sqlite3
from fastapi import APIRouter
from hermes_constants import get_hermes_home
from hermes_state import SessionDB
home = get_hermes_home()
db = SessionDB(home / 'state.db', read_only=True)
actions = sqlite3.connect(home / 'actions.sqlite3', check_same_thread=False)
actions.execute('PRAGMA journal_mode=WAL')
actions.execute('CREATE TABLE IF NOT EXISTS actions(id INTEGER)')
router = APIRouter()
@router.get('/ping')
def ping():
    return {'pid': os.getpid(), 'open': db._conn is not None}
'''


def _sqlite_targets(pid, home):
    targets = []
    try:
        fds = os.listdir(f'/proc/{pid}/fd')
    except FileNotFoundError:
        return []
    for fd in fds:
        try:
            target = os.readlink(f'/proc/{pid}/fd/{fd}')
        except FileNotFoundError:
            continue
        if str(home) + '/' in target and ('state.db' in target or 'actions.sqlite3' in target):
            targets.append(target)
    return targets


def _port(log, process):
    assert process.poll() is None, log.read_text(errors='replace')[-5000:]
    match = re.search(r'backend listening on [\d.]+:(\d+)', log.read_text(errors='replace'))
    return int(match.group(1)) if match else None


def test_delete_and_cli_recreate_while_gateway_and_serve_survive(mux, tmp_path):
    root, home = mux['root'], mux['boot']
    dashboard = home / 'plugins' / 'custody-fixture' / 'dashboard'
    dashboard.mkdir(parents=True)
    (dashboard / 'manifest.json').write_text(json.dumps({'name': 'custody-fixture', 'api': 'plugin_api.py'}))
    (dashboard / 'plugin_api.py').write_text(PLUGIN_API)
    (home / 'config.yaml').write_text(json.dumps({**mux['config'], 'plugins': {'enabled': ['custody-fixture']}}))
    # A shared-backend plugin retains a bare cross-profile reader, just as the
    # original panel cache did. It belongs to serve, not its isolated host.
    parent_plugin = root / 'plugins' / 'serve-custody'
    parent_plugin.mkdir(parents=True)
    (parent_plugin / 'plugin.yaml').write_text('name: serve-custody\nversion: 1.0.0\n')
    (parent_plugin / '__init__.py').write_text('''
from hermes_constants import get_hermes_home
from hermes_state import SessionDB
db = None
def register(ctx):
    global db
    db = SessionDB(get_hermes_home() / 'profiles' / 'boot' / 'state.db', read_only=True)
''')
    root_config = json.loads((root / 'config.yaml').read_text())
    root_config['plugins'] = {'enabled': ['serve-custody']}
    (root / 'config.yaml').write_text(json.dumps(root_config))
    gateway, initial = live.start_daemon(mux)
    serve = None
    log_path = tmp_path / 'serve.log'
    try:
        with log_path.open('w') as log:
            serve = subprocess.Popen([sys.executable, '-m', 'hermes_cli.main', 'serve', '--isolated',
                                      '--host', '127.0.0.1', '--port', '0'], cwd=live.ROOT,
                                     env={**mux['env'], 'HERMES_DASHBOARD_SESSION_TOKEN': 'custody-test-token'},
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            port = wait_until(lambda: _port(log_path, serve), timeout=90, what='serve listener')
            from tests.e2e.core._service_readiness import service_response
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=90,
                              headers={'X-Hermes-Session-Token': 'custody-test-token'}) as http:
                ready = service_response(http, '/api/status')
                assert ready.status_code == 200, ready.text
                response = http.get('/api/plugins/custody-fixture/ping', params={'profile': 'boot'})
                assert response.status_code == 200, response.text
                child_pid = response.json()['pid']
                assert child_pid not in (gateway.pid, serve.pid, os.getpid())
                assert _sqlite_targets(child_pid, home)
                # Exercise the normal cross-profile session polling path too.
                sessions = http.get('/api/sessions', params={'profile': 'boot'})
                assert sessions.status_code == 200, sessions.text
                assert _sqlite_targets(serve.pid, home)
                assert _sqlite_targets(gateway.pid, home)
                deleted = http.delete('/api/profiles/boot')
                assert deleted.status_code == 200, (deleted.status_code, deleted.text)
                assert not home.exists()
                assert not _sqlite_targets(child_pid, home)
                assert not _sqlite_targets(serve.pid, home)
                assert not _sqlite_targets(gateway.pid, home)
                assert gateway.poll() is None and serve.poll() is None
                for _ in range(3):
                    absent = http.get('/api/plugins/custody-fixture/ping', params={'profile': 'boot'})
                    assert absent.status_code in (404, 400), absent.text
                    assert not home.exists()
                created = subprocess.run([sys.executable, '-m', 'hermes_cli.main', 'profile', 'create', 'boot'],
                                         cwd=live.ROOT, env=mux['env'], text=True, capture_output=True, timeout=120, check=False)
                assert created.returncode == 0, (created.stdout, created.stderr)
                wait_until(lambda: home.resolve() in live.served_homes(live.control(root, 'identify')),
                           timeout=30, what='same-name profile served')
                after = live.control(root, 'identify')
                assert after['instance_id'] == initial['instance_id']
                sid = asyncio.run(live.open_session_for(root, home, after))
                assert sid
                assert gateway.poll() is None and serve.poll() is None
    finally:
        if serve is not None:
            live.stop(serve, expect=None)
        live.stop(gateway, expect=None)
