"""A managed Kanban run continues across its owner gateway's restart. [daveey]

Actual dispatcher argv, real ``gateway.run`` owners on one disposable home, loopback model. The first
owner is stopped (SIGTERM: the runtime Stop of a gateway shutdown/restart) while the card's turn waits
on the model (or, with ``PROBE_MID_TOOL=1``, while its ``terminal`` tool call runs); the real submitter
starts the second owner on the same home as it reconnects. The run's submitter (the real
``hermes_cli.kanban_worker_client``) must survive the gap and the second owner must continue the same
turn in the same session: one run, one claim, one worker pid, no reclaim/crash/failure.
"""
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

import psutil

from tests.gateway.fixtures.local_recovery_probe import control, daemon, isolate_host_policy
from tests.waiting import wait_until


def restarted_owner(home, previous):
    """The real submitter ensures its next owner; never race it with a second launcher."""
    def ready():
        try:
            desc = control(home, 'identify')
        except (OSError, ValueError):
            return None
        if desc.get('state') != 'ready':
            return None
        assert desc['pid'] != previous['pid'], desc
        assert desc['instance_id'] != previous['instance_id'], desc
        return desc

    desc = wait_until(ready, timeout=120, what='submitter-started replacement owner ready')
    assert desc is not None
    return desc


def stop_descendants(processes):
    """Identity-bound teardown, including an owner whose submitter has already exited."""
    for proc in processes:
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(processes, timeout=10)
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(alive, timeout=10)
    assert not alive, alive

CONTINUE_PREFIX = '[Kanban run continued after a gateway restart]'


class Model(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        messages = body.get('messages', [])
        if messages:
            self.server.requests.append(body)
        users = [m for m in messages if m['role'] == 'user']
        continued = any(isinstance(m.get('content'), str) and m['content'].startswith(CONTINUE_PREFIX) for m in users)
        message = {'role': 'assistant', 'content': 'CONTINUED_COMPLETE'}
        finish = 'stop'
        if messages and body.get('tools') and not continued and not self.server.blocked.is_set():
            self.server.blocked.set()
            if self.server.mid_tool:  # the first turn runs a tool that is still running at the stop
                message = {'role': 'assistant', 'content': None, 'tool_calls': [{'index': 0, 'id': 'midtool-1',
                    'type': 'function', 'function': {'name': 'terminal', 'arguments': json.dumps(
                        {'command': f'touch {self.server.marker} && sleep 120', 'timeout': 300})}}]}
                finish = 'tool_calls'
            else:  # the first turn waits here until its owner is stopped
                self.server.release.wait(120)
        if continued and body.get('tools') and messages[-1]['role'] == 'user':
            message = {'role': 'assistant', 'content': None, 'tool_calls': [{'index': 0, 'id': 'continued-complete',
                'type': 'function', 'function': {'name': 'kanban_complete',
                                                 'arguments': json.dumps({'summary': 'Continued after restart'})}}]}
            finish = 'tool_calls'
        payload = json.dumps({'id': 'cont', 'choices': [{'index': 0, 'message': message, 'finish_reason': finish}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}).encode()
        kind = 'application/json'
        if body.get('stream'):
            payload = ('data: ' + json.dumps({'id': 'cont', 'choices': [{'index': 0, 'delta': message, 'finish_reason': finish}]})
                       + '\n\ndata: [DONE]\n\n').encode()
            kind = 'text/event-stream'
        try:
            self.send_response(200)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


def _wait(predicate, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.1)
    raise AssertionError(f'deadline: {what}')


def main():
    root = Path(__file__).resolve().parents[3]
    home = Path(os.environ['HERMES_HOME'])
    home.mkdir(parents=True, exist_ok=True)
    Path(os.environ['HOME']).mkdir(parents=True, exist_ok=True)
    os.environ['HERMES_KANBAN_HOME'] = str(home)
    policy_root = isolate_host_policy(home)
    peer = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    peer.requests = []
    peer.blocked, peer.release = threading.Event(), threading.Event()
    peer.mid_tool, peer.marker = os.environ.get('PROBE_MID_TOOL') == '1', home / 'tool-started'
    threading.Thread(target=peer.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{peer.server_port}/v1'
    os.environ.update(OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url)
    workspace = home / 'workspace'
    workspace.mkdir(exist_ok=True)
    cfg = {'gateway': {'multiplex_profiles': False}, 'model': {'provider': 'custom', 'default': 'loop-model', 'base_url': url},
           'auxiliary': {'title_generation': {'enabled': False}}, 'platform_toolsets': {'cli': ['terminal', 'file']},
           'terminal': {'backend': 'local'}, 'agent': {'api_max_retries': 1}, 'kanban': {'dispatch_in_gateway': False}}
    (home / 'config.yaml').write_text(json.dumps(cfg), encoding='utf-8')
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    from hermes_cli import kanban_db_dispatch as dispatch
    dispatch._resolve_hermes_argv = lambda: [sys.executable, '-m', 'hermes_cli.main']
    os.environ['HERMES_KANBAN_CRASH_GRACE_SECONDS'] = '0'
    kb.create_board('owned')
    with closing(connect(board='owned')) as conn:
        tid = kb.create_task(conn, title='RESTART_CONTINUE_SENTINEL', body='Acceptance: finish across a restart',
                             assignee='default', workspace_kind='dir', workspace_path=str(workspace))
    env = dict(os.environ)
    receipt, clients = {'task_id': tid, 'policy_root': policy_root}, []
    successor = None
    original_popen = subprocess.Popen

    def spawn(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        if 'hermes_cli.kanban_worker_client' in args[0]:
            clients.append(child)
        return child

    def kinds(conn):
        return [r[0] for r in conn.execute('SELECT kind FROM task_events WHERE task_id=? ORDER BY id', (tid,))]

    def spawned(conn):
        launched = dispatch.dispatch_once(conn, board='owned', max_spawn=1)
        return launched if launched.spawned else None
    try:
        subprocess.Popen = spawn
        with closing(connect(board='owned')) as conn:
            with daemon(root, home, env, barrier=False) as (proc, first):
                assert first['pid'] == proc.pid, first
                receipt['owner_pids'] = [proc.pid]
                launched = _wait(lambda: spawned(conn), 60, 'dispatcher claim and spawn')
                assert [s[0] for s in launched.spawned] == [tid], launched
                task = kb.get_task(conn, tid)
                receipt.update(run_id=task.current_run_id, claim_lock=task.claim_lock)
                assert peer.blocked.wait(90), 'model never reached'
                if peer.mid_tool:
                    _wait(peer.marker.exists, 90, 'the turn\'s terminal tool call started')
                receipt['worker_pid'] = kb.get_task(conn, tid).worker_pid
                proc.terminate()  # the runtime Stop of a gateway shutdown/restart, mid-turn
                proc.wait(60)
            peer.release.set()
            mid = kb.get_task(conn, tid)
            receipt.update(checkpointed='restart_checkpoint' in kinds(conn), client_alive_in_gap=clients[0].poll() is None,
                           gap_status=mid.status, gap_run=mid.current_run_id, gap_claim=mid.claim_lock,
                           gap_worker_pid=mid.worker_pid)
            # The dispatcher ticks during the gap: its stale/dead sweeps must leave the live run alone.
            dispatch.dispatch_once(conn, board='owned', max_spawn=0)
            # connect_gateway ensures an owner during reconnect. A competing Popen either loses
            # startup or appears ready via that owner's socket while recording the wrong PID.
            second = restarted_owner(home, first)
            successor = psutil.Process(second['pid'])
            receipt['owner_pids'].append(successor.pid)
            _wait(lambda: clients[0].poll() is not None, 300, 'worker exit after continuation')
            dispatch.reap_worker_zombies()
            receipt['client_exit'] = list(dispatch._classify_worker_exit(clients[0].pid) or ())
            dispatch._recent_worker_exits.clear()
            dispatch.dispatch_once(conn, board='owned', max_spawn=0)
            task = kb.get_task(conn, tid)
            runs = [list(r) for r in conn.execute('SELECT id, outcome, status FROM task_runs WHERE task_id=?', (tid,))]
            receipt.update(task_status=task.status, failures=task.consecutive_failures, runs=runs, kinds=kinds(conn),
                           clients=len(clients))
        with closing(sqlite3.connect((home / 'state.db').as_uri() + '?mode=ro', uri=True)) as db:
            receipt['admissions'] = [list(r) for r in db.execute(
                'SELECT target_session_id, request_id, status, outcome FROM session_admissions ORDER BY seq')]
        turns = [[m.get('content') for m in r['messages'] if m['role'] == 'user'] for r in peer.requests if r.get('tools')]
        receipt['mid_tool_results'] = sorted({m.get('content') for r in peer.requests for m in r['messages']
                                              if m.get('role') == 'tool' and m.get('tool_call_id') == 'midtool-1'})
        receipt['continued_in_history'] = any(
            any(isinstance(t, str) and 'RESTART_CONTINUE_SENTINEL' in t for t in turn)
            and any(isinstance(t, str) and t.startswith(CONTINUE_PREFIX) for t in turn) for turn in turns)
    finally:
        subprocess.Popen = original_popen
        peer.release.set()
        # Capture still-starting descendants before killing the submitter. Stop it first so it
        # cannot start another owner during cleanup; retain an adopted owner after reparenting.
        descendants = psutil.Process().children(recursive=True)
        if successor is not None and successor not in descendants:
            descendants.append(successor)
        for child in clients:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
        stop_descendants(descendants)
        peer.shutdown()
        peer.server_close()
        (home / 'model-requests.json').write_text(json.dumps(peer.requests, indent=2), encoding='utf-8')
        (home / 'receipt.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
        print(json.dumps(receipt))


if __name__ == '__main__':
    main()
