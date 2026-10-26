"""Real SQLite/CLI-process handoff through the server-bound Desktop RPC.

No model/network calls: exercises durable admission, scheduled cooperative stop,
RPC ownership and transcript identity rather than an idle registry-only owner.
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tui_gateway import server
from tests.waiting import wait_until

REPO = Path(__file__).resolve().parents[2]
CHILD = r"""
import json, os, sys, threading
from pathlib import Path
from types import SimpleNamespace
from hermes_state import SessionDB
from agent.turn_facade_lease import DurableTurnLease
from hermes_state_pidns import holder_namespace_token
home = Path(sys.argv[1])
db = SessionDB(home / 'state.db')
holder = f'pid={os.getpid()}{holder_namespace_token()}:turn=child:platform=cli'
assert db.try_acquire_session_turn_lease('root', holder)
stopped = threading.Event()
agent = SimpleNamespace(session_id='root', _touch_activity=lambda *a: None,
                        interrupt=lambda *a, **k: stopped.set())
lease = DurableTurnLease(agent, db, 'root', holder)
if sys.argv[2] == 'cooperative':
    lease.start()
(home / 'ready.tmp').write_text(json.dumps({'holder': holder, 'pid': os.getpid()}))
(home / 'ready.tmp').replace(home / 'ready.json')
try:
    if sys.argv[2] == 'cooperative':
        assert stopped.wait(30), 'handoff never reached the shared lease'
        # The interrupt alone must not authorize a new writer.
        (home / 'interrupted').touch()
        from tests.waiting import wait_until
        wait_until(lambda: (home / 'release').exists(), timeout=30)
    else:
        sys.stdin.readline()
finally:
    lease.stop_refresher()
    lease.join_threads()
    lease.release()
    (home / 'released').touch()
    db.close()
"""


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home = tmp_path / 'profile'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    db = SessionDB(home / 'state.db')
    db.create_session('root', source='cli')
    db.append_message('root', 'user', 'original')
    db.append_message('root', 'assistant', 'original answer')
    db.end_session('root', 'compression')
    db.create_session('tip', source='cli', parent_session_id='root')
    session = {'agent': None, 'session_key': 'tip', 'profile_home': str(home),
               'history': [], 'history_lock': threading.Lock(), 'history_version': 0,
               'running': False, 'source': 'desktop', 'attached_images': []}
    sid = 'durable-desktop'
    monkeypatch.setitem(server._sessions, sid, session)
    # Reproduce warm Desktop: it already has its registry slot BEFORE CLI starts.
    assert server._ensure_active_session_slot(sid, session) is None
    procs = []
    def spawn(mode):
        proc = subprocess.Popen([sys.executable, '-c', CHILD, str(home), mode],
                                cwd=REPO, env=dict(os.environ), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        procs.append(proc)
        wait_until(lambda: (home / 'ready.json').exists() or proc.poll() is not None, timeout=30)
        assert proc.poll() is None, proc.communicate()
        return proc, json.loads((home / 'ready.json').read_text())
    yield home, db, sid, session, spawn
    for proc in procs:
        if proc.poll() is None:
            (home / 'release').touch()
            proc.communicate(input='finish\n', timeout=35)
    server._release_active_session_slot(session)
    db.close()


def test_warm_desktop_refuses_cli_then_waits_for_actual_release(setup):
    home, db, sid, session, spawn = setup
    proc, ready = spawn('cooperative')
    owner = db.session_turn_lease_owner('tip')
    assert owner['conversation_id'] == 'root'
    assert owner['holder'] == ready['holder']
    before = db.get_messages('tip')
    started = time.monotonic()
    reply = server._methods['prompt.submit']('submit', {'session_id': sid, 'text': 'follow up exactly once'})
    assert time.monotonic() - started < 5
    assert reply['error']['data']['reason'] == 'SESSION_NOT_OWNED'
    assert ready['holder'] in reply['error']['message']
    assert 'session.take_over' in reply['error']['message']
    assert db.get_messages('tip') == before  # refusal happens before accepting/persisting input

    outcome = []
    done = threading.Event()
    def take_over():
        try:
            outcome.append(server._methods['session.take_over']('take', {'session_id': sid}))
        finally:
            done.set()
    worker = threading.Thread(target=take_over)
    worker.start()
    try:
        wait_until(lambda: (home / 'interrupted').exists(), timeout=10)
        assert not done.is_set()  # interrupt != handoff acknowledgement
        assert not db.try_acquire_session_turn_lease('tip', 'competing-writer')
        assert db.session_turn_lease_owner('tip')['holder'] == ready['holder']
        (home / 'release').touch()
        assert done.wait(10)
        assert outcome[0]['result']['status'] == 'taken_over'
        assert outcome[0]['result']['acknowledged'] is True
        assert outcome[0]['result']['previous_owner']['surface'] == 'cli'
        assert outcome[0]['result']['previous_owner']['pid'] == ready['pid']
        assert db.session_turn_lease_owner('tip') is None
        assert server._ensure_active_session_slot(sid, session) is None
        proc.communicate(timeout=10)
        assert proc.returncode == 0
    finally:
        (home / 'release').touch()
        worker.join(15)


def test_older_cli_is_bounded_refusal_and_never_forced_out(setup, monkeypatch):
    _home, db, sid, _session, spawn = setup
    proc, ready = spawn('old')
    monkeypatch.setenv('HERMES_SESSION_TAKEOVER_ACK_TIMEOUT_S', '0.5')
    start = time.monotonic()
    result = server._methods['session.take_over']('take', {'session_id': sid})
    assert time.monotonic() - start < 5
    assert result['error']['data']['reason'] == 'SESSION_NOT_OWNED'
    assert 'older Hermes CLI' in result['error']['message']
    assert db.session_turn_lease_owner('tip')['holder'] == ready['holder']
    assert proc.poll() is None
    assert not db.try_acquire_session_turn_lease('tip', 'competitor')


def test_unavailable_store_refuses_before_accepting_input(setup, monkeypatch):
    import contextlib
    _home, db, sid, session, _spawn = setup
    before = db.get_messages('tip')
    monkeypatch.setattr(server, '_session_db', lambda _session: contextlib.nullcontext(None))
    result = server._methods['prompt.submit']('unavailable', {'session_id': sid, 'text': 'never accepted'})
    assert result['error']['code'] == 4090
    assert session['running'] is False
    assert db.get_messages('tip') == before
    assert '_submit_user_row' not in session


def test_late_desktop_admission_is_bounded_and_own_holder_can_steer(setup):
    from agent.turn_facade_lease import admit_durable_turn_lease
    _home, db, sid, session, spawn = setup
    _proc, ready = spawn('old')
    warnings = []
    agent = SimpleNamespace(_session_db=db, session_id='tip', platform='desktop',
                            _interrupt_requested=False, _emit_warning=warnings.append)
    start = time.monotonic()
    admission = admit_durable_turn_lease(
        agent, session_id='tip', relay_turn_id='desktop',
        task_context={'platform': 'desktop'}, conversation_history=[])
    assert time.monotonic() - start < 5
    assert admission.lease is None
    assert admission.early_result['failure_reason'] == 'session_busy'
    assert 'session.take_over' in admission.early_result['final_response']
    assert db.session_turn_lease_owner('tip')['holder'] == ready['holder']
    # Exact holder equality, not same pid/platform, exempts the running local turn.
    session['agent'] = SimpleNamespace(_active_session_turn_lease_holder=ready['holder'])
    assert server._ensure_active_session_slot(sid, session) is None
    session['agent']._active_session_turn_lease_holder = 'another-generation'
    assert server._ensure_active_session_slot(sid, session).reason == 'SESSION_NOT_OWNED'
