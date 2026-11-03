"""Desktop ⌘B (POST /api/sessions/{id}/mute) and outcome refresh work against a live gateway.

Both took the offline maintenance lock, which a running gateway always holds, so every ⌘B on a
gateway-served profile failed with "Exclusive maintenance refused" and the park never stuck. They now
write on the live owner's own store, like owner-backfill.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import httpx
import pytest

from tests.gateway.test_normal_runtime_boot import control


def _seed(home: Path, sid: str) -> None:
    from hermes_state import SessionDB

    db = SessionDB(db_path=home / 'state.db')
    try:
        db.create_session(sid, source='desktop')
        db.append_message(sid, role='user', content='fix it')
        db.append_message(sid, role='assistant', content='Fixed and pushed; CI is green.')
    finally:
        db.close()


@pytest.mark.platforms('linux')
def test_mute_and_outcome_refresh_run_on_the_live_owner(tmp_path):
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir(mode=0o700)
    user.mkdir()
    model = {'model': {'provider': 'custom', 'default': 'm', 'base_url': 'http://127.0.0.1:1/v1'},
             'auxiliary': {'title_generation': {'enabled': False}}}
    (home / 'config.yaml').write_text(json.dumps(model))
    _seed(home, 'chat')
    root = Path(__file__).resolve().parents[2]
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TZ', 'TIRITH_ENABLED') if k in os.environ}
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               PYTHONUNBUFFERED='1')
    with (tmp_path / 'gateway.log').open('w+') as log:
        process = subprocess.Popen([sys.executable, '-m', 'gateway.run'], cwd=root, env=env,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline, descriptor = time.monotonic() + 60, {}
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    descriptor = control(home, 'identify')
                    if descriptor.get('state') == 'ready':
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(.1)
            log.seek(0)
            assert descriptor.get('state') == 'ready', (descriptor, log.read())

            def headers():
                # A native grant ticket is single-use: mint one per request, as Desktop does.
                return {'X-Hermes-Gateway-Ticket': control(home, 'session-ticket', {
                    'profile_id': str(home.resolve()), 'instance_id': descriptor['instance_id'],
                    'purpose': 'native-http'})['ticket']}
            with httpx.Client(base_url=descriptor['api_origin'], trust_env=False, timeout=30) as client:
                muted = client.post('/api/sessions/chat/mute', json={'muted': True}, headers=headers())
                assert muted.status_code == 200, muted.text
                assert muted.json()['muted'] is True and muted.json()['outcome']['kind'] == 'muted'
                stored = client.get('/api/sessions/chat/outcome', headers=headers())
                assert stored.status_code == 200, stored.text
                assert stored.json()['outcome']['kind'] == 'muted'

                unmuted = client.post('/api/sessions/chat/mute', json={'muted': False}, headers=headers())
                assert unmuted.status_code == 200, unmuted.text
                assert unmuted.json()['outcome']['kind'] != 'muted'

                refreshed = client.get('/api/sessions/chat/outcome', params={'refresh': 1}, headers=headers())
                assert refreshed.status_code == 200, refreshed.text
                assert client.post('/api/sessions/nope/mute', json={'muted': True}, headers=headers()).status_code == 404
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
