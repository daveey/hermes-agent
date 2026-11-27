"""Foreign final-certificate reader through page certification and pickup.

Assembly adapter is synthetic; actual Board HTTP attestation has separate coverage.
These tests do not claim composed original CLI→DO→HOME→executor/live proof.
"""
from __future__ import annotations

import copy
import sqlite3
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, kanban_db_sync as sync
from hermes_cli import kanban_release_application as app, kanban_release_certificate as cert
from hermes_cli import kanban_release_executor as subject, kanban_release_public as public
from hermes_cli import kanban_sync_quarantine as quarantine
from tests.hermes_cli import test_owner_release_materialize as helpers

board=helpers.board
proof=helpers.proof
assembly=helpers.assembly


def projection(row):
    return {k:row[k] for k in app.EVENT_FIELDS}


def reply(a):
    value=helpers.adapter(helpers.wire(a))
    p=value['consumption']
    delivery=app._event(a.conn,'delivery-1')
    att=dict(version=1,board=p['intent']['board'],reader_gateway='executor',intent=p['intent'],renewal=projection(p['renewal']),
             delivery=delivery,events=[projection(e) for e in value['events']])
    result=value['events'][-1]
    return dict(board=att['board'],table='task_events',uid=result['uid'],state='live',row=result,
                tombstone=None,floor=0,feed_floor=0,head=500,owner_release_applied_v1=att)


def changes(value):
    att=value['owner_release_applied_v1']
    return [sync.RemoteChange(seq=100+i,version=1,table='task_events',uid=e['uid'],op='upsert',fields=e,
                              hlc='1700000000000-0000-server',origin_gateway=e['origin_gateway'])
            for i,e in enumerate([att['delivery'],att['renewal'],*att['events']])]


class Hub:
    def __init__(self,conn,value):
        self.conn,self.value=conn,value
        self.calls=[]
    def row_evidence(self,board,table,uid,**kwargs):
        assert not self.conn.in_transaction
        self.calls.append((board,table,uid,kwargs))
        return copy.deepcopy(self.value)


def foreign(a):
    with kb.write_txn(a.conn):
        a.conn.execute("UPDATE kanban_sync_state SET gateway_id='executor'")


def test_page_capture_split_arrival_and_foreign_actual_execution_reader(assembly):
    from hermes_cli import kanban_spend_decision as decision
    a=assembly;value=reply(a);foreign(a);rows=changes(value);hub=Hub(a.conn,value)
    assert decision.execution_restricted(a.conn,a.tid)
    safe=quarantine.certified_page(a.conn,hub,value['board'],rows)
    assert len(hub.calls)==1 and hub.calls[0][3]['max_response_bytes']==public.MAX_RESPONSE_BYTES
    assert subject.verified_application(a.conn,'intent-1') is None
    sync.apply_remote_changes(a.conn,safe[:-1],new_cursor=400,observers=False)
    assert subject.verified_application(a.conn,'intent-1') is None
    sync.apply_remote_changes(a.conn,safe[-1:],new_cursor=500,observers=False)
    got=subject.verified_application(a.conn,'intent-1')
    assert got is not None and 'consumption' not in got
    assert app.verified_application(a.conn,'intent-1') is None
    assert app.consumer_application(a.conn,'intent-1')==got
    assert not decision.execution_restricted(a.conn,a.tid)
    assert kb.get_task(a.conn,a.tid).status=='triage'  # no task-image import
    assert not a.conn.execute("SELECT 1 FROM sqlite_master WHERE name='owner_release_applied_receipts'").fetchone() or app.verified_application(a.conn,'intent-1') is None


@pytest.mark.parametrize('half',['renewal','delivery','result','release','promotion','budget'])
@pytest.mark.parametrize('damage',['payload','origin','run','missing'])
def test_every_local_public_half_rechecked(assembly,half,damage):
    a=assembly;value=reply(a);foreign(a);rows=changes(value)
    quarantine.certified_page(a.conn,Hub(a.conn,value),value['board'],rows)
    sync.apply_remote_changes(a.conn,rows,new_cursor=500,observers=False)
    assert subject.verified_application(a.conn,'intent-1') is not None
    att=value['owner_release_applied_v1']
    kinds={'result':'owner_release_applied','release':'stop_released','promotion':'promoted_manual','budget':'spend_budget_set'}
    target=att[half] if half in ('renewal','delivery') else next(e for e in att['events'] if e['kind']==kinds[half])
    with kb.write_txn(a.conn):
        if damage=='missing':
            a.conn.execute('DELETE FROM task_events WHERE uid=?',(target['uid'],))
        else:
            column,new={'payload':('payload','{}'),'origin':('origin_gateway','forged'),'run':('run_id',999)}[damage]
            if damage=='run':
                new=a.conn.execute("INSERT INTO task_runs(task_id,profile,status,started_at,ended_at) VALUES (?,'default','scheduled',1,2)", (a.tid,)).lastrowid
            a.conn.execute(f'UPDATE task_events SET {column}=? WHERE uid=?',(new,target['uid']))
    assert subject.verified_application(a.conn,'intent-1') is None


def test_home_must_not_fallback_and_successor_stays_held(assembly):
    from hermes_cli import kanban_spend_decision as decision
    a=assembly;value=reply(a);rows=changes(value);foreign(a)
    quarantine.certified_page(a.conn,Hub(a.conn,value),value['board'],rows)
    sync.apply_remote_changes(a.conn,rows,new_cursor=500,observers=False)
    with kb.write_txn(a.conn):
        a.conn.execute("UPDATE kanban_sync_state SET gateway_id='home'")
    assert subject.verified_application(a.conn,'intent-1') is None
    assert app.consumer_application(a.conn,'intent-1') is None
    with kb.write_txn(a.conn):
        a.conn.execute("UPDATE kanban_sync_state SET gateway_id='renamed-home'")
    assert app.consumer_application(a.conn,'intent-1') is None
    foreign(a)
    assert subject.verified_application(a.conn,'intent-1') is not None
    with kb.write_txn(a.conn):
        kb._append_event(a.conn,a.tid,'decision_hold_restricted',{'cause':'safety_refusal','episode':'successor'})
    assert decision.execution_restricted(a.conn,a.tid)


@pytest.mark.parametrize('mode',['absent','malformed','feed','oversize'])
def test_no_proof_or_conflict_does_not_authorize_or_livelock_page(assembly,mode):
    a=assembly;value=reply(a);foreign(a);rows=changes(value)
    if mode=='absent':
        del value['owner_release_applied_v1']
    if mode=='malformed':
        value['owner_release_applied_v1']['intent']['actor']['session']='forged'
    if mode=='feed':
        rows[-1].fields['payload']='{}'
    hub=Hub(a.conn,value)
    if mode=='oversize':
        def oversized(*args,**kwargs):
            raise subject.BoundedResponseTooLarge('bounded')
        hub.row_evidence=oversized
    safe=quarantine.certified_page(a.conn,hub,value['board'],rows)
    assert subject.verified_application(a.conn,'intent-1') is None
    if mode=='absent':
        assert safe==rows and not quarantine.held(a.conn,a.tid)
    else:
        assert not safe and quarantine.held(a.conn,a.tid)
        kind=quarantine.entries(a.conn)[0]['kind']
        assert kind in quarantine.NO_GENERIC_RELEASE


def test_immutable_receipt_sql_replace_and_readonly_reopen(assembly):
    a=assembly;value=reply(a);foreign(a);rows=changes(value)
    quarantine.certified_page(a.conn,Hub(a.conn,value),value['board'],rows)
    sync.apply_remote_changes(a.conn,rows,new_cursor=500,observers=False)
    for statement in (f'DELETE FROM {subject.TABLE}',f"UPDATE {subject.TABLE} SET digest='changed'",
                      f'INSERT OR REPLACE INTO {subject.TABLE} SELECT * FROM {subject.TABLE}'):
        with pytest.raises(sqlite3.Error):
            a.conn.execute(statement)
    path=a.conn.execute('PRAGMA database_list').fetchone()[2]
    ro=sqlite3.connect(f'file:{path}?mode=ro',uri=True)
    try:
        class Rows:
            def execute(self,*args):
                return ro.execute(*args)
        assert subject.verified_application(Rows(),'intent-1') is not None
        assert app.consumer_application(Rows(),'intent-1') is not None
    finally:
        ro.close()


def test_real_pickup_adopts_before_apply_and_remaps_history(assembly,tmp_path,monkeypatch):
    from hermes_cli import kanban_worker_node as worker, kanban_db_connect as kbc
    from hermes_cli import kanban_spend_decision as decision
    a=assembly;value=reply(a);foreign(a);rows=changes(value)
    quarantine.certified_page(a.conn,Hub(a.conn,value),value['board'],rows)
    sync.apply_remote_changes(a.conn,rows,new_cursor=500,observers=False)
    monkeypatch.setenv('HERMES_KANBAN_DB',str(tmp_path/'executor.db'))
    kb.init_db()
    with kbc.connect_closing() as dest:
        sync.apply_sync_schema_steps(dest);dest.commit()
        with kb.write_txn(dest):
            dest.execute("INSERT INTO kanban_sync_state(board_id,gateway_id,hlc_last,mode) VALUES ('example','executor','','primary')")
        sync.set_identity(dest,'example','executor');sync.set_mode(dest,'primary')
        assert worker.import_cards(dest,a.conn,[a.tid])==[a.tid]
        got=subject.verified_application(dest,'intent-1')
        assert got is not None
        assert not decision.execution_restricted(dest,a.tid)
        assert app.verified_application(dest,'intent-1') is None
        assert not dest.execute("SELECT 1 FROM sqlite_master WHERE name='owner_release_home_queue'").fetchone()


@pytest.mark.parametrize('table',['kanban_sync_state',subject.TABLE])
def test_optional_malformed_schema_fails_closed(table):
    conn=sqlite3.connect(':memory:')
    try:
        conn.execute(f'CREATE TABLE {table} (unexpected TEXT)')
        assert subject.verified_application(conn,'intent-1') is None
    finally:
        conn.close()


def test_authenticated_reader_feed_and_cache_hash_fences(assembly):
    a=assembly;value=reply(a);foreign(a);rows=changes(value)
    att,parsed=public.evidence(value,board=value['board'],task=a.tid,uid=rows[-1].uid)
    with kb.write_txn(a.conn):a.conn.execute("UPDATE kanban_sync_state SET gateway_id='wrong-reader'")
    with pytest.raises(subject.ReplicaConflict,match='reader'):
        subject._store(a.conn,att,parsed)
    foreign(a)
    altered=copy.deepcopy(rows[-1]);altered.fields['created_at']+=1
    with pytest.raises(subject.ReplicaConflict,match='feed result'):
        subject.certify_page(a.conn,Hub(a.conn,value),value['board'],[altered])
    subject.certify_page(a.conn,Hub(a.conn,value),value['board'],rows)
    sync.apply_remote_changes(a.conn,rows,new_cursor=500,observers=False)
    with kb.write_txn(a.conn):
        a.conn.execute(f'DROP TRIGGER {subject.TABLE}_update')
        a.conn.execute(f"UPDATE {subject.TABLE} SET digest='corrupt'")
    assert subject.verified_application(a.conn,'intent-1') is None


@pytest.mark.parametrize('reader', ['executor', 'renamed-home'])
def test_renewal_home_alias_cannot_use_public_cache_without_private_proof(assembly, reader):
    # Synthetic authenticated attestation, as in the neighboring reader tests.
    # Unlike changing local identity alone, this keeps reader_gateway matching;
    # only equality with the canonical renewal HOME separates the two cases.
    a = assembly
    value = reply(a)
    att = value['owner_release_applied_v1']
    att['reader_gateway'] = reader
    att['renewal']['origin_gateway'] = 'renamed-home'
    for event in att['events']:
        event['origin_gateway'] = 'renamed-home'
    value['row'] = att['events'][-1]
    assert att['intent']['home'] == 'home' != reader
    with kb.write_txn(a.conn):
        a.conn.execute('UPDATE kanban_sync_state SET gateway_id=?', (reader,))
        # The fixture already has this renewal locally with pending transport;
        # model its canonical alias stamp too, rather than expecting feed import
        # to overwrite locally pending history.
        a.conn.execute('UPDATE task_events SET origin_gateway=? WHERE uid=?',
                       ('renamed-home', att['renewal']['uid']))
    rows = changes(value)
    # Both variants pass real public validation, authenticated cache capture,
    # and remote apply: no mismatched-reader or missing-public-half veto masks
    # the renewal-origin HOME fence in verified_application.
    safe = quarantine.certified_page(a.conn, Hub(a.conn, value), value['board'], rows)
    assert safe == rows
    sync.apply_remote_changes(a.conn, safe, new_cursor=500, observers=False)
    assert subject._cached(a.conn, 'intent-1') is not None
    assert app.verified_application(a.conn, 'intent-1') is None
    before = list(a.conn.iterdump())
    got = subject.verified_application(a.conn, 'intent-1')
    if reader == 'renamed-home':
        assert got is None, 'canonical renewal HOME must retain private application proof'
        assert app.consumer_application(a.conn, 'intent-1') is None
    else:
        assert got is not None
        assert app.consumer_application(a.conn, 'intent-1') == got
    assert list(a.conn.iterdump()) == before


def test_unreadable_pickup_receipt_holds_one_card(assembly,tmp_path,monkeypatch):
    from hermes_cli import kanban_worker_node as worker, kanban_db_connect as kbc
    a=assembly;value=reply(a);foreign(a);rows=changes(value)
    sync.apply_remote_changes(a.conn,rows,new_cursor=500,observers=False)
    a.conn.execute(f'CREATE TABLE {subject.TABLE}(unexpected TEXT)');a.conn.commit()
    monkeypatch.setenv('HERMES_KANBAN_DB',str(tmp_path/'broken-pickup.db'));kb.init_db()
    with kbc.connect_closing() as dest:
        assert worker.import_cards(dest,a.conn,[a.tid])==[]
        assert kb.get_task(dest,a.tid) is None

