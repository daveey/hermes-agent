"""One request, one verified Desktop grant, one admission: real receiver chain, real card-answer runtime."""
import copy
import json
import shutil

import pytest

from hermes_cli import kanban_card_answer as ca
from hermes_cli import kanban_card_answer_runtime as runtime
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_selfwork_exception as sx
from hermes_cli.kanban_selfwork_receiver import receive
from tests.hermes_cli import test_kanban_card_answer_proof as proof_helpers
from tests.hermes_cli import test_kanban_selfwork_guard_entry as entry
from tests.hermes_cli import test_kanban_selfwork_guards as guards

world, nochat_envelope, request_envelope = entry.world, entry.nochat_envelope, entry.request_envelope


class Hub:
    """The Hub's request records as this home would read them with its own credential."""
    gateway = 'owner'

    def __init__(self):
        self.rows, self.bytes = {}, {}

    def read(self, rid):
        return copy.deepcopy(self.rows[rid])

    def raw(self, rid):
        if rid in self.bytes:
            return self.bytes[rid]
        if rid not in self.rows:
            raise ca.AnswerRefused('courier_record_missing')
        return json.dumps({'request': self.rows[rid]}).encode()


@pytest.fixture
def home(request_envelope, monkeypatch, tmp_path):
    """A frozen home holding the owner card, and the Hub record of the request it just rejected."""
    envelope = {**request_envelope, 'base_version': None}
    envelope['args']['create']['self_work']['kind'] = 'guard'
    rejection = receive(copy.deepcopy(envelope))
    assert rejection['code'] == 'self_work_guard_frozen', rejection
    hub = Hub()
    hub.rows[envelope['id']] = {**copy.deepcopy(envelope), 'state': 'rejected', 'result': rejection, 'claims': 1}
    with kbc.connect_closing() as conn:
        conn.execute("UPDATE kanban_sync_state SET mode='primary'")
        conn.commit()
        tid = kb.create_task(conn, title='Design and deliver accounting recovery', assignee='default')
        kb.claim_task(conn, tid)
        assert kb.block_task(conn, tid, reason='Needs the exception', kind='needs_input', human_request={
            'action': 'Grant the exception. Unblock = grant; archive = decline.', 'why_human': 'policy',
            'interaction': 'approval', 'scope': 'exception for ' + envelope['id'], 'evidence': 'rejected'})
    monkeypatch.setattr(sx, 'TARGET', sx.Target(
        envelope['id'], sx._sha({k: envelope[k] for k in sx.ENVELOPE}), sx._sha(rejection), tid, 'approval/1'))
    monkeypatch.setattr(runtime, 'Records', lambda: hub)
    monkeypatch.setattr(runtime, '_profile', lambda: ('default', tmp_path))
    return {'envelope': envelope, 'hub': hub, 'task': tid, 'dir': tmp_path, 'rejection': rejection}


def click(home, choice='unblock', operation='desktop-operation-one'):
    """David's answer, delivered the way the Hub delivers it: two record IDs to the owner runtime."""
    with kbc.connect_closing() as conn:
        answer = dict(ownerGateway='owner', ownerProfile='default', board='default', task=home['task'],
                      generation=ca.snapshot(conn, home['task'])['generation'], choice=choice,
                      operationId=operation)
    _, rows = proof_helpers.records()
    for row in rows.values():
        row['args']['answer'] = copy.deepcopy(answer)
    rows['original']['key'] = 'default/default/' + home['task']
    rows['proof']['result']['answer'] = copy.deepcopy(answer)
    home['hub'].rows.update(rows)
    return runtime.owner('original', 'proof')


def offer(home, **kw):
    with kbc.connect_closing() as conn:
        return sx.offer(conn, home['hub'], **kw)


def reconsider(home):
    with kbc.connect_closing() as conn:
        return sx.reconsider(conn, home['hub'])


def state(home):
    with kbc.connect_closing() as conn:
        journal = None
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='self_work_intake'").fetchone():
            row = conn.execute('SELECT request_json FROM self_work_intake WHERE request_id=?',
                               (home['envelope']['id'],)).fetchone()
            journal = row and row[0]
        return {'status': sx.status(conn), 'journal': journal,
                'tasks': [t for t in kb.list_tasks(conn) if t.id != home['task']],
                'owner': kb.get_task(conn, home['task']),
                'comments': {t.id: len(kb.list_comments(conn, t.id)) for t in kb.list_tasks(conn)}}


def test_offer_click_admit_once(home):
    stamp = offer(home)
    assert offer(home) == stamp  # a live offer is returned, not re-filed
    with kbc.connect_closing() as conn:
        ask = ca.snapshot(conn, home['task'])['ask']
    assert ask[sx.STAMP] == stamp and home['envelope']['args']['create']['title'] in ask['action']
    assert home['envelope']['id'] in ask['action'] and 'Unblock =' in ask['action'] and 'Archive =' in ask['action']
    assert state(home)['owner'].status == 'blocked' and state(home)['status']['grant'] is None
    with pytest.raises(sx.Refused, match='no verified Desktop grant'):
        reconsider(home)

    receipt = click(home)
    assert receipt['decisionProvenance'] == 'recorded_desktop_operation'
    after = state(home)
    assert after['owner'].status == 'ready'  # plain unblock: the card returns to work
    assert after['status']['grant'] == {'offer_uid': stamp['offer_uid'], 'operationId': 'desktop-operation-one'}
    assert after['tasks'] == [] and after['status']['spent'] == []  # the click admits nothing
    assert click_replay(home) == receipt

    result = reconsider(home)
    assert result['created'] is True and result['code'] == 'origin_bind_pending', result
    assert result['request_id'] == home['envelope']['id']
    done = state(home)
    assert [t.id for t in done['tasks']] == [result['task_id']]
    assert done['tasks'][0].created_by == home['envelope']['from'] == 'gw:origin'
    assert [s['offer_uid'] for s in done['status']['spent']] == [stamp['offer_uid']]
    assert done['status']['admission']['task_id'] == result['task_id']
    # The origin obligation is the ordinary one: the same binding the courier path records.
    assert result['origin_creation']['binding']['origin'] == home['envelope']['args']['origin']
    assert result['origin_creation']['child_request_id'].startswith('origin-bind-')

    again = reconsider(home)  # stored receipt; nothing spent or created twice
    assert again['replayed'] is True and again['task_id'] == result['task_id']
    assert len(state(home)['tasks']) == 1 and len(state(home)['status']['spent']) == 1
    # The Hub's rejection record is what it was. The freeze is still on: another guard request in this area is
    # added to the now-open owner, and one in an area with no owner is refused as before.
    assert sx._sha(home['hub'].rows[home['envelope']['id']]['result']) == sx.TARGET.rejection_sha256
    other = copy.deepcopy(home['envelope'])
    other['id'] = 'request-owner-other'
    appended = receive(copy.deepcopy(other))
    assert appended['disposition'] == 'append' and appended['task_id'] == result['task_id'], appended
    other['id'], other['args']['create']['self_work']['area'] = 'request-owner-third', 'gateway'
    assert receive(other)['code'] == 'self_work_guard_frozen'
    assert len(state(home)['tasks']) == 1 and len(state(home)['status']['spent']) == 1


def click_replay(home):
    del home['hub'].rows['proof']  # a recorded decision replays with its source offline
    return runtime.owner('original', 'proof')


def test_journal_equals_ordinary_admission_of_same_envelope(home, monkeypatch):
    """Control: the identical envelope through the ordinary chain with the freeze off, in a copy of the board."""
    from hermes_cli import config
    files = [p for p in home['dir'].iterdir() if p.name.startswith('kanban.db')]
    keep = home['dir'] / 'before'
    keep.mkdir()
    for path in files:
        shutil.copy2(path, keep / path.name)
    original_load = config.load_config

    def unfrozen(*args, **kwargs):
        value = original_load(*args, **kwargs)
        value['kanban']['self_work']['freeze_guards'] = False
        return value
    with monkeypatch.context() as patch:
        patch.setattr(config, 'load_config', unfrozen)
        control = receive(copy.deepcopy(home['envelope']))
    assert control['created'] is True, control
    expected = state(home)
    for path in home['dir'].iterdir():
        if path.name.startswith('kanban.db'):
            path.unlink()
    for path in keep.iterdir():
        shutil.copy2(path, home['dir'] / path.name)

    offer(home)
    click(home)
    result = reconsider(home)
    actual = state(home)
    assert actual['journal'] == expected['journal'] and actual['journal'] is not None
    journal = json.loads(actual['journal'])
    assert set(journal) == {'args', 'intent'} and set(journal['intent']) == {'request_id', 'owner', 'origin'}
    create = home['envelope']['args']['create']
    assert journal['args'] == {**create, 'board': 'default'} and 'tenant' not in create
    assert (journal['args']['session_id'], journal['intent']['origin']) == ('creator', home['envelope']['args']['origin'])
    # Attribution differs from the control by design and is asserted on its own.
    assert expected['tasks'][0].created_by != 'gw:origin'
    assert actual['tasks'][0].created_by == 'gw:origin'
    assert actual['tasks'][0].tenant == expected['tasks'][0].tenant
    assert result['task_id'] == actual['tasks'][0].id


def test_open_owner_is_reused_inside_admission(home):
    stamp = offer(home)
    click(home)
    with kbc.connect_closing() as conn:
        existing = guards.seed(conn, tenant=stamp['tenant'])
    result = reconsider(home)
    assert result['disposition'] == 'append' and result['task_id'] == existing, result
    done = state(home)
    assert [t.id for t in done['tasks']] == [existing] and done['comments'][existing] == 1
    assert len(done['status']['spent']) == 1


@pytest.mark.parametrize('case', ['agent_ask', 'column_stamp', 'foreign_event', 'other_text'])
def test_only_the_homes_own_offer_grants(home, case):
    stamp = offer(home)
    with kbc.connect_closing() as conn, kb.write_txn(conn):
        ask = json.loads(conn.execute('SELECT human_request FROM tasks WHERE id=?', (home['task'],)).fetchone()[0])
        if case == 'agent_ask':  # the same visible text without the stamp
            ask.pop(sx.STAMP)
        elif case == 'column_stamp':  # a stamp written into the synced column, never offered by this board
            ask[sx.STAMP] = {**stamp, 'offer_uid': 'f' * 32}
        elif case == 'other_text':  # the stamp kept on text the home did not write
            ask['action'] += ' Also release everything.'
        else:  # the offer event arrives from another gateway
            conn.execute("DELETE FROM task_events WHERE task_id=? AND kind=?", (home['task'], sx.OFFERED))
            kb._append_event(conn, home['task'], sx.OFFERED, {sx.STAMP: stamp, 'ask_sha256': 'x'})
            columns = {r[1] for r in conn.execute('PRAGMA table_info(task_events)')}
            if 'origin_gateway' not in columns:
                conn.execute('ALTER TABLE task_events ADD COLUMN origin_gateway TEXT')
            conn.execute("UPDATE task_events SET origin_gateway='foreign' WHERE task_id=? AND kind=?",
                         (home['task'], sx.OFFERED))
        conn.execute('UPDATE tasks SET human_request=? WHERE id=?', (json.dumps(ask), home['task']))
    click(home)
    after = state(home)
    assert after['owner'].status == 'ready' and after['status']['grant'] is None  # ordinary unblock, no grant
    with pytest.raises(sx.Refused):
        reconsider(home)
    assert state(home)['tasks'] == []


@pytest.mark.parametrize('choice', ['archive', 'held', 'contained', 'expired'])
def test_no_grant_without_a_clean_unblock(home, monkeypatch, choice):
    stamp = offer(home)
    if choice == 'archive':
        click(home, 'archive')
        assert state(home)['owner'].status == 'archived'
    else:
        if choice == 'held':
            monkeypatch.setattr('hermes_cli.kanban_spend_decision.pending', lambda c, t: True)
        elif choice == 'contained':
            monkeypatch.setattr('hermes_cli.kanban_containment_guard.pending', lambda c, t: True)
        else:
            monkeypatch.setattr(sx.time, 'time', lambda: stamp['expires_at'] + 1)
        with pytest.raises(ca.AnswerRefused):
            click(home)
        assert state(home)['owner'].status == 'blocked'
    assert state(home)['status']['grant'] is None
    with pytest.raises(sx.Refused):
        reconsider(home)
    assert state(home)['tasks'] == []


def _bytes(old, new):
    """Serve the record with one JSON token replaced: the bytes the home reads, not a parsed copy."""
    return lambda hub, row: hub.bytes.__setitem__(row['id'], json.dumps({'request': row}).replace(old, new).encode())


CHANGES = {
    'title': lambda hub, row: row['args']['create'].update(title=row['args']['create']['title'] + '!'),
    'origin': lambda hub, row: row['args']['origin'].update(session='another'),
    'nested': lambda hub, row: row['args']['create']['self_work'].update(area='gateway'),
    'sender': lambda hub, row: row.update({'from': 'gw:other'}),
    'result': lambda hub, row: row.update(result={**row['result'], 'error': 'edited'}),
    'state': lambda hub, row: row.update(state='done'),
    'missing': lambda hub, row: hub.rows.clear(),
    'field': lambda hub, row: row.pop('base_version'),
    'duplicate': _bytes('"claims": 1', '"claims": 1, "claims": 1'),
    'nan': _bytes('"claims": 1', '"claims": NaN'),
}


@pytest.mark.parametrize('change', sorted(CHANGES))
def test_changed_or_ambiguous_original_refuses(home, change):
    CHANGES[change](home['hub'], home['hub'].rows[home['envelope']['id']])
    with pytest.raises(sx.Refused):
        offer(home)
    assert sx.STAMP not in json.dumps(state(home)['owner'].human_request)


def test_original_changed_after_grant_admits_nothing(home):
    offer(home)
    click(home)
    home['hub'].rows[home['envelope']['id']]['args']['create']['body'] = 'a different job'
    with pytest.raises(sx.Refused, match='intent changed'):
        reconsider(home)
    assert state(home)['tasks'] == [] and state(home)['status']['spent'] == []


@pytest.mark.parametrize('case', ['tenant_drift', 'expired', 'spent', 'create_fails'])
def test_admission_refusals_spend_nothing(home, monkeypatch, case):
    stamp = offer(home)
    click(home)
    if case == 'tenant_drift':
        monkeypatch.setattr('hermes_cli.kanban_lanes.lane_tenant', lambda *a, **k: 'another-tenant')
    elif case == 'expired':
        monkeypatch.setattr('hermes_cli.kanban_selfwork_owner.time.time', lambda: stamp['expires_at'] + 1)
    elif case == 'spent':
        with kbc.connect_closing() as conn:
            kb._append_event(conn, home['task'], sx.SPENT, {'offer_uid': stamp['offer_uid']})
    else:
        def fail(*a, **k):
            raise RuntimeError('insert failed after the exception was spent')
        monkeypatch.setattr('hermes_cli.kanban_selfwork.record', fail)
    result = reconsider(home)
    assert result.get('created') is not True and result.get('handoff_complete') is False, result
    after = state(home)
    assert after['tasks'] == [] and after['journal'] is None
    assert len(after['status']['spent']) == (1 if case == 'spent' else 0)  # the rollback un-spends it
    if case == 'create_fails':
        monkeypatch.undo()


def test_capability_is_not_reachable_from_any_envelope_or_caller(home):
    offer(home)
    click(home)
    for key in ('selfwork_exception',):
        forged = copy.deepcopy(home['envelope'])
        forged['args']['create'][key] = {'grant': True}
        with pytest.raises(ValueError, match='self_work_request_invalid'):
            receive(forged)
    with pytest.raises(Exception):
        receive(copy.deepcopy(home['envelope']), exception={'grant': True})
    other = copy.deepcopy(home['envelope'])
    other['args']['create']['title'] = 'Something else entirely'
    with kbc.connect_closing() as conn:
        grant = sx._live_grant(conn)
    with pytest.raises(ValueError, match='self_work_exception_intent_changed'):
        sx._receive(other, grant)
    unbound = sx.Capability('gw:origin', grant)  # never passed through the receiver's hash check
    with kbc.connect_closing() as conn, pytest.raises(Exception, match="not the home's"):
        sx.check(conn, unbound, request_id=home['envelope']['id'], now=0)
    assert state(home)['tasks'] == [] and state(home)['status']['spent'] == []
    # validate_human_request drops the stamp from every supplied ask.
    from hermes_cli.kanban_human_request import validate_human_request
    assert sx.STAMP not in validate_human_request({
        'action': 'a', 'why_human': 'b', 'interaction': 'approval', 'scope': 'c', 'evidence': 'd', sx.STAMP: {}})


def test_module_cli_is_the_ingress_and_takes_no_request(home, monkeypatch, capsys):
    """Both triggers carry no authority: no argument names a request, a grant or an envelope."""
    monkeypatch.setattr('hermes_cli.kanban_card_answer_transport.Records', lambda: home['hub'])

    def run(*argv):
        code = sx.main(list(argv))
        return code, json.loads(capsys.readouterr().out)
    assert run('offer', home['envelope']['id'])[0] == 64 and run('reconsider', '--grant', 'x')[0] == 64
    assert run('reconsider') == (2, {'ok': False, 'refused': 'no verified Desktop grant for this request'})
    code, offered = run('offer')
    assert code == 0 and offered['result']['request_id'] == home['envelope']['id']
    click(home)
    code, admitted = run('reconsider')
    assert code == 0 and admitted['result']['created'] is True, admitted
    code, read = run('status')
    assert code == 0 and read['result']['admission']['task_id'] == admitted['result']['task_id']
    assert read['result']['grant']['operationId'] == 'desktop-operation-one'
    assert [t.id for t in state(home)['tasks']] == [admitted['result']['task_id']]


def test_offer_needs_a_waiting_owner_card_that_asked_for_it(home):
    with kbc.connect_closing() as conn, kb.write_txn(conn):
        ask = json.loads(conn.execute('SELECT human_request FROM tasks WHERE id=?', (home['task'],)).fetchone()[0])
        ask['scope'] = 'something unrelated'
        conn.execute('UPDATE tasks SET human_request=? WHERE id=?', (json.dumps(ask), home['task']))
    with pytest.raises(sx.Refused, match='not a request for this exception'):
        offer(home)
    assert state(home)['tasks'] == []  # the dry run left nothing behind
