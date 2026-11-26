"""Real remote-apply writer and HOME entrypoint, synthetic Board adapter labelled.

Actual HTTP/DO certificate bytes are independently checked in test_final. These
SQLite adapter tests are NOT the composed live DO/HOME/native/executor proof.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, kanban_release_application as app
from hermes_cli import kanban_release_final as final, kanban_release_certificate as cert
from hermes_cli import kanban_release_materialize as subject
from tests.hermes_cli import test_owner_release_assemble as helpers
from tests.hermes_cli import test_owner_release_final as final_helpers

canonical=final_helpers.canonical
board=helpers.board
proof=helpers.proof
assembly=helpers.assembly


def adapter(wire):
    """Canonical-stamp adapter; not a substitute for actual DO transport."""
    proposal=wire['proposal']
    def stamped(row,event,i):
        fields=cert._EVENT if event else cert._COMMENT
        r={k:row.get(k) for k in fields}
        r.update(seq=100+i,version=1,updated_at=1700000000000,hlc='1700000000000-0000-server',
                 origin_gateway=proposal['consumption']['renewal']['origin_gateway'],deleted_at=None)
        if event:
            r.update(ord=50+i,run_uid=None)
        return r
    return dict(version=1,operation=wire,consumption=proposal['consumption'],
                task=dict(uid=wire['task'],status='ready'),
                comments=[stamped(c,False,i) for i,c in enumerate(proposal['comments'])],
                events=[stamped(e,True,i+1) for i,e in enumerate(proposal['events'])],
                allowance=dict(window_uid='existing-window',episode_uid=proposal['consumption']['intent']['episode'],
                               runs=2,used=0,remaining=2))


def wire(a):
    return final.freeze(a.conn,a.proof,delivery_uid='delivery-1')


def snapshot(a):
    return {t:[tuple(r) for r in a.conn.execute(f'SELECT * FROM {t} ORDER BY rowid')]
            for t in ('tasks','task_events','task_comments','kanban_sync_outbox','kanban_sync_state',
                      'owner_release_applied_receipts')}


def test_import_records_applied_atomically_without_historical_task_image(assembly):
    from hermes_cli import kanban_spend_decision as decision
    a=assembly
    value=adapter(wire(a))
    task=tuple(a.conn.execute('SELECT * FROM tasks WHERE id=?',(a.tid,)).fetchone())
    outbox=[tuple(r) for r in a.conn.execute('SELECT * FROM kanban_sync_outbox')]
    got=subject.materialize(a.conn,value)
    assert got['result']['outcome']=='released'
    assert app.verified_application(a.conn,'intent-1') is not None
    assert tuple(a.conn.execute('SELECT * FROM tasks WHERE id=?',(a.tid,)).fetchone())==task
    assert kb.get_task(a.conn,a.tid).status=='triage'  # ordinary current feed owns status
    assert [tuple(r) for r in a.conn.execute('SELECT * FROM kanban_sync_outbox')]==outbox
    assert not decision.execution_restricted(a.conn,a.tid)
    before=snapshot(a)
    assert subject.materialize(a.conn,value)==got
    assert snapshot(a)==before


@pytest.mark.parametrize('fault',['receipt','certificate','event'])
def test_late_real_sql_failure_rolls_back_public_and_private_rows(assembly,fault):
    import sqlite3
    a=assembly
    value=adapter(wire(a))
    subject.install(a.conn)
    table={'receipt':app.TABLE,'certificate':subject.TABLE,'event':'task_events'}[fault]
    a.conn.execute(f"CREATE TRIGGER fail_final AFTER INSERT ON {table} BEGIN SELECT RAISE(ABORT,'final rollback'); END")
    before=snapshot(a)
    with pytest.raises(sqlite3.DatabaseError,match='final rollback'):
        subject.materialize(a.conn,value)
    assert snapshot(a)==before
    assert subject.verified(a.conn,'intent-1') is None


def test_successor_safety_and_pending_task_state_are_not_overwritten(assembly):
    from hermes_cli import kanban_spend_decision as decision
    a=assembly
    value=adapter(wire(a))
    with kb.write_txn(a.conn):
        kb._append_event(a.conn,a.tid,'decision_hold_restricted',{'cause':'safety_refusal','episode':'successor'})
        a.conn.execute("UPDATE tasks SET status='blocked',human_request='independent' WHERE id=?",(a.tid,))
    task=tuple(a.conn.execute('SELECT * FROM tasks WHERE id=?',(a.tid,)).fetchone())
    subject.materialize(a.conn,value)
    assert tuple(a.conn.execute('SELECT * FROM tasks WHERE id=?',(a.tid,)).fetchone())==task
    assert decision.execution_restricted(a.conn,a.tid)


def test_comment_ids_remap_while_portable_frozen_payload_is_unchanged(assembly):
    a=assembly
    value=adapter(wire(a))
    c=value['comments'][0]
    frozen_id=value['operation']['proposal']['comments'][0]['id']
    with kb.write_txn(a.conn):
        kb.add_comment(a.conn,a.tid,'another','Unrelated successor comment',system=True)
    subject.materialize(a.conn,value)
    local=a.conn.execute('SELECT id FROM task_comments WHERE uid=?',(c['uid'],)).fetchone()[0]
    assert local!=frozen_id
    e=next(e for e in value['events'] if e['kind']=='spend_budget_set')
    assert app._event(a.conn,e['uid'])['payload']==e['payload']
    assert cert.parse(e['payload'])['decision_comment']['comment_uid']==c['uid']


def test_unanswered_effect_transport_is_not_erased_or_overwritten(assembly):
    a=assembly
    value=adapter(wire(a))
    row=value['events'][0]
    with kb.write_txn(a.conn):
        a.conn.execute('INSERT INTO task_events(uid,task_id,kind,payload,created_at) VALUES (?,?,?,?,?)',
                       (row['uid'],a.tid,'local-pending','{}',0))
    before=snapshot(a)
    with pytest.raises(ValueError,match='unanswered'):
        subject.materialize(a.conn,value)
    assert snapshot(a)==before


class Hub:
    def __init__(self,a,loss=False):
        self.a=a
        self.ident=SimpleNamespace(gateway='home')
        self.value=None
        self.posts=[]
        self.loss=loss
    def owner_release_application(self,board,uid):
        from hermes_cli.kanban_sync_client import HubError
        assert not self.a.conn.in_transaction
        if self.value is None:
            raise HubError(404,'not found')
        return copy.deepcopy(self.value)
    def ops(self,board,ops):
        assert not self.a.conn.in_transaction
        self.posts.append(copy.deepcopy(ops))
        self.value=adapter(ops[0])
        if self.loss:
            raise OSError('reply lost after commit')
        return {'results':[{'status':'applied'}]}


@pytest.mark.parametrize('loss',[False,True])
def test_actual_home_process_reconciles_lost_reply_and_completed_result_first(assembly,loss,monkeypatch):
    from hermes_cli import kanban_release_home as home, kanban_operator_call as operator
    monkeypatch.setattr(operator, '_board_slug', lambda _conn: 'example')
    a=assembly
    hub=Hub(a,loss)
    row=a.conn.execute('SELECT * FROM owner_release_home_queue').fetchone()
    got=home._process(a.conn,hub,row,'home','example')
    assert got['result']['outcome']=='released'
    assert len(hub.posts)==1
    with kb.write_txn(a.conn):
        a.conn.execute("UPDATE task_chat_links SET released=1 WHERE uid='link-1'")
        kb._append_event(a.conn,a.tid,'spend_budget_triaged',{'cause':'scope_stopped'})
    assert home._process(a.conn,hub,row,'home','example')==got
    assert len(hub.posts)==1


def test_feed_cursor_race_refuses_without_rewinding(assembly,monkeypatch):
    from hermes_cli import kanban_db_sync as sync
    a=assembly
    value=adapter(wire(a))
    actual=sync.apply_remote_changes
    def raced(conn,changes,**kwargs):
        with kb.write_txn(conn):
            conn.execute('UPDATE kanban_sync_state SET cursor=999')
        return actual(conn,changes,**kwargs)
    monkeypatch.setattr(sync,'apply_remote_changes',raced)
    with pytest.raises(ValueError,match='cursor advanced'):
        subject.materialize(a.conn,value)
    assert sync.sync_state(a.conn)['cursor']==999
    assert app.verified_application(a.conn,'intent-1') is None


def test_postcommit_error_reopens_exact_receipt_without_reapplication(assembly,monkeypatch):
    import sqlite3
    from hermes_cli import kanban_db_sync as sync
    a=assembly
    value=adapter(wire(a))
    actual=sync.apply_remote_changes
    def uncertain(conn,changes,**kwargs):
        actual(conn,changes,**kwargs)
        raise OSError('after real COMMIT')
    monkeypatch.setattr(sync,'apply_remote_changes',uncertain)
    with pytest.raises(OSError,match='after real COMMIT'):
        subject.materialize(a.conn,value)
    path=a.conn.execute('PRAGMA database_list').fetchone()[2]
    reopened=sqlite3.connect(f'file:{path}?mode=ro',uri=True)
    try:
        assert app.verified_application(reopened,'intent-1') is not None
        assert subject.verified(reopened,'intent-1')==value
    finally:
        reopened.close()
    before=snapshot(a)
    assert subject.materialize(a.conn,value)['result']['outcome']=='released'
    assert snapshot(a)==before


def test_completed_queue_never_rotates_or_recontacts_hub(assembly,monkeypatch):
    from hermes_cli import kanban_release_home as home
    a=assembly
    subject.materialize(a.conn,adapter(wire(a)))
    before=[tuple(r) for r in a.conn.execute('SELECT * FROM owner_release_home_queue')]
    def forbidden():
        pytest.fail('completed application must not construct another Hub')
    monkeypatch.setattr(home,'_hub',forbidden)
    home.tick(a.conn,a.conn,'home','example')
    assert [tuple(r) for r in a.conn.execute('SELECT * FROM owner_release_home_queue')]==before


@pytest.mark.parametrize('missing',['comment','ancillary_event'])
def test_skipped_materialization_rolls_back_instead_of_certifying(assembly,monkeypatch,missing):
    from hermes_cli import kanban_db_sync as sync
    a=assembly
    value=adapter(wire(a))
    wanted=value['comments'][0]['uid'] if missing=='comment' else value['events'][0]['uid']
    actual=sync._apply_one
    def skip(conn,change,own,result):
        if change.uid!=wanted:
            return actual(conn,change,own,result)
        result.skipped+=1
    monkeypatch.setattr(sync,'_apply_one',skip)
    before=snapshot(a)
    with pytest.raises(ValueError,match='failed materialization|effect missing'):
        subject.materialize(a.conn,value)
    assert snapshot(a)==before


def test_final_certificate_uses_real_hub_get_route_and_complete_response_cap(canonical,monkeypatch):
    from hermes_cli.kanban_sync_hub_routes import HubRoutesMixin
    class Client(HubRoutesMixin):
        def _call(self,*args,**kwargs):
            assert args==('GET','/v2/boards/board%20name/owner-release/intent%3Aone/application')
            assert kwargs=={'max_response_bytes':final.MAX_BYTES}
            return canonical
    assert Client().owner_release_application('board name','intent:one')==canonical


