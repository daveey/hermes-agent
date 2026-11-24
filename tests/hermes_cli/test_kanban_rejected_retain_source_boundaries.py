"""Direct correlation boundary cases that SQL selection would otherwise mask."""
import copy
import json

import pytest

from tests.hermes_cli import test_kanban_rejected_retain_source as helpers


@pytest.mark.parametrize('defect', [
    'card_board','card_task','principal_empty','target_session','run_bool','run_zero',
    'ended_inf','ended_bool','ended_before_start','message_session','message_id_type',
    'message_bool_time','message_inf_time','message_outside_session','call_role','call_empty_id',
    'claim_kind','claim_run','admitted_kind','admitted_run','admitted_time',
    'two_command_rows','command_not_dict',
])
def test_direct_source_correlations(tmp_path, defect):
    from hermes_cli import kanban_rejected_retain_source as source
    _, op, wire, admission, session, messages = helpers.fixture_db(tmp_path)
    payload=json.loads(admission['payload_json'])
    card=payload['kanban_card_v1']
    show=json.loads(messages[0]['content'])
    calls=json.loads(messages[1]['tool_calls'])
    terminal=json.loads(messages[3]['content'])
    changes={
        'card_board':(card,'board','other'), 'card_task':(card,'task_id','other'),
        'target_session':(admission,'target_session_id','other'),
        'ended_inf':(session,'ended_at',float('inf')), 'ended_bool':(session,'ended_at',True),
        'message_session':(messages[3],'session_id','other'),
        'message_id_type':(messages[3],'id',40.0),
        'message_bool_time':(messages[0],'timestamp',True),
        'message_inf_time':(messages[3],'timestamp',float('inf')),
        'message_outside_session':(messages[3],'timestamp',101),
        'call_role':(messages[1],'role','user'),
        'claim_kind':(show['events'][0],'kind','unrelated'),
        'claim_run':(show['events'][0],'run_id',43),
        'admitted_kind':(show['events'][1],'kind','unrelated'),
        'admitted_run':(show['events'][1],'run_id',43),
        'admitted_time':(show['events'][1],'created_at',11),
    }
    if defect in changes:
        target,key,value=changes[defect];target[key]=value
    if defect=='principal_empty':admission['principal_id']=session['user_id']=''
    if defect in ('run_bool','run_zero'):
        run=True if defect=='run_bool' else 0
        card['run_id']=run
        request='kanban:'+json.dumps(['default',wire['task'],run],separators=(',',':'))
        admission['request_id']=request
        show['task']['current_run_id']=run
        for event in show['events']:event['run_id']=run
        show['events'][1]['payload']['request_id']=request
    if defect=='ended_before_start':session['started_at']=101
    if defect=='message_bool_time':
        for event in show['events']:event['created_at']=1
    if defect=='call_empty_id':
        calls[0]['id']=calls[0]['call_id']='';messages[2]['tool_call_id']=''
    if defect=='two_command_rows':terminal['output']=json.dumps([op,op])
    if defect=='command_not_dict':terminal['output']=json.dumps([list(op)])
    admission['payload_json']=json.dumps(payload)
    messages[0]['content']=json.dumps(show)
    messages[1]['tool_calls']=json.dumps(calls)
    messages[3]['content']=json.dumps(terminal)
    try: result=source._source(admission,session,messages,'default',op,wire)
    except (ValueError,TypeError,KeyError,AttributeError):result=[]
    assert result==[]


@pytest.mark.parametrize('defect', ['op_missing','op_changed','value_missing','value_changed','value_type'])
def test_exact_command_comparison(tmp_path, defect):
    from hermes_cli import kanban_rejected_retain_source as source
    _, op, wire, *_=helpers.fixture_db(tmp_path)
    value=copy.deepcopy(op)
    if defect=='op_missing':op.pop('operation_kind')
    if defect=='op_changed':op['operation_kind']='other'
    if defect=='value_missing':value.pop('operation_kind')
    if defect=='value_changed':value['operation_kind']='other'
    if defect=='value_type':value=list(value)
    try: result=source._command(value,op,wire)
    except (ValueError,TypeError,KeyError,AttributeError):result=False
    assert not result


@pytest.mark.parametrize('bad', [None, '', 0])
def test_invalid_board_and_wire_versions_are_not_sources(tmp_path,bad):
    from hermes_cli import kanban_rejected_retain_source as source
    path,op,wire,*_=helpers.fixture_db(tmp_path)
    assert source.discover(path,bad,op,wire) is None


@pytest.mark.parametrize('field,value', [('op','other'),('payload',{'version':True}),('payload',{'version':2})])
def test_invalid_frozen_wire_is_not_a_source(tmp_path,field,value):
    from hermes_cli import kanban_rejected_retain_source as source
    path,op,wire,*_=helpers.fixture_db(tmp_path)
    wire[field]=value
    assert source.discover(path,'default',op,wire) is None
