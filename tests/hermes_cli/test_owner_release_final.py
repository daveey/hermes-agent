"""Final wire custody and actual HTTP/DO-produced certificate contract."""
from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_release_certificate as cert
from hermes_cli import kanban_release_final as subject
from tests.hermes_cli import test_owner_release_assemble as helpers

board = helpers.board
proof = helpers.proof
assembly = helpers.assembly


@pytest.fixture
def canonical():
    return json.loads((Path(__file__).parents[1] / 'fixtures/owner_release_application.json').read_text())


def test_actual_board_private_application_contract(canonical):
    assert subject.validate(canonical, canonical['operation']) == canonical


@pytest.mark.parametrize('damage', ['operation','proof','event_payload','event_origin','event_type','event_run',
                                   'event_order','event_seq','event_keys','comment_body','comment_origin',
                                   'task','allowance_episode','allowance_sum','allowance_remaining','allowance_type',
                                   'allowance_keys','envelope_keys'])
def test_certificate_refuses_adjacent_valid_corruption(canonical, damage):
    wire = copy.deepcopy(canonical['operation'])
    actions = {
        'operation': lambda: canonical['operation'].update(client_op_id='other-operation'),
        'proof': lambda: canonical['consumption']['intent'].update(note='forged'),
        'event_payload': lambda: canonical['events'][0].update(payload='{}'),
        'event_origin': lambda: canonical['events'][0].update(origin_gateway='other-home'),
        'event_type': lambda: canonical['events'][0].update(version=True),
        'event_run': lambda: canonical['events'][0].update(run_uid='foreign-run'),
        'event_order': lambda: canonical['events'][1].update(ord=canonical['events'][0]['ord']),
        'event_seq': lambda: canonical['events'][1].update(seq=canonical['events'][0]['seq']),
        'event_keys': lambda: canonical['events'][0].update(extra=True),
        'comment_body': lambda: canonical['comments'][0].update(body='forged'),
        'comment_origin': lambda: canonical['comments'][0].update(origin_gateway='other-home'),
        'task': lambda: canonical['task'].update(uid='other-task'),
        'allowance_episode': lambda: canonical['allowance'].update(episode_uid='other-episode'),
        'allowance_sum': lambda: canonical['allowance'].update(used=900),
        'allowance_remaining': lambda: canonical['allowance'].update(remaining=0,runs=0,used=0),
        'allowance_type': lambda: canonical['allowance'].update(used=False),
        'allowance_keys': lambda: canonical['allowance'].update(install_permission=True),
        'envelope_keys': lambda: canonical.update(applied=True),
    }
    actions[damage]()
    with pytest.raises(ValueError):
        subject.validate(canonical, wire)


@pytest.mark.parametrize('origin', [None, 'certified-home', 'foreign', '', False, {}])
def test_prepared_comment_origin_only_certified_home(canonical, origin):
    proposal = canonical['operation']['proposal']
    proposal['consumption']['renewal']['origin_gateway'] = 'certified-home'
    proposal['comments'][0]['origin_gateway'] = origin
    if origin is None or origin == 'certified-home':
        subject._proposal_shape(proposal)
    else:
        with pytest.raises(ValueError, match='sequence'):
            subject._proposal_shape(proposal)


@pytest.mark.parametrize('origin', ['certified-home', 'foreign', '', False, {}])
def test_prepared_event_origin_still_requires_null(canonical, origin):
    proposal = canonical['operation']['proposal']
    proposal['consumption']['renewal']['origin_gateway'] = 'certified-home'
    proposal['events'][0]['origin_gateway'] = origin
    with pytest.raises(ValueError, match='sequence'):
        subject._proposal_shape(proposal)


def test_response_cap_distinct_from_proposal_cap(canonical):
    # Historical task postimage may carry a large unrelated body; never source
    # authority and never applied. Complete response is bounded independently.
    canonical['task']['body'] = 'x' * (cert.MAX_BYTES + 1)
    assert subject.validate(canonical, canonical['operation']) == canonical
    canonical['task']['body'] = 'x' * subject.MAX_BYTES
    with pytest.raises(ValueError,match='oversized'):
        subject.validate(canonical, canonical['operation'])


def test_frozen_wire_does_not_apply_trial_or_change_task(assembly):
    from tests.hermes_cli.test_owner_release_prepared import snapshot
    a=assembly
    before=snapshot(a)
    wire=subject.freeze(a.conn,a.proof,delivery_uid='delivery-1')
    assert snapshot(a)==before
    assert wire['op']=='apply_owner_release'
    assert subject.saved(a.conn,'intent-1')==wire
    assert kb.get_task(a.conn,a.tid).status=='triage'
    with kb.write_txn(a.conn):
        a.conn.execute("UPDATE task_chat_links SET released=1 WHERE uid='link-1'")
        kb._append_event(a.conn,a.tid,'spend_budget_triaged',{'cause':'scope_stopped'})
    assert subject.freeze(a.conn,a.proof,delivery_uid='delivery-1')==wire


@pytest.mark.parametrize('mode',['update','delete','replace','rowid','anchor'])
def test_wire_and_independent_anchor_are_immutable(assembly,mode):
    a=assembly
    expected=subject.freeze(a.conn,a.proof,delivery_uid='delivery-1')
    row=a.conn.execute(f'SELECT rowid,* FROM {subject.TABLE}').fetchone()
    sql,args={
        'update':(f"UPDATE {subject.TABLE} SET digest='bad'",()),
        'delete':(f'DELETE FROM {subject.TABLE}',()),
        'replace':(f'INSERT OR REPLACE INTO {subject.TABLE} SELECT * FROM {subject.TABLE}',()),
        'rowid':(f'INSERT OR REPLACE INTO {subject.TABLE}(rowid,intent_uid,delivery_uid,operation_uid,wire,digest) VALUES (?,?,?,?,?,?)',
                 (row[0],'other','other-delivery','other-op',row[4],row[5])),
        'anchor':("UPDATE owner_release_home_queue SET application_digest=NULL",()),
    }[mode]
    with pytest.raises(sqlite3.DatabaseError,match='immutable'):
        with kb.write_txn(a.conn):
            a.conn.execute(sql,args)
    assert subject.saved(a.conn,'intent-1')==expected


def test_deleted_wire_never_remints(assembly):
    a=assembly
    subject.freeze(a.conn,a.proof,delivery_uid='delivery-1')
    a.conn.execute(f'DROP TRIGGER {subject.TABLE}_delete')
    a.conn.execute(f'DELETE FROM {subject.TABLE}')
    a.conn.commit()
    with pytest.raises(ValueError,match='missing; never remint'):
        subject.freeze(a.conn,a.proof,delivery_uid='delivery-1')


def test_frozen_wire_anchor_failure_rolls_back_operation(assembly):
    a=assembly
    subject.install(a.conn)
    a.conn.execute("CREATE TRIGGER fail_anchor BEFORE UPDATE OF application_digest ON owner_release_home_queue BEGIN SELECT RAISE(ABORT,'anchor failure'); END")
    with pytest.raises(sqlite3.DatabaseError,match='anchor failure'):
        subject.freeze(a.conn,a.proof,delivery_uid='delivery-1')
    assert not a.conn.execute(f'SELECT 1 FROM {subject.TABLE}').fetchone()
    assert a.conn.execute('SELECT application_digest FROM owner_release_home_queue').fetchone()[0] is None


@pytest.mark.parametrize('damage',['order','origin','run','outcome','episode_order','comment_len','comment_uid'])
def test_exact_canonical_proposal_shape_without_masking(canonical,damage):
    proposal=canonical['operation']['proposal']
    events=proposal['events']
    releases=[e for e in events if e['kind']=='stop_released']
    commented=next(e for e in events if e['kind']=='commented')
    def comment_payload(key):
        p=cert.parse(commented['payload'])
        p[key]=0
        commented['payload']=cert.encode(p)
    def swap(rows,key=None):
        if key is None:
            rows[0],rows[1]=rows[1],rows[0]
        else:
            rows[0][key],rows[1][key]=rows[1][key],rows[0][key]
    actions={
        'order':lambda:swap(events),
        'origin':lambda:proposal['comments'][0].update(origin_gateway='other'),
        'run':lambda:events[0].update(run_id=1),
        'outcome':lambda:proposal['application']['result'].update(outcome='waiting_on_parents'),
        'episode_order':lambda:swap(releases,'payload'),
        'comment_len':lambda:comment_payload('len'),
        'comment_uid':lambda:comment_payload('comment_uid'),
    }
    actions[damage]()
    with pytest.raises(ValueError):
        subject._proposal_shape(proposal)


@pytest.mark.parametrize('damage',['task','intent','reserved','op','extra'])
def test_final_wire_coordinates_are_not_proposal_cas(canonical,damage):
    op=canonical['operation']
    updates={'task':{'task':'other-task'},'intent':{'intent_uid':'other-intent'},
             'reserved':{'client_op_id':op['proposal']['delivery_uid']},'op':{'op':'upsert'},'extra':{'actor':'human'}}
    op.update(updates[damage])
    with pytest.raises(ValueError):
        subject._wire(op)


def test_final_wire_insert_requires_private_writer(assembly):
    a=assembly
    subject.install(a.conn)
    with pytest.raises(sqlite3.DatabaseError,match='immutable'):
        with kb.write_txn(a.conn):
            a.conn.execute(f'INSERT INTO {subject.TABLE} VALUES (?,?,?,?,?)',('x','y','z','{}','bad'))


def test_corrupt_anchor_or_missing_proposal_never_mints_replacement(assembly):
    a=assembly
    original=subject.freeze(a.conn,a.proof,delivery_uid='delivery-1')
    from hermes_cli import kanban_release_prepared as prepared
    a.conn.execute(f'DROP TRIGGER {prepared.TABLE}_delete')
    a.conn.execute(f'DELETE FROM {prepared.TABLE}')
    a.conn.commit()
    with pytest.raises(ValueError,match='custody differs'):
        subject.freeze(a.conn,a.proof,delivery_uid='delivery-1')
    assert a.conn.execute(f'SELECT operation_uid FROM {subject.TABLE}').fetchone()[0]==original['client_op_id']


def test_real_hub_private_application_http_bounds(canonical):
    """Actual Hub transport with labelled HTTP peer, NOT a DO composition."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    from hermes_cli.kanban_sync_client import Hub, Identity
    from hermes_cli.kanban_sync_hub_transport import BoundedResponseTooLarge
    canonical['task']['body']='x'*(cert.MAX_BYTES+1)
    response=[cert.encode(canonical).encode('ascii')]
    received=[]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.path,self.headers.get('Authorization')))
            self.send_response(200)
            self.send_header('Content-Length',str(len(response[0])))
            self.send_header('Content-Type','application/json')
            self.end_headers()
            try:
                self.wfile.write(response[0])
            except (ConnectionResetError,BrokenPipeError):
                pass
        def log_message(self,format,*args):
            pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    hub=Hub(Identity(url=f'http://127.0.0.1:{server.server_port}',key='fixture-key',gateway='home'))
    try:
        got=hub.owner_release_application('example','intent-1')
        assert subject.validate(got,canonical['operation'])==canonical
        assert received==[('/v2/boards/example/owner-release/intent-1/application','Bearer fixture-key')]
        response[0]=b' '*(subject.MAX_BYTES+1)
        with pytest.raises(BoundedResponseTooLarge):
            hub.owner_release_application('example','intent-1')
    finally:
        hub.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


