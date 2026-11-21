"""Kanban ingress must never create exact-model exclusivity."""
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_pin_reroute as pins
from hermes_cli import kanban_review_model as review
from tests.hermes_cli import test_kanban_model_tier as fixtures

board = fixtures.board

@pytest.mark.parametrize('kwargs', [
    {'model_override':'gpt-6.1-sol','provider_override':'openai-codex'},
    {'model_override':'claude-opus-5-5'},
    {'model_override':'class:deep','provider_override':'anthropic'},
    {'pin_mandatory':True},
    {'body':'require_model: true'},
    {'body':'Mandatory exact-head Opus review, no fallback.'},
    {'body':'Use only grok-4.6 / xai-oauth.'},
])
def test_create_refuses_restrictions_before_writing(board, kwargs):
    with kbc.connect() as conn:
        before=conn.execute('SELECT count(*) FROM tasks').fetchone()[0]
        with pytest.raises(ValueError,match='model-neutral'):
            kb.create_task(conn,title='ordinary work',assignee='default',**kwargs)
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0]==before

@pytest.mark.parametrize('entry', ['set-model','mandatory','edit','review'])
def test_mutations_refuse_atomically(board, entry):
    with kbc.connect() as conn:
        tid=kb.create_task(conn,title='ordinary work',assignee='default')
        if entry=='review': kb.claim_task(conn,tid)
        before=tuple(conn.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone())
        events=conn.execute('SELECT count(*) FROM task_events').fetchone()[0]
        with pytest.raises(ValueError,match='model-neutral'):
            if entry=='set-model': kb.set_model_override(conn,tid,'gpt-6.1-sol',provider='openai-codex')
            elif entry=='mandatory': pins.set_pin_mandatory(conn,tid,True)
            elif entry=='edit': kb.edit_task(conn,tid,body='require_model: true',priority=1)
            else: kb.request_review(conn,tid,summary='review',metadata={'review_model':'gpt-6.1-sol','review_provider':'openai-codex'})
        assert tuple(conn.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone())==before
        assert conn.execute('SELECT count(*) FROM task_events').fetchone()[0]==events

@pytest.mark.parametrize('model,provider', [(None,None),('class:deep','auto'),('class:standard',None)])
def test_class_and_clear_allowed(board,model,provider):
    with kbc.connect() as conn:
        tid=kb.create_task(conn,title='work',body='Independent qualified security review; preserve author exclusions.',assignee='default',model_override=model,provider_override=provider)
        assert kb.set_model_override(conn,tid,None)
        assert kb.get_task(conn,tid).model_override is None

@pytest.mark.parametrize('metadata',[{'review_provider':'anthropic'},{'review_model':'claude-opus-5-5'},{'review_model':'class:deep','review_provider':'anthropic'}])
def test_review_metadata_refuses_restrictions(metadata):
    with pytest.raises(ValueError,match='model-neutral'):review.validate_review_override(metadata)
