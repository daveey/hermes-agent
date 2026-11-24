"""Measure real CLI startup in disposable homes (never restart an installed backend).

Run: bash scripts/run-in-hermes-env python3 scripts/bench_serve_startup.py
Timings start immediately before Popen, including managed launcher/bootstrap work.
The BOARD/ANNOTATION hold and SESSIONS canary switches match the approved rollout.
This is a synthetic SQLite inventory, not unit-start/direct+proxy live acceptance.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time


def request(port, path, authenticated, started):
    client = http.client.HTTPConnection('127.0.0.1', port, timeout=.1)
    try:
        headers = {'X-Hermes-Session-Token': 'fixture-benchmark-token'} if authenticated else {}
        client.request('GET', path, headers=headers)
        response = client.getresponse()
        body = response.read()
        observation = {'path': path, 'authenticated': authenticated,
                       'seconds': time.monotonic() - started, 'status': response.status}
        return observation, json.loads(body) if response.status == 200 else None
    except (OSError, http.client.HTTPException):
        return None, None
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', type=int, default=1000)
    parser.add_argument('--deadline', type=float, default=20)
    parser.add_argument('--assert-timings', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    original_home = Path.home() / '.hermes'
    fixture = Path(tempfile.mkdtemp(prefix='serve-startup-', dir=os.environ.get('TMPDIR')))
    home = fixture / 'proof'
    home.mkdir()
    default_home = fixture / '.hermes'
    default_home.mkdir()
    # Borrow installed immutable tools/dependencies; all mutable app state is disposable.
    for target in (home, default_home):
        for name in ('installs', 'tools'):
            (target / name).symlink_to(original_home / name, target_is_directory=True)
    os.environ['HOME'] = str(fixture)
    os.environ['HERMES_HOME'] = str(home)
    (home / 'config.yaml').write_text('model:\n  default: fixture\nplugins:\n  enabled: []\n', encoding='utf-8')
    from hermes_state import SessionDB
    from hermes_cli._launchers import runtime_command

    store = SessionDB(db_path=home / 'state.db')
    try:
        for i in range(args.rows):
            sid = f'benchmark-row-{i}'
            store.create_session(sid, source='cli')
            store.append_message(session_id=sid, role='user', content='benchmark fixture')
    finally:
        store.close()
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    command = runtime_command(root, module='hermes_cli.main') + [
        'serve', '--proof-home', str(home), '--isolated', '--port', str(port)]
    env = dict(os.environ, HERMES_FACTS_SESSIONS='1', HERMES_FACTS_ANNOTATION='0',
               HERMES_FACTS_BOARD='0', HERMES_FACTS_LINKS='0',
               HERMES_DASHBOARD_SESSION_TOKEN='fixture-benchmark-token')
    for key in tuple(env):
        if key.startswith(('HERMES_KANBAN_', 'HERMES_WORKER_', 'HERMES_AGENT_OWNER_')):
            env.pop(key)
    # Do not disable recovery/sweep services: exercise the actual serve lifespan.
    env.pop('HERMES_TEST_ISOLATION', None)
    env.pop('PYTHONPROFILEIMPORTTIME', None)
    paths = ['/api/health', '/api/profiles/sessions?limit=50&min_messages=1',
             '/api/profiles/sessions/sidebar']
    observations, first, errors = [], {}, []
    first_response = None
    auth_rejected = False
    pause = threading.Event()
    with (fixture / 'serve.log').open('wb') as log:
        started = time.monotonic()
        child = subprocess.Popen(command, env=env, cwd=root, stdout=log, stderr=log)
        try:
            while time.monotonic() - started < args.deadline and child.poll() is None:
                for path, authenticated in [(p, True) for p in paths] + [(paths[1], False)]:
                    observation, data = request(port, path, authenticated, started)
                    if observation is None:
                        continue
                    elapsed, status = observation['seconds'], observation['status']
                    first_response = elapsed if first_response is None else first_response
                    observations.append(observation)
                    if not authenticated:
                        auth_rejected |= status in (401, 403)
                        if status == 200:
                            errors.append('unauthenticated sessions returned 200')
                    elif status == 200:
                        assert data is not None
                        rows = data.get('sessions', []) if 'sidebar' not in path else data.get('recents', {}).get('sessions', [])
                        if path == paths[0] or any(row['id'].startswith('benchmark-row-') for row in rows):
                            first.setdefault(path, elapsed)
                        else:
                            errors.append('successful sessions read lacked seeded rows: ' + path)
                if len(first) == len(paths) and auth_rejected:
                    break
                pause.wait(.02)
        finally:
            child.terminate()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
    cold = {p: any(o['path'] == p and o['authenticated'] and o['status'] == 503 for o in observations)
            for p in paths[1:]}
    timing_pass = (first_response is not None and first_response < 1.5
                   and all(first.get(p, float('inf')) < 5 for p in paths[1:]))
    report = {'fixture': str(fixture), 'rows': args.rows, 'first_response': first_response,
              'first_success': first, 'auth_rejected': auth_rejected, 'cold_503': cold,
              'timing_pass': timing_pass, 'errors': errors, 'exit_code': child.returncode,
              'observations': observations}
    (fixture / 'result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'observations'}, indent=2))
    valid = not errors and auth_rejected and all(cold.values()) and len(first) == len(paths)
    return 0 if valid and (timing_pass or not args.assert_timings) else 1


if __name__ == '__main__':
    raise SystemExit(main())
