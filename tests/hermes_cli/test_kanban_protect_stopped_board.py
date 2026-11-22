"""Real frozen M2 Core SQLite -> Board HTTP/DO -> replica/restart acceptance."""
import hashlib
import json
import os
import time
import uuid
from urllib.request import Request, urlopen

import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import kanban_sync_support as support


@pytest.fixture(params=[False, True], ids=['same-home', 'foreign'])
def composed(tmp_path, monkeypatch, request):
    if not os.environ.get('B2_M2_LOCAL_BOARD_URL'):
        pytest.skip('requires isolated local wrangler Board')
    foreign = request.param
    url=os.environ['B2_M2_LOCAL_BOARD_URL']
    assert url.startswith('http://127.0.0.1:')
    home=support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    board='m2-'+uuid.uuid4().hex[:12]

    def enrolled():
        gateway='m2-'+uuid.uuid4().hex[:12]
        key='hmk_'+uuid.uuid4().hex
        headers={'Content-Type':'application/json','Authorization':'Bearer test-admin-key'}
        with urlopen(Request(url+'/v1/admin/gateways',json.dumps({'name':gateway}).encode(),headers),timeout=10) as r:
            code=json.load(r)['code']
        with urlopen(Request(url+'/v1/enroll',json.dumps({'gateway':gateway,'code':code,'key_sha256':hashlib.sha256(key.encode()).hexdigest()}).encode(),{'Content-Type':'application/json'}),timeout=10) as r:
            json.load(r)
        return client.Hub(client.Identity(url,key,gateway))

    hub=enrolled()
    actor_hub=enrolled() if foreign else hub
    conn=kbc.connect(home/'kanban.db')
    try:
        client.ensure_identity(conn,board,hub.ident.gateway)
        sync.set_mode(conn,'primary')
        target=kb.create_task(conn,title='isolated historical source fixture',assignee='default')
        assert client.push(conn,hub,board,hub.ident.gateway,primary=True).rejected==0
        client.pull(conn,hub,board,observers=False)
        actor='t_'+uuid.uuid4().hex[:12]
        oldrun,run,source,request_event,request_uid=['m2-'+uuid.uuid4().hex for _ in range(5)]
        def up(table,uid,fields):
            return {'client_op_id':'up-'+uuid.uuid4().hex,'op':'upsert','table':table,'uid':uid,'fields':fields,'hlc':f'{time.time_ns()//1_000_000:013d}-0000-fixture'}
        def apply(h,ops):
            answer=h.ops(board,ops)
            assert all(r['status'] in ('applied','merged') for r in answer['results']),answer
        apply(actor_hub,[up('tasks',actor,{'title':'existing administrative parent','status':'ready','assignee':'default','created_at':1000})])
        apply(hub,[up('task_links',f'{actor}>{target}',{'parent_id':actor,'child_id':target}),
                   up('task_runs',oldrun,{'task_id':target,'profile':'default','status':'crashed','outcome':'crashed','started_at':1000,'ended_at':1001}),
                   up('task_events',source,{'task_id':target,'run_uid':oldrun,'kind':'failure_finalized','created_at':1001,'payload':json.dumps({'run_uid':oldrun,'reason_code':'provider_refusal','tripped':True,'final_status':'triage','trigger_outcome':'crashed'})})])
        production = request.node.name.startswith('test_production_')
        if not production:
            assert actor_hub._call('POST',f'/v2/boards/{board}/leases',{'action':'grant','task':actor,'run_uid':run,'ttl_s':3600})['status']=='applied'
        t=hub.row_evidence(board,'tasks',target)['row']
        args={'source_event_uid':source,'source_run_uid':oldrun,'expected_status':t['status'],'expected_current_run_uid':None,'expected_updated_at':t['updated_at'],'expected_episode':None,
              'requested_owner':actor,'next_step':'Resolve original administrative authority only','recovery_trigger':'Explicit supported disposition, never retry original scope','escalate_at':int(time.time())+86400,'evidence':[source]}
        if production:
            yield dict(conn=conn, hub=hub, actor_hub=actor_hub, actor=actor, board=board,
                       args=args, source=source, home=home, target=target, oldrun=oldrun)
            return
        payload={'version':1,'request_uid':request_uid,'action':'protect_stopped','board':board,'home':hub.ident.gateway,'target_task_id':target,'requested_at':int(time.time()),
                 'caller':{'gateway':actor_hub.ident.gateway,'task_id':actor,'run_uid':run},'args':args}
        apply(actor_hub,[up('task_events',request_event,{'task_id':actor,'run_uid':run,'kind':'operator_call_requested','created_at':int(time.time()),'payload':json.dumps(payload)})])
        from hermes_cli import kanban_protect_transport as transport
        with kb.write_txn(conn):
            frozen=transport.reserve(conn,board,payload,request_event)
        wire=json.loads(frozen['request_json'])
        yield dict(conn=conn, hub=hub, board=board, request_uid=request_uid, wire=wire,
                   frozen=frozen, payload=payload, request_event=request_event, actor_hub=actor_hub,
                   home=home, target=target, oldrun=oldrun, run=run)
    finally:
        conn.close()


def test_home_stop_lost_reply_restart_exact_receipt(composed):
    from hermes_cli import kanban_protect_transport as transport
    c = composed
    conn, hub, board, uid = c['conn'], c['hub'], c['board'], c['request_uid']
    assert hub.ops(board,[c['wire']])['results'][0]['status']=='applied'
    assert not conn.execute("SELECT 1 FROM task_events WHERE kind='operator_call_result'").fetchone()
    conn.close()
    with kbc.connect(c['home']/'kanban.db') as reopened:
        assert transport.command(reopened,uid)['request_json']==c['frozen']['request_json']
        result=transport.confirm(reopened,hub,board,uid)
        assert result['delivery_state']=='confirmed' and result['execution_authority'] is False
        assert result['dependency_satisfied'] is False and result['terminal_for_run'] is False
        assert transport.confirm(reopened,hub,board,uid)==result
        assert transport.command(reopened,uid)['request_json']==c['frozen']['request_json']
        assert hub.row_evidence(board,'tasks',c['target'])['row']['status']=='triage'
        assert hub.row_evidence(board,'task_runs',c['oldrun'])['row']['outcome']=='crashed'
        assert hub.row_evidence(board,'task_runs',c['run'])['row']['ended_at'] is None
        assert reopened.execute('SELECT 1 FROM task_events WHERE uid=?',(c['wire']['event_uid'],)).fetchone()


class EvidenceFault:
    """Inject one fault after real authenticated HTTP; never stub lifecycle predicates."""
    def __init__(self, hub, wire, fault):
        self.hub, self.wire, self.fault, self.ident = hub, wire, fault, hub.ident

    def __getattr__(self, key):
        return getattr(self.hub, key)

    def row_evidence(self, board, table, uid):
        proof = self.hub.row_evidence(board, table, uid)
        row = proof.get('row')
        if not row:
            return proof
        if uid == self.wire['source_event_uid']:
            if self.fault == 'source_uid':
                row['uid'] = 'different-source'
            elif self.fault == 'source_reason':
                p = json.loads(row['payload'])
                p['reason_code'] = 'ordinary_error'
                row['payload'] = json.dumps(p)
        elif uid == self.wire['source_run_uid'] and self.fault == 'run_uid':
            row['uid'] = 'different-run'
        elif uid == self.wire['task'] and self.fault == 'target_home':
            row['created_on'] = 'different-home'
        elif uid == self.wire['request_event_uid'] and self.fault == 'request_args':
            p = json.loads(row['payload'])
            p['args']['next_step'] = 'changed after freezing'
            row['payload'] = json.dumps(p)
        return proof


@pytest.mark.parametrize('fault', ['source_uid', 'source_reason', 'run_uid', 'target_home', 'request_args'])
def test_corrupt_evidence_never_confirms_or_changes_custody(composed, fault):
    from hermes_cli import kanban_protect_transport as transport
    c = composed
    conn, hub, board, uid = c['conn'], c['hub'], c['board'], c['request_uid']
    with pytest.raises(ValueError):
        transport.confirm(conn, EvidenceFault(hub, c['wire'], fault), board, uid)
    frozen = transport.command(conn, uid)
    assert frozen['delivery_state'] == 'pending'
    assert frozen['request_json'] == c['frozen']['request_json']
    assert not conn.execute("SELECT 1 FROM task_events WHERE kind='operator_call_result'").fetchone()
    # Removing only the injected fault must reconcile the SAME already-applied op.
    assert transport.confirm(conn, hub, board, uid)['delivery_state'] == 'confirmed'


def test_real_applied_response_lost_inside_confirm_then_reopen(composed):
    from hermes_cli import kanban_protect_transport as transport
    c = composed
    class LostReply(EvidenceFault):
        def ops(self, board, ops):
            assert self.hub.ops(board, ops)['results'][0]['status'] == 'applied'
            raise TimeoutError('actual applied response lost at transport boundary')
    with pytest.raises(TimeoutError):
        transport.confirm(c['conn'], LostReply(c['hub'], c['wire'], None), c['board'], c['request_uid'])
    assert transport.command(c['conn'], c['request_uid'])['delivery_state'] == 'pending'
    c['conn'].close()
    with kbc.connect(c['home']/'kanban.db') as reopened:
        assert transport.command(reopened, c['request_uid'])['request_json'] == c['frozen']['request_json']
        assert transport.confirm(reopened, c['hub'], c['board'], c['request_uid'])['delivery_state'] == 'confirmed'


def test_actual_incoming_courier_lost_reply_restart_one_terminal_result(composed, monkeypatch):
    from contextlib import contextmanager
    from hermes_cli import kanban_operator_call as courier, kanban_protect_transport as transport
    c = composed
    conn, hub, slug, uid = c['conn'], c['hub'], c['board'], c['request_uid']
    rep = kbc.connect(c['home']/'replica.db')
    client.ensure_identity(rep, slug, hub.ident.gateway)
    client.pull(rep, hub, slug, observers=False)
    @contextmanager
    def replica(_):
        yield rep
    monkeypatch.setattr(courier, '_replica', replica)
    monkeypatch.setattr(courier, '_board_slug', lambda _: slug)
    monkeypatch.setattr(courier, '_evidence', lambda b, e: hub.row_evidence(b, 'task_events', e))
    monkeypatch.setattr(client, 'load_identity', lambda: hub.ident)
    original = client.Hub.ops
    def lost(self, b, ops):
        answer = original(self, b, ops)
        if any(op.get('op') == 'protect_stopped' for op in ops):
            assert answer['results'][0]['status'] == 'applied'
            raise TimeoutError('applied at DO; courier loses reply')
        return answer
    monkeypatch.setattr(client.Hub, 'ops', lost)
    try:
        first = courier.apply_incoming(conn)
        assert (uid, 'accepted_pending_canonical') in first
        assert not conn.execute("SELECT 1 FROM task_events WHERE kind='operator_call_result'").fetchone()
        row = conn.execute("SELECT outcome,finished_at FROM operator_calls WHERE side='execute' AND request_uid=?", (uid,)).fetchone()
        assert tuple(row) == ('accepted_pending_canonical', None)
        assert transport.command(conn, uid)['request_json'] == c['frozen']['request_json']
        conn.close()
        with kbc.connect(c['home']/'kanban.db') as reopened:
            monkeypatch.setattr(client.Hub, 'ops', original)
            courier._MARK.clear()
            from hermes_cli import kanban_protect_operator as _operator
            _operator._BACKOFF.clear()  # a restart forgets in-memory backoff
            assert (uid, 'applied') in courier.apply_incoming(reopened)
            assert courier.apply_incoming(reopened) == []
            results = reopened.execute("SELECT payload FROM task_events WHERE kind='operator_call_result'").fetchall()
            assert len(results) == 1
            result = json.loads(results[0][0])
            assert result['outcome'] == 'applied' and result['protection']['execution_authority'] is False
            assert transport.command(reopened, uid)['request_json'] == c['frozen']['request_json']
    finally:
        rep.close()

