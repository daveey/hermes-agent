"""The abandonment guard's ownership snapshot is read-only and unknown without proof."""
import hashlib
import json
import os
import sqlite3
import threading
import time

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_origin_snapshot as snap
from hermes_cli.kanban_db_dispatch import _set_worker_pid
from tests.hermes_cli.test_kanban_origin_creation import board as board  # noqa: PLC0414 -- fixture
from tests.hermes_cli.test_kanban_origin_evidence import create_bound
from tests.hermes_cli.test_kanban_origin_ledger import world as world, publish, accept  # noqa: PLC0414
from tui_gateway import kanban_consumer_health as health

BODY = 'Build the thing exactly'


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def env(world, board, monkeypatch, tmp_path):
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    ledger, replica, binding = world
    with kbc.connect_closing() as conn:
        tid = create_bound(conn, binding, body=BODY)
    publish(replica, binding)
    accept(world)
    state = tmp_path / 'state.db'
    with sqlite3.connect(state) as db:
        db.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT)')
        db.execute("INSERT INTO sessions VALUES ('s_current')")
        db.execute("INSERT INTO messages (session_id,role,content) VALUES ('s_current','user',?)", (BODY,))
    return {'ledger': ledger, 'state': state, 'tid': tid, 'binding': binding, 'home': tmp_path,
            'owner': binding['owner'], 'board': tmp_path / 'kanban.db'}


def start_worker(tid):
    with kbc.connect_closing() as conn:
        assert kb.claim_task(conn, tid)
        _set_worker_pid(conn, tid, os.getpid())


@pytest.fixture
def consumer(env):
    stop = threading.Event()
    ready, polled = threading.Event(), threading.Event()
    session = {'session_key': 's_current', 'profile_home': str(env['home'])}

    def run():
        polled.wait(5)
        health.polled(env['home'], 's_current', env['board'])
        ready.set()
        stop.wait(5)
    thread = threading.Thread(target=run, daemon=True)
    health.register(session, stop, thread)
    thread.start()
    polled.set()
    assert ready.wait(3)
    yield {'session': session, 'stop': stop, 'thread': thread}
    stop.set()
    thread.join(3)


def take(env, **kw):
    args = dict(ledger=env['ledger'], state_db=env['state'], gateway='owner', owner=env['owner'],
                binding_id=env['binding']['binding_id'],
                request={'uid': '1', 'digest': sha(BODY)}, home=env['home'], consumer_board=env['board'])
    args.update(kw)
    return snap.ownership_snapshot(**args)


def test_live_needs_running_claim_binding_request_and_consumer(env, consumer):
    start_worker(env['tid'])
    result = take(env)
    assert result['state'] == 'live' and result['reasons'] == []
    assert result['continuation']['kind'] == 'running' and result['continuation']['pid'] == os.getpid()
    assert result['binding']['version'] == env['binding']['version']
    assert result['binding']['publication_version'] == 3
    assert result['consumer']['available'] and result['consumer']['incarnation']
    assert result['request'] == {'uid': '1', 'digest': sha(BODY)}


def test_snapshot_never_writes_or_adopts(env, consumer):
    start_worker(env['tid'])
    paths = [env['ledger'], env['board'], env['state']]
    before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]
    assert take(env)['state'] == 'live'
    assert take(env, request={'uid': '9', 'digest': 'x'})['state'] == 'unknown'
    assert [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths] == before
    with sqlite3.connect(env['ledger']) as db:
        assert db.execute('SELECT observation_version FROM origin_obligations').fetchone() == (3,)


def test_query_only_connection_cannot_write(env):
    from contextlib import closing
    with closing(snap._open(env['ledger'])) as conn, pytest.raises(sqlite3.OperationalError):
        conn.execute('DELETE FROM origin_receipts')


def test_unclaimed_task_has_no_continuation(env, consumer):
    result = take(env)
    assert result['state'] == 'unknown' and result['reasons'] == ['owner_wait_state_unverified']


def test_dead_or_recycled_worker_is_unknown(env, consumer):
    start_worker(env['tid'])
    with kbc.connect_closing() as conn:
        conn.execute('UPDATE task_runs SET worker_pid=2147483000')
    assert take(env)['reasons'] == ['owner_live_continuation_unverified']
    with kbc.connect_closing() as conn:
        conn.execute('UPDATE task_runs SET worker_pid=?, claim_expires=1', (os.getpid(),))
    assert take(env)['reasons'] == ['owner_live_continuation_unverified']


@pytest.mark.parametrize('field,value', [('profile', 'work'), ('board', 'other'), ('task_id', 't_missing')])
def test_wrong_owner_scope_is_unknown(env, consumer, field, value):
    start_worker(env['tid'])
    assert take(env, owner={**env['owner'], field: value})['state'] == 'unknown'


def test_wrong_binding_id_is_unknown(env, consumer):
    start_worker(env['tid'])
    assert take(env, binding_id='binding-other')['state'] == 'unknown'


def test_changed_task_body_or_new_origin_request_is_unknown(env, consumer):
    start_worker(env['tid'])
    with sqlite3.connect(env['state']) as db:
        db.execute("INSERT INTO messages (session_id,role,content) VALUES ('s_current','user','Another ask')")
    assert 'origin_request_superseded' in take(env)['reasons']
    assert 'owner_request_digest_mismatch' in take(env, request={'uid': '2', 'digest': sha('Another ask')})['reasons']


def test_consumer_absent_stop_replacement_and_refusal_invalidate(env, consumer):
    start_worker(env['tid'])
    first = take(env)
    assert first['state'] == 'live'
    epoch = first['consumer']['epoch']
    assert take(env, consumer_board=None)['reasons'] == ['result_consumer_unverified']
    consumer['session']['_kanban_refused_until'] = health.time.monotonic() + 60
    health.invalidate(consumer['session'])  # what the refusal path does
    refused = take(env)
    assert refused['reasons'] == ['result_consumer_unverified']
    consumer['session']['_kanban_refused_until'] = 0
    # Recovery after refusal needs a fresh poll; the discarded receipt does not come back.
    assert take(env)['reasons'] == ['result_consumer_unverified']
    assert take(env)['consumer']['epoch'] > epoch
    consumer['stop'].set()  # Stop / disconnect
    assert take(env)['reasons'] == ['result_consumer_unverified']


def test_replacement_consumer_changes_epoch_and_incarnation(env, consumer):
    start_worker(env['tid'])
    before = take(env)['consumer']
    stop, ready = threading.Event(), threading.Event()
    session = {'session_key': 's_current', 'profile_home': str(env['home'])}

    def run():
        health.polled(env['home'], 's_current', env['board'])
        ready.set()
        stop.wait(5)
    thread = threading.Thread(target=run, daemon=True)
    health.register(session, stop, thread)
    thread.start()
    assert ready.wait(3)
    try:
        after = take(env, expected_consumer_epoch=before['epoch'])
        assert after['consumer']['epoch'] > before['epoch']
        assert 'consumer_epoch_changed' in after['reasons'] and after['state'] == 'unknown'
    finally:
        stop.set()
        thread.join(3)


def test_remote_owner_requires_exact_fresh_evidence(env, consumer):
    start_worker(env['tid'])
    now = time.time()
    local = snap.owner_evidence(env['owner'], env['binding']['binding_id'], now=now)
    assert local['state'] == 'live'
    remote_owner = {**env['owner'], 'gateway': 'elsewhere'}

    def remote(**kw):
        return take(env, gateway='origin', owner=remote_owner, owner_evidence_in=kw.pop('evidence', None), now=now, **kw)
    assert remote()['reasons'] == ['owner_evidence_absent']
    good = {**local, 'owner': remote_owner}
    # Remote readings bind to the ledger obligation's owner, so a mismatching one stays unknown.
    assert remote(evidence=good)['state'] == 'unknown'
    stale = {**local, 'observed_at': now - 120}
    assert take(env, owner_evidence_in=stale, now=now)['state'] == 'live'  # local owner ignores supplied evidence
    assert snap._validate_owner(stale, env['owner'], env['binding']['binding_id'], now) == 'owner_evidence_stale'
    forged = {**local, 'continuation': {'kind': 'running', 'claim_expires': 1, 'pid': 1, 'worker_started_at': 'x'}}
    assert snap._validate_owner(forged, env['owner'], env['binding']['binding_id'], now) == 'owner_live_continuation_unverified'
    assert snap._validate_owner({**local, 'owner': remote_owner}, env['owner'], env['binding']['binding_id'], now) == 'owner_evidence_scope_mismatch'
    assert snap._validate_owner(local, env['owner'], env['binding']['binding_id'], now) is None


def test_remote_owner_composes_with_ledger_when_evidence_matches(env, consumer):
    """Origin side of a remote owner: the ledger obligation names owner gateway 'owner'."""
    start_worker(env['tid'])
    now = time.time()
    local = snap.owner_evidence(env['owner'], env['binding']['binding_id'], now=now)
    # This host plays the origin ('origin' gateway); the owner reading arrives via the courier.
    result = take(env, gateway='origin', owner_evidence_in=local, now=now)
    assert result['state'] == 'live', result
    assert take(env, gateway='origin', owner_evidence_in={**local, 'body_digest': sha('other')}, now=now)['reasons'] \
        == ['owner_request_digest_mismatch']


def test_missing_stores_are_unknown(env, consumer, tmp_path):
    start_worker(env['tid'])
    assert take(env, ledger=tmp_path / 'absent.db')['state'] == 'unknown'
    assert not (tmp_path / 'absent.db').exists()
    assert take(env, state_db=tmp_path / 'absent-state.db')['state'] == 'unknown'
    assert not (tmp_path / 'absent-state.db').exists()


def test_scheduled_without_dispatcher_receipt_is_unknown(env, consumer):
    with kbc.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (env['tid'],))
        conn.execute("INSERT INTO task_events (task_id,kind,payload,created_at) VALUES (?,?,?,?)",
                     (env['tid'], 'continued', json.dumps({'wake_at': time.time() + 600}), int(time.time())))
    assert take(env)['reasons'] == ['owner_dispatcher_unverified']


def test_owner_cli_answers_only_for_its_exact_route_and_writes_nothing(env, monkeypatch, capsys):
    import argparse
    import io
    from hermes_cli import kanban
    from hermes_cli.kanban_parser import build_parser
    cfg = env['home'] / 'mail.json'
    cfg.write_text(json.dumps({'gateway': 'owner'}))
    monkeypatch.setenv('HERMES_MAIL_CONFIG', str(cfg))
    monkeypatch.setenv('HERMES_HUB_STATE', str(env['home'] / 'hub'))
    start_worker(env['tid'])

    def ask(owner, to_gw='owner'):
        request = {'id': 'req-snapshot-1', 'from': 'gw:origin', 'to_gw': to_gw, 'action': 'ownership-snapshot',
                   'args': {'owner': owner, 'binding_id': env['binding']['binding_id']}}
        parser = build_parser(argparse.ArgumentParser().add_subparsers())
        args = parser.parse_args(['ownership-snapshot', '--request-file', '-', '--json'])
        monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(request)))
        assert kanban.kanban_command(args) == 0
        return json.loads(capsys.readouterr().out)

    before = env['board'].read_bytes()
    good = ask(env['owner'])
    assert good['state'] == 'live' and good['continuation']['kind'] == 'running'
    assert ask({**env['owner'], 'gateway': 'elsewhere'})['reasons'] == ['origin_route_mismatch']
    assert ask(env['owner'], to_gw='other')['reasons'] == ['origin_route_mismatch']
    assert env['board'].read_bytes() == before


def test_open_is_strictly_read_only_on_a_wal_store(tmp_path):
    path = tmp_path / "wal.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE t (x)")
        db.execute("INSERT INTO t VALUES (1)")
    holder = sqlite3.connect(path)  # a live writer keeps the WAL in place
    holder.execute("INSERT INTO t VALUES (2)")
    holder.commit()
    before = (path.read_bytes(), (tmp_path / "wal.db-wal").exists())
    conn = snap._open(path)
    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 2
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO t VALUES (3)")
    conn.close()
    assert (path.read_bytes(), (tmp_path / "wal.db-wal").exists()) == before
    holder.close()
    with pytest.raises(sqlite3.Error):
        snap._open(tmp_path / "missing.db")  # never creates or falls back to rw
    assert not (tmp_path / "missing.db").exists()
