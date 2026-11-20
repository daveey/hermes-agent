"""Authorization uses accepted local custody, never replica chat links."""
import copy
import io
import json
import sqlite3
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_origin_ledger as ledger
from hermes_cli import kanban_selfwork_forward as forward
from hermes_cli import kanban_sync_client as sync


@pytest.fixture
def case(tmp_path, monkeypatch):
    origin = {'gateway': 'origin-gw', 'profile': 'default', 'session': 'chat'}
    creator = {'gateway': 'owner-gw', 'profile': 'default', 'board': 'work', 'task_id': 'task'}
    home = {'gateway': 'home-gw', 'profile': 'default', 'board': 'intake'}
    binding = {'binding_id': 'binding', 'version': 1, 'publication_version': 1,
               'owner': creator, 'origin': origin, 'task_record_key': 'default/work/task'}
    replica = tmp_path / 'replica.db'
    with sqlite3.connect(replica) as conn:
        conn.execute('CREATE TABLE replica (gw TEXT,coll TEXT,key TEXT,version INTEGER,data TEXT)')
        task = {**creator, 'id': 'task', 'status': 'running', 'origin_bindings': [binding]}
        conn.execute('INSERT INTO replica VALUES (?,?,?,?,?)',
                     ('owner-gw', 'tasks', binding['task_record_key'], 1, json.dumps(task)))
    path = tmp_path / 'kanban/origin-ledger.db'
    assert ledger.accept_binding(path=path, replica=replica, binding=binding, request_id='binding-request',
                                 sender='gw:owner-gw', gateway='origin-gw', profile='default',
                                 resolve_session=lambda s: s)['status'] == 'accepted'
    config = {'kanban': {'self_work': {'home': home}}}
    monkeypatch.setattr(forward.owner, '_runtime', lambda: (config, 'origin-gw', 'default', tmp_path))
    monkeypatch.setattr(forward.time, 'time', lambda: 100)
    lease = {'task_id': 'task', 'run_uid': 'run', 'holder': 'worker-gw', 'expires': 101000}
    page = {'head': 1, 'table': 'leases', 'rows': [lease], 'more': False, 'next': 'task'}
    calls = []

    def snapshot(board, table, after):
        calls.append((board, table, after))
        return copy.deepcopy(page)

    monkeypatch.setattr(sync, 'load_identity', lambda: SimpleNamespace(gateway='origin-gw'))
    def hub(identity, **kwargs):
        assert identity.gateway == 'origin-gw'
        return SimpleNamespace(snapshot_page=snapshot)
    monkeypatch.setattr(sync, 'Hub', hub)
    request = {'id': 'forward-request', 'from': 'gw:worker-gw', 'to_gw': 'origin-gw',
               'coll': 'sessions', 'key': 'default/chat', 'action': 'selfwork-forward',
               'args': {'origin': origin, 'creator': {**creator, 'run_uid': 'run'},
                        'create': {'title': 'Improve', 'self_work': {
                            'version': 1, 'repository': 'hermes-core', 'area': 'test', 'kind': 'sweep'}}}}
    return SimpleNamespace(request=request, path=path, page=page, lease=lease, calls=calls,
                           home=home, binding=binding, config=config)


def test_reject_mismatched_enrollment(case, monkeypatch):
    monkeypatch.setattr(sync, 'load_identity', lambda: SimpleNamespace(gateway='foreign'))
    with pytest.raises(ValueError, match='self_work_route_mismatch'):
        forward.receive(case.request)


def test_reject_invalid_declaration(case):
    case.request['args']['create']['self_work'] = {'area': 'test'}
    with pytest.raises(ValueError):
        forward.receive(case.request)
    assert not case.calls


@pytest.mark.parametrize('kind', ['cursor', 'duplicate', 'unsorted'])
def test_reject_malformed_inventory(case, kind):
    if kind == 'cursor':
        case.page['next'] = 'wrong'
    elif kind == 'duplicate':
        case.page['rows'].append(copy.deepcopy(case.lease))
    else:
        case.page['rows'].insert(0, {**case.lease, 'task_id': 'zzz'})
    with pytest.raises(ValueError, match='self_work_lease_invalid'):
        forward.receive(case.request)


def test_command_offline_is_pending(case, monkeypatch, capsys):
    def offline():
        raise sync.SyncUnavailable('offline')
    monkeypatch.setattr(sync, 'load_identity', offline)
    monkeypatch.setattr(forward.sys, 'stdin', io.StringIO(json.dumps(case.request)))
    forward.command(SimpleNamespace(request_file='-'))
    assert json.loads(capsys.readouterr().out)['status'] == 'pending'


def test_accept_journal_and_expired_replay(case, monkeypatch):
    result = forward.receive(case.request)
    assert result['code'] == 'self_work_forward_authorized'
    assert result['handoff_complete'] is False
    downstream = result['downstream']
    assert downstream['key'] == 'default/intake'
    assert downstream['args']['origin'] == case.request['args']['origin']
    assert downstream['args']['create']['session_id'] == 'chat'
    assert downstream['args']['create']['board'] == 'intake'
    with sqlite3.connect(case.path) as conn:
        evidence = json.loads(conn.execute('SELECT evidence_json FROM selfwork_forward_admissions').fetchone()[0])
    assert evidence['binding'] == case.binding
    assert evidence['lease'] == case.lease
    monkeypatch.setattr(forward.time, 'time', lambda: 200)
    assert forward.receive(case.request) == result
    assert case.calls == [('work', 'leases', '')]


@pytest.mark.parametrize('field,value', [('task_id', 'other'), ('run_uid', 'other'),
                                         ('holder', 'owner-gw'), ('expires', 100000),
                                         ('expires', float('inf')), ('expires', '101000'),
                                         ('expires', True)])
def test_bad_lease(case, field, value):
    case.lease[field] = value
    with pytest.raises(ValueError, match='self_work_lease_invalid'):
        forward.receive(case.request)


@pytest.mark.parametrize('change', [{'rows': None}, {'more': 1}, {'head': True}, {'table': 'tasks'},
                                    {'more': True, 'next': ''}, {'rows': [{}]},
                                    {'more': True}, {'rows': []}])
def test_bad_pages(case, change):
    case.page.update(change)
    with pytest.raises(ValueError, match='self_work_lease_invalid'):
        forward.receive(case.request)
    assert len(case.calls) <= forward._MAX_PAGES


@pytest.mark.parametrize('sql', [
    'DELETE FROM origin_receipts', 'DELETE FROM origin_obligations',
    'UPDATE origin_obligations SET released=1',
    "UPDATE origin_obligations SET binding_hash='forged'",
    "UPDATE origin_receipts SET receipt_json='{}'",
])
def test_no_local_authority(case, sql):
    with sqlite3.connect(case.path) as conn:
        conn.execute(sql)
    with pytest.raises(ValueError, match='self_work_creator_unauthorized'):
        forward.receive(case.request)
    assert not case.calls


@pytest.mark.parametrize('field', ['gateway', 'profile', 'board', 'task_id'])
def test_exact_creator(case, field):
    case.request['args']['creator'][field] = 'wrong'
    with pytest.raises(ValueError, match='self_work_creator_unauthorized'):
        forward.receive(case.request)


def test_transaction_rechecks_obligation(case, monkeypatch):
    real = forward._lease
    def release(*args):
        evidence = real(*args)
        with sqlite3.connect(case.path) as conn:
            conn.execute('UPDATE origin_obligations SET released=1')
        return evidence
    monkeypatch.setattr(forward, '_lease', release)
    with pytest.raises(ValueError, match='self_work_creator_unauthorized'):
        forward.receive(case.request)


@pytest.mark.parametrize('change', ['envelope', 'home', 'route'])
def test_replay_conflicts(case, change):
    forward.receive(case.request)
    if change == 'envelope':
        case.request['args']['create']['title'] = 'changed'
    elif change == 'home':
        case.home['board'] = 'changed'
    else:
        case.request['key'] = 'default/other'
    with pytest.raises(ValueError, match='self_work_(forward_conflict|route_mismatch)'):
        forward.receive(case.request)
    assert len(case.calls) == 1


def test_offline_closed(case, monkeypatch):
    def offline():
        raise sync.SyncUnavailable('offline')
    monkeypatch.setattr(sync, 'load_identity', offline)
    with pytest.raises(ValueError, match='self_work_lease_unavailable'):
        forward.receive(case.request)


@pytest.mark.parametrize('field,value', [('from', 'user:worker-gw'), ('to_gw', 'other'),
                                         ('coll', 'tasks'), ('action', 'create'), ('key', 'other/chat')])
def test_route(case, field, value):
    case.request[field] = value
    with pytest.raises(ValueError, match='self_work_route_mismatch'):
        forward.receive(case.request)


@pytest.mark.parametrize('changed_head', [False, True])
def test_pagination(case, monkeypatch, changed_head):
    def snapshot(board, table, after):
        if not after:
            return {**case.page, 'rows': [{**case.lease, 'task_id': 'another'}],
                    'more': True, 'next': 'another'}
        assert after == 'another'
        return {**case.page, 'head': 2 if changed_head else 1}
    monkeypatch.setattr(sync, 'Hub', lambda *a, **k: SimpleNamespace(snapshot_page=snapshot))
    if changed_head:
        with pytest.raises(ValueError, match='self_work_lease_invalid'):
            forward.receive(case.request)
    else:
        assert forward.receive(case.request)['status'] == 'accepted'


def test_page_bound(case, monkeypatch):
    calls = []
    def snapshot(board, table, after):
        calls.append(after)
        cursor = f'other-{len(calls):03d}'
        return {**case.page, 'rows': [{**case.lease, 'task_id': cursor}],
                'more': True, 'next': cursor}
    monkeypatch.setattr(sync, 'Hub', lambda *a, **k: SimpleNamespace(snapshot_page=snapshot))
    with pytest.raises(ValueError, match='self_work_lease_invalid'):
        forward.receive(case.request)
    assert len(calls) == forward._MAX_PAGES


def test_cli_dispatch(case, monkeypatch, capsys):
    from hermes_cli.kanban import kanban_command
    monkeypatch.setattr(forward.sys, 'stdin', io.StringIO(json.dumps(case.request)))
    assert kanban_command(SimpleNamespace(kanban_action='selfwork-forward', request_file='-', json=True)) == 0
    assert json.loads(capsys.readouterr().out)['code'] == 'self_work_forward_authorized'


def test_command_stdin(case, monkeypatch, capsys):
    monkeypatch.setattr(forward.sys, 'stdin', io.StringIO(json.dumps(case.request)))
    assert forward.command(SimpleNamespace(request_file='-', json=True)) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'accepted'


def _seed_local_creator(tmp_path, monkeypatch, *, current=True, session='chat', created_on='origin-gw',
                       holder=None):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_sync as sync
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    kb.init_db()
    monkeypatch.setattr(sync, 'sync_enabled', lambda: True)
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing() as conn:
        sync.install_or_remove(conn, str(kb.kanban_db_path()))
        tid = kb.create_task(conn, title='detector')
        conn.execute("UPDATE tasks SET created_on=?, status='running', current_run_id=1 WHERE id=?",
                     (created_on, tid))
        conn.execute("DELETE FROM task_runs WHERE task_id=?", (tid,))
        conn.execute("INSERT INTO task_runs(id,task_id,status,started_at,ended_at,uid) VALUES(1,?,'running',1,?,?)",
                     (tid, None if current else 2, 'run'))
        if not current:
            conn.execute("UPDATE tasks SET current_run_id=2, status='ready' WHERE id=?", (tid,))
            conn.execute("INSERT INTO task_runs(id,task_id,status,started_at,uid) VALUES(2,?,'running',3,'other')",
                         (tid,))
        conn.execute("INSERT INTO task_events(task_id,run_id,kind,payload,created_at) VALUES(?,?,?,?,1)",
                     (tid, 1, 'owner_admitted', json.dumps({'session_id': session})))
        if holder is not None:
            conn.execute("INSERT INTO task_events(task_id,run_id,kind,payload,created_at) VALUES(?,?,?,?,1)",
                         (tid, 1, 'claimed', json.dumps({'holder': holder})))
        conn.commit()
    return tid


def _journal_rejected(tmp_path, monkeypatch, request, *, code='self_work_creator_unauthorized', envelope=None,
                     at=1):
    hub = tmp_path / 'hub-state'
    hub.mkdir(exist_ok=True)
    stored = envelope if envelope is not None else {k: request[k] for k in
                                                    ('id', 'from', 'to_gw', 'coll', 'key', 'action', 'args')
                                                    if k in request}
    with sqlite3.connect(hub / 'hub.db') as conn:
        conn.execute('CREATE TABLE req_ledger (id TEXT PRIMARY KEY, state TEXT, result TEXT, at REAL, '
                     'envelope_json TEXT, phase TEXT, context_json TEXT)')
        conn.execute('INSERT INTO req_ledger VALUES (?,?,?,?,?,?,?)',
                     (request['id'], 'finished',
                      json.dumps(['rejected', {'status': 'rejected', 'code': code, 'handoff_complete': False}]),
                      at, json.dumps(stored, sort_keys=True, separators=(',', ':'), ensure_ascii=False),
                      'finished', '{}'))
    monkeypatch.setenv('HERMES_HUB_STATE', str(hub))


def _local_request(case, tid):
    case.request['args']['creator'] = {'gateway': 'origin-gw', 'profile': 'default', 'board': 'default',
                                       'task_id': tid, 'run_uid': 'run'}
    case.lease['task_id'] = tid
    case.page['next'] = tid
    return case.request


def test_local_detector_outbound_prepare_send_to_receiver(case, tmp_path, monkeypatch):
    from hermes_cli import kanban_origin, kanban_selfwork_outbound as outbound
    tid = _seed_local_creator(tmp_path, monkeypatch, holder='worker-gw')
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.setenv('HERMES_KANBAN_WORKER_TASK', tid)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', '1')
    monkeypatch.setenv('HERMES_ORIGIN_SESSION', '@session:default@origin-gw/chat')
    monkeypatch.setattr(kanban_origin, '_replica_task_owners',
                        lambda task, board: [('origin-gw', 'default')])
    home, body, request_id = outbound.prepare({
        'title': 'Improve', 'idempotency_key': 'detector-transport', 'tenant': 'hermes',
        'self_work': case.request['args']['create']['self_work']})
    assert body['creator'] == {'gateway': 'origin-gw', 'profile': 'default', 'board': 'default',
                               'task_id': tid, 'run_uid': 'run'}
    case.lease['task_id'] = tid
    case.page['next'] = tid
    received = []

    def courier(argv, **kwargs):
        assert argv[2:6] == ['origin-gw', 'sessions', 'default/chat', 'selfwork-forward']
        rid = argv[argv.index('--id') + 1]
        envelope = {'id': rid, 'from': 'gw:worker-gw', 'to_gw': argv[2],
                    'coll': argv[3], 'key': argv[4], 'action': argv[5],
                    'args': json.loads(kwargs['input'])}
        received.append(forward.receive(envelope))
        return SimpleNamespace(returncode=0, stdout=rid + '\n', stderr='')

    monkeypatch.setattr(outbound.subprocess, 'run', courier)
    queued = outbound.send(home, body, request_id)
    assert queued['created'] is False
    assert queued['handoff_complete'] is False
    assert received[0]['code'] == 'self_work_forward_authorized'
    assert received[0]['request_id'] == request_id
    assert received[0]['downstream']['args']['creator'] == body['creator']
    assert len(case.calls) == 1


def test_origin_local_live_creator_does_not_need_remote_obligation(case, tmp_path, monkeypatch):
    tid = _seed_local_creator(tmp_path, monkeypatch)
    result = forward.receive(_local_request(case, tid))
    assert result['code'] == 'self_work_forward_authorized'
    assert result['downstream']['args']['creator']['task_id'] == tid
    assert result['downstream']['args']['creator']['gateway'] == 'origin-gw'
    with sqlite3.connect(case.path) as conn:
        evidence = json.loads(conn.execute('SELECT evidence_json FROM selfwork_forward_admissions').fetchone()[0])
    assert evidence['local_origin'] is True
    assert evidence['lease']['run_uid'] == 'run'


@pytest.mark.parametrize('damage', ['session', 'created_on', 'run_uid', 'ended', 'holder'])
def test_origin_local_live_negatives(case, tmp_path, monkeypatch, damage):
    kwargs = {}
    if damage == 'session':
        kwargs['session'] = 'other'
    elif damage == 'created_on':
        kwargs['created_on'] = 'foreign-gw'
    elif damage == 'ended':
        kwargs['current'] = False
    tid = _seed_local_creator(tmp_path, monkeypatch, **kwargs)
    request = _local_request(case, tid)
    if damage == 'run_uid':
        request['args']['creator']['run_uid'] = 'other'
    elif damage == 'holder':
        case.lease['holder'] = 'other-gw'
    with pytest.raises(ValueError, match='self_work_(creator_unauthorized|lease_invalid)'):
        forward.receive(request)


def test_remote_obligation_path_unchanged_without_local_task(case):
    with sqlite3.connect(case.path) as conn:
        conn.execute('DELETE FROM origin_receipts')
        conn.execute('DELETE FROM origin_obligations')
    with pytest.raises(ValueError, match='self_work_creator_unauthorized'):
        forward.receive(case.request)
    assert not case.calls


def test_stopped_local_creator_repairs_same_rejected_envelope(case, tmp_path, monkeypatch):
    tid = _seed_local_creator(tmp_path, monkeypatch, current=False, holder='worker-gw')
    request = _local_request(case, tid)
    _journal_rejected(tmp_path, monkeypatch, request)
    result = forward.receive(request)
    assert result['code'] == 'self_work_forward_authorized'
    assert result['request_id'] == request['id']
    with sqlite3.connect(case.path) as conn:
        evidence = json.loads(conn.execute('SELECT evidence_json FROM selfwork_forward_admissions').fetchone()[0])
    assert evidence['local_origin'] is True
    assert evidence['repair'] == 'rejected_unauthorized'
    assert 'lease' not in evidence or evidence.get('lease') is None
    monkeypatch.setattr(forward.time, 'time', lambda: 200)
    assert forward.receive(request) == result
    assert not case.calls


def test_stopped_local_creator_without_rejected_journal_stays_unauthorized(case, tmp_path, monkeypatch):
    tid = _seed_local_creator(tmp_path, monkeypatch, current=False)
    with pytest.raises(ValueError, match='self_work_creator_unauthorized'):
        forward.receive(_local_request(case, tid))
    assert not case.calls


def test_repair_rejects_mismatched_journal_envelope(case, tmp_path, monkeypatch):
    tid = _seed_local_creator(tmp_path, monkeypatch, current=False, holder='worker-gw')
    request = _local_request(case, tid)
    other = copy.deepcopy(request)
    other['args']['create']['title'] = 'forged'
    _journal_rejected(tmp_path, monkeypatch, request, envelope=other)
    with pytest.raises(ValueError, match='self_work_creator_unauthorized'):
        forward.receive(request)


def test_repair_rejects_other_failure_codes(case, tmp_path, monkeypatch):
    tid = _seed_local_creator(tmp_path, monkeypatch, current=False, holder='worker-gw')
    request = _local_request(case, tid)
    _journal_rejected(tmp_path, monkeypatch, request, code='self_work_route_mismatch')
    with pytest.raises(ValueError, match='self_work_creator_unauthorized'):
        forward.receive(request)


def test_same_gateway_accepted_obligation_still_authorizes(case, tmp_path):
    origin = case.request['args']['origin']
    owner = {'gateway': 'origin-gw', 'profile': 'default', 'board': 'work', 'task_id': 'task'}
    binding = {'binding_id': 'local-binding', 'version': 1, 'publication_version': 1,
               'owner': owner, 'origin': origin, 'task_record_key': 'default/work/task'}
    replica = tmp_path / 'replica-same-gw.db'
    with sqlite3.connect(replica) as conn:
        conn.execute('CREATE TABLE replica (gw TEXT,coll TEXT,key TEXT,version INTEGER,data TEXT)')
        task = {**owner, 'id': 'task', 'status': 'running', 'origin_bindings': [binding]}
        conn.execute('INSERT INTO replica VALUES (?,?,?,?,?)',
                     ('origin-gw', 'tasks', binding['task_record_key'], 1, json.dumps(task)))
    assert ledger.accept_binding(
        path=case.path, replica=replica, binding=binding, request_id='same-gw-binding',
        sender='gw:origin-gw', gateway='origin-gw', profile='default',
        resolve_session=lambda s: s)['status'] == 'accepted'
    case.request['args']['creator'] = {**owner, 'run_uid': 'run'}
    result = forward.receive(case.request)
    assert result['code'] == 'self_work_forward_authorized'
    with sqlite3.connect(case.path) as conn:
        evidence = json.loads(conn.execute(
            'SELECT evidence_json FROM selfwork_forward_admissions WHERE request_id=?',
            (case.request['id'],)).fetchone()[0])
    assert evidence['binding'] == binding
    assert evidence['lease'] == case.lease


def _matching_local_obligation(case, request, damage):
    binding = copy.deepcopy(case.binding)
    binding['owner'] = {k: v for k, v in request['args']['creator'].items() if k != 'run_uid'}
    binding['task_record_key'] = 'default/default/' + binding['owner']['task_id']
    digest = ledger.binding_hash(binding)
    with sqlite3.connect(case.path) as conn:
        conn.execute('UPDATE origin_obligations SET binding_id=?,binding_hash=?,binding_json=?,released=?',
                     (ledger._binding_key(binding), 'forged' if damage == 'hash' else digest,
                      ledger._json(binding), int(damage == 'released')))
        conn.execute('DELETE FROM origin_receipts')
        if damage == 'accepted':
            ack = {'status': 'accepted', 'binding_hash': digest, 'binding_id': binding['binding_id'],
                   'owner': binding['owner'], 'origin': binding['origin']}
            conn.execute('INSERT INTO origin_receipts VALUES (?,?,?)', ('local-ack', digest, ledger._json(ack)))


@pytest.mark.parametrize('historical', [False, True])
@pytest.mark.parametrize('damage', ['released', 'hash', 'receipt'])
def test_invalid_matching_obligation_vetoes_local_fallback(case, tmp_path, monkeypatch, historical, damage):
    tid = _seed_local_creator(tmp_path, monkeypatch, current=not historical, holder='worker-gw')
    request = _local_request(case, tid)
    if historical:
        _journal_rejected(tmp_path, monkeypatch, request)
    _matching_local_obligation(case, request, damage)
    with pytest.raises(ValueError, match='self_work_creator_unauthorized'):
        forward.receive(request)
    assert not case.calls
    with sqlite3.connect(case.path) as conn:
        assert conn.execute('SELECT count(*) FROM selfwork_forward_admissions').fetchone()[0] == 0


@pytest.mark.parametrize('historical', [False, True])
@pytest.mark.parametrize('damage', ['released', 'accepted'])
def test_local_fallback_rechecks_obligation_veto(case, tmp_path, monkeypatch, historical, damage):
    tid = _seed_local_creator(tmp_path, monkeypatch, current=not historical, holder='worker-gw')
    request = _local_request(case, tid)
    if historical:
        _journal_rejected(tmp_path, monkeypatch, request)
    real = forward._historical_proof if historical else forward._lease

    injected = []

    def add_veto(*args, **kwargs):
        proof = real(*args, **kwargs)
        if not injected:
            _matching_local_obligation(case, request, damage)
            injected.append(True)
        return proof

    monkeypatch.setattr(forward, '_historical_proof' if historical else '_lease', add_veto)
    with pytest.raises(ValueError, match='self_work_creator_unauthorized'):
        forward.receive(request)
    with sqlite3.connect(case.path) as conn:
        assert conn.execute('SELECT count(*) FROM selfwork_forward_admissions').fetchone()[0] == 0


def test_historical_repair_rejects_never_holder_sender(case, tmp_path, monkeypatch):
    tid = _seed_local_creator(tmp_path, monkeypatch, current=False, holder='worker-gw')
    request = _local_request(case, tid)
    request['from'] = 'gw:never-held-gw'
    with pytest.raises(ValueError, match='self_work_creator_unauthorized'):
        forward.receive(request)
    _journal_rejected(tmp_path, monkeypatch, request)
    with pytest.raises(ValueError, match='self_work_creator_unauthorized'):
        forward.receive(request)


def test_historical_repair_rejects_journal_before_start(case, tmp_path, monkeypatch):
    tid = _seed_local_creator(tmp_path, monkeypatch, current=False, holder='worker-gw')
    request = _local_request(case, tid)
    _journal_rejected(tmp_path, monkeypatch, request, at=0)
    with pytest.raises(ValueError, match='self_work_creator_unauthorized'):
        forward.receive(request)
    assert not case.calls


@pytest.mark.parametrize('historical', [False, True])
@pytest.mark.parametrize('damage', ['session', 'created_on'])
def test_local_provenance_rechecked_before_admission(case, tmp_path, monkeypatch, historical, damage):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    tid = _seed_local_creator(tmp_path, monkeypatch, current=not historical, holder='worker-gw')
    request = _local_request(case, tid)
    if historical:
        _journal_rejected(tmp_path, monkeypatch, request)
    seam = '_historical_proof' if historical else '_lease'
    real = getattr(forward, seam)
    injected = []

    def change_provenance(*args, **kwargs):
        proof = real(*args, **kwargs)
        if not injected:
            with connect_closing(kb.kanban_db_path('default')) as conn:
                if damage == 'session':
                    conn.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='owner_admitted'",
                                 (json.dumps({'session_id': 'other'}), tid))
                else:
                    conn.execute('UPDATE tasks SET created_on=? WHERE id=?', ('foreign-gw', tid))
                conn.commit()
            injected.append(True)
        return proof

    monkeypatch.setattr(forward, seam, change_provenance)
    with pytest.raises(ValueError, match='self_work_creator_unauthorized'):
        forward.receive(request)
    assert injected == [True]
    with sqlite3.connect(case.path) as conn:
        assert conn.execute('SELECT count(*) FROM selfwork_forward_admissions').fetchone()[0] == 0


def test_historical_repair_rejects_stale_journal_after_stop(case, tmp_path, monkeypatch):
    tid = _seed_local_creator(tmp_path, monkeypatch, current=False, holder='worker-gw')
    request = _local_request(case, tid)
    _journal_rejected(tmp_path, monkeypatch, request, at=3)
    with pytest.raises(ValueError, match='self_work_creator_unauthorized'):
        forward.receive(request)
