"""Exercise actual producer entrypoints, including combined PATCH atomicity."""
import json
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.plugins import test_kanban_model_override as api_fixtures

kanban_home=api_fixtures.kanban_home
client=api_fixtures.client
conn=api_fixtures.conn

@pytest.mark.parametrize('payload',[
    {'model_override':'gpt-6.1-sol','provider_override':'openai-codex'},
    {'body':'require_model: true'},
    {'body':'Mandatory exact-head Opus review, no fallback.'},
])
def test_rest_create_rejects(client,conn,payload):
    before=conn.execute('SELECT count(*) FROM tasks').fetchone()[0]
    response=client.post('/api/plugins/kanban/tasks',json={'title':'work','assignee':'default',**payload})
    assert response.status_code==400,response.text
    assert 'model-neutral' in response.text
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0]==before

@pytest.mark.parametrize('payload',[
    {'model_override':'gpt-6.1-sol','provider_override':'openai-codex'},
    {'body':'require_model: true'},
    {'provider_override':'anthropic'},
])
def test_rest_patch_refuses_before_other_fields(client,conn,payload):
    tid=kb.create_task(conn,title='work',assignee='default')
    before=tuple(conn.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone())
    response=client.patch('/api/plugins/kanban/tasks/'+tid,json={'priority':1,'status':'triage',**payload})
    assert response.status_code==400,response.text
    assert 'model-neutral' in response.text
    assert tuple(conn.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone())==before

@pytest.mark.parametrize('payload', [
    {'model_override': 'gpt-6.1-sol', 'provider_override': 'openai-codex'},
    {'body': 'require_model: true'},
    {'status': 'review', 'metadata': {'review_model': 'gpt-6.1-sol', 'review_provider': 'openai-codex'}},
])
def test_invalid_mixed_patch_preserves_all_fields_and_history(client, conn, payload):
    tid = kb.create_task(conn, title='original', body='original body', assignee='default')
    before = tuple(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
    events = [tuple(row) for row in conn.execute('SELECT * FROM task_events WHERE task_id=?', (tid,))]
    response = client.patch('/api/plugins/kanban/tasks/' + tid, json={
        'assignee': '', 'status': 'triage', 'priority': 1, 'body': 'replacement', **payload,
    })
    assert response.status_code == 400, response.text
    assert 'model-neutral' in response.text
    assert tuple(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()) == before
    assert [tuple(row) for row in conn.execute('SELECT * FROM task_events WHERE task_id=?', (tid,))] == events


@pytest.mark.parametrize('selector', [{}, {'model_override': 'class:deep', 'provider_override': 'auto'}])
def test_valid_neutral_mixed_patch_succeeds(client, conn, selector):
    tid = kb.create_task(conn, title='original', assignee='default')
    response = client.patch('/api/plugins/kanban/tasks/' + tid, json={
        'assignee': '', 'status': 'triage', 'priority': 1, 'body': 'Use tools and an eligible reviewer.', **selector,
    })
    assert response.status_code == 200, response.text
    stored = kb.get_task(conn, tid)
    assert stored is not None
    assert stored.assignee is None and stored.status == 'triage' and stored.priority == 1
    assert stored.body == 'Use tools and an eligible reviewer.'
    assert stored.model_override == selector.get('model_override')
    assert stored.provider_override == selector.get('provider_override')


@pytest.mark.parametrize('payload',[
    {'model':'gpt-6.1-sol','provider':'openai-codex'},
    {'pin_mandatory':True},
    {'body':'require_model: true'},
])
def test_agent_create_rejects_before_creation(kanban_home,conn,payload):
    from tools import kanban_tools as tools
    before=conn.execute('SELECT count(*) FROM tasks').fetchone()[0]
    result=json.loads(tools._handle_create({'title':'work','assignee':'default',**payload}))
    assert not result.get('ok'),result
    assert 'model-neutral' in str(result),result
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0]==before
