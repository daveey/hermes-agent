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


@pytest.mark.skipif(not os.environ.get('B2_M2_LOCAL_BOARD_URL'), reason='requires isolated local wrangler Board')
@pytest.mark.parametrize('foreign', [False, True])
def test_home_stop_lost_reply_restart_exact_receipt(tmp_path, monkeypatch, foreign):
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
        assert actor_hub._call('POST',f'/v2/boards/{board}/leases',{'action':'grant','task':actor,'run_uid':run,'ttl_s':3600})['status']=='applied'
        t=hub.row_evidence(board,'tasks',target)['row']
        args={'source_event_uid':source,'source_run_uid':oldrun,'expected_status':t['status'],'expected_current_run_uid':None,'expected_updated_at':t['updated_at'],'expected_episode':None,
              'requested_owner':actor,'next_step':'Resolve original administrative authority only','recovery_trigger':'Explicit supported disposition, never retry original scope','escalate_at':int(time.time())+86400,'evidence':[source]}
        payload={'version':1,'request_uid':request_uid,'action':'protect_stopped','board':board,'home':hub.ident.gateway,'target_task_id':target,'requested_at':int(time.time()),
                 'caller':{'gateway':actor_hub.ident.gateway,'task_id':actor,'run_uid':run},'args':args}
        apply(actor_hub,[up('task_events',request_event,{'task_id':actor,'run_uid':run,'kind':'operator_call_requested','created_at':int(time.time()),'payload':json.dumps(payload)})])
        from hermes_cli import kanban_protect_transport as transport
        with kb.write_txn(conn):
            frozen=transport.reserve(conn,board,payload,request_event)
        wire=json.loads(frozen['request_json'])
        # Commit canonical effect and deliberately lose its ACK before any local result.
        assert hub.ops(board,[wire])['results'][0]['status']=='applied'
        assert not conn.execute("SELECT 1 FROM task_events WHERE kind='operator_call_result'").fetchone()
        conn.close()
        conn=kbc.connect(home/'kanban.db')
        assert transport.command(conn,request_uid)['request_json']==frozen['request_json']
        result=transport.confirm(conn,hub,board,request_uid)
        assert result['delivery_state']=='confirmed' and result['execution_authority'] is False
        assert result['dependency_satisfied'] is False and result['terminal_for_run'] is False
        assert transport.confirm(conn,hub,board,request_uid)==result
        assert transport.command(conn,request_uid)['request_json']==frozen['request_json']
        assert hub.row_evidence(board,'tasks',target)['row']['status']=='triage'
        assert hub.row_evidence(board,'task_runs',oldrun)['row']['outcome']=='crashed'
        assert hub.row_evidence(board,'task_runs',run)['row']['ended_at'] is None
        assert conn.execute('SELECT 1 FROM task_events WHERE uid=?',(wire['event_uid'],)).fetchone()
    finally:
        conn.close()
