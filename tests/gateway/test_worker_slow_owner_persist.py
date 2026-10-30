"""A real worker survives an owner whose state.db write lock is held past the old 20 s receipt bound.

The owner may wait up to its transcript write patience (60 s) for the lock. While a foreign writer
holds it for 25 s, the worker's next ``worker.persist`` reply arrives late but well inside that
patience: the worker must still be listening, finish its turn and exit 0, and the owner must log the
slow request (operation, lock wait, total, session).
"""
import asyncio
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from tests.gateway.fixtures.local_recovery_probe import daemon, rpc, websocket

HOLD_S = 25.0


class Model(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if body.get('messages'):
            self.server.requests.append(body)
            if not self.server.held.is_set():
                # Another Hermes process takes the write lock while the worker waits on inference;
                # the worker's next persist (the assistant row) queues behind it.
                holder = sqlite3.connect(str(self.server.db), isolation_level=None, check_same_thread=False,
                                         timeout=30)
                holder.execute('BEGIN IMMEDIATE')
                self.server.held.set()
                self.server.held_at = time.monotonic()

                def release():
                    holder.rollback()
                    holder.close()
                    self.server.released_at = time.monotonic()
                threading.Timer(HOLD_S, release).start()
        message = {'role': 'assistant', 'content': 'SLOW_OWNER_ACK'}
        usage = {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}
        frame = {'id': 'worker-reply', 'model': 'worker-model', 'usage': usage,
                 'choices': [{'index': 0, 'message': message, 'finish_reason': 'stop'}]}
        kind = 'application/json'
        if body.get('stream'):
            frame['choices'][0]['delta'] = frame['choices'][0].pop('message')
            payload = ('data: ' + json.dumps(frame) + '\n\ndata: [DONE]\n\n').encode()
            kind = 'text/event-stream'
        else:
            payload = json.dumps(frame).encode()
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.mark.platforms("linux")
def test_worker_outlives_an_owner_write_wait_longer_than_twenty_seconds(tmp_path):
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir(mode=0o700)
    user.mkdir()
    peer = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    peer.requests, peer.held, peer.db = [], threading.Event(), home / 'state.db'
    peer.held_at = peer.released_at = None
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{peer.server_port}/v1'
    (home / 'config.yaml').write_text(json.dumps({'gateway': {'multiplex_profiles': False},
        'model': {'provider': 'custom', 'default': 'worker-model', 'base_url': url},
        'auxiliary': {'title_generation': {'enabled': False}}, 'platform_toolsets': {'cli': []}}))
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TZ', 'TIRITH_ENABLED') if k in os.environ}
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url)

    async def create(desc):
        async with websocket(home, desc) as ws:
            response = await rpc(ws, 'session.create', request_id='worker-session', source='cli',
                                 model='worker-model', toolsets=[])
            assert 'result' in response, response
            return response['result']['session_id']
    try:
        with daemon(root, home, env, barrier=False) as (_, desc):
            sid = asyncio.run(create(desc))
            receipt = tmp_path / 'receipt.json'
            result = subprocess.run([sys.executable, str(root / 'tests/gateway/fixtures/agent_persistence_worker.py')],
                cwd=root, env=env, input=json.dumps({'home': str(home), 'url': url, 'session_id': sid,
                                                   'receipt': str(receipt)}) + '\n',
                text=True, capture_output=True, timeout=150, check=False)
            assert peer.held.is_set(), 'the lock was never taken: the test did not exercise a slow owner'
            assert result.returncode == 0, result.stdout + result.stderr
            assert 'worker_receipt_timeout' not in result.stderr, result.stderr
            assert peer.released_at is not None and peer.released_at - peer.held_at >= HOLD_S - 0.5
            proof = json.loads(receipt.read_text())
            assert not proof['failed'], proof
            assert 'SLOW_OWNER_ACK' in proof['result'], proof
            assert proof['finished']['status'] == 'terminal'
            with closing(sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True)) as db:
                rows = db.execute('SELECT role,content FROM messages WHERE session_id=? ORDER BY id', (sid,)).fetchall()
            assert sum(1 for role, content in rows if role == 'assistant' and 'SLOW_OWNER_ACK' in content) == 1, rows
            # The owner names the slow request, once: operation, lock wait, total and session.
            agent_log = home / 'logs' / 'agent.log'
            slow = [line for line in agent_log.read_text(errors='replace').splitlines()
                    if 'slow worker.persist' in line]
            assert len(slow) == 1, slow or agent_log.read_text(errors='replace')[-4000:]
            waits = []
            for line in slow:
                match = re.search(r'operation=(\S+) session=(\S+) lock_wait=([\d.]+)s total=([\d.]+)s outcome=(\S+)',
                                  line)
                assert match, line
                waits.append((float(match[4]), float(match[3]), match[2], match[5]))
            total, lock_wait, session, outcome = max(waits)
            assert total > 20.0 and lock_wait > 20.0 and outcome == 'ok', slow
            assert session == sid, slow
            print(json.dumps({'slow': slow, 'held_s': peer.released_at - peer.held_at}))
    finally:
        peer.shutdown()
        peer.server_close()
        thread.join(timeout=5)
