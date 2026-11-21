"""Legacy routing restrictions must not constrain future admitted runs."""
import json
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_model_tier as tier
from hermes_cli import kanban_review_model as review
from tests.hermes_cli import test_kanban_model_tier as fixtures

board=fixtures.board


@pytest.mark.parametrize('extra', [
    ['--model', 'gpt-6.1-sol', '--provider', 'openai-codex'],
    ['--pin-mandatory'],
    ['--body', 'require_model: true'],
])
def test_cli_create_refuses_before_preflight(board, monkeypatch, capsys, extra):
    import argparse
    from hermes_cli import kanban as cli
    from hermes_cli import kanban_hub_admission_placement as placement
    monkeypatch.setattr(placement, 'cli_preflight', lambda *a, **k: pytest.fail('preflight reached'))
    parser=argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers())
    args=parser.parse_args(['kanban','create','work','--assignee','default',*extra])
    assert cli._cmd_create(args) == 2
    assert 'model-neutral' in capsys.readouterr().err


def test_legacy_pin_cannot_gate_worker_placement(board):
    from dataclasses import asdict
    from hermes_cli import kanban_mandatory_pin as mandatory
    with kbc.connect() as conn:
        tid=kb.create_task(conn,title='work',assignee='default')
        task=legacy(conn,tid)
        assert mandatory.pin_of(asdict(task)) is None
        assert mandatory.requirements(asdict(task)) == set()
        assert mandatory.worker_first_refusal(asdict(task)) is None


def test_direct_mandatory_mark_rejected(board):
    from hermes_cli.kanban_mandatory_pin import mark
    with kbc.connect() as conn:
        tid=kb.create_task(conn,title='work',assignee='default')
        legacy(conn,tid)
        with pytest.raises(ValueError, match='model-neutral'):
            mark(conn,tid)


def test_specify_refuses_before_promotion(board):
    with kbc.connect() as conn:
        tid=kb.create_task(conn,title='work',assignee='default',triage=True)
        before=tuple(conn.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone())
        with pytest.raises(ValueError, match='model-neutral'):
            kb.specify_triage_task(conn,tid,title='new',body='require_model: true')
        assert tuple(conn.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()) == before


def legacy(conn, tid):
    # Isolated fixture models a pre-policy row; never a production mutation API.
    conn.execute('UPDATE tasks SET model_override=?,provider_override=?,model_pin_mandatory=1,body=? WHERE id=?',
                 ('grok-4.6','xai-oauth','require_model: true',tid))
    conn.commit()
    return kb.get_task(conn,tid)


def test_legacy_pin_ignored_in_tier_and_owner_without_receipt(board):
    with kbc.connect() as conn:
        tid=kb.create_task(conn,title='work',assignee='default')
        task=legacy(conn,tid)
        assert tier.resolve_model_tier(conn,task,lane='ready',cfg={})==('class:standard','auto','default')
        assert tier.owner_model_pin(conn,task)==('class:standard','auto')
        assert kb.get_task(conn,tid).model_override=='grok-4.6'


def test_legacy_mandatory_does_not_disable_alternatives(board):
    from hermes_cli.kanban_pin_reroute import is_mandatory_pin
    from hermes_cli.kanban_provider_walls import requires_model
    with kbc.connect() as conn:
        tid=kb.create_task(conn,title='work',assignee='default')
        task=legacy(conn,tid)
        assert not is_mandatory_pin(task)
        assert not requires_model(task)


def test_current_run_receipt_not_replaced_by_specification(board):
    with kbc.connect() as conn:
        tid=kb.create_task(conn,title='work',assignee='default')
        legacy(conn,tid)
        kb.claim_task(conn,tid)
        task=kb.get_task(conn,tid)
        kb._append_event(conn,tid,'model_tier',{'model':'gpt-6-luna','provider':'openai-codex','lane':'ready'},run_id=task.current_run_id)
        assert tier.owner_model_pin(conn,task)==('gpt-6-luna','openai-codex')


def test_dynamic_review_ignores_legacy_override_and_pin(board,monkeypatch):
    from hermes_cli import kanban_provider_walls as pw
    from hermes_cli import kanban_llm_pacing_dispatch as pacing
    monkeypatch.setattr(review,'implementer_models',lambda c,t:{'author-model'})
    monkeypatch.setattr(review,'independent_routes',lambda *a,**k:iter([('held-model','xai-oauth'),('available-model','openai-codex')]))
    monkeypatch.setattr(review,'registry_refusal',lambda *a:None)
    monkeypatch.setattr(pw,'active_walls',lambda **k:{'xai-oauth':9999999999})
    monkeypatch.setattr(pw,'live_quota_wall',lambda *a:None)
    monkeypatch.setattr(pacing,'held_pools',lambda *a:{})
    with kbc.connect() as conn:
        tid=kb.create_task(conn,title='Independent source review',assignee='default')
        task=legacy(conn,tid)
        monkeypatch.setattr(review,'_handoff',lambda *a:(None,{'review_model':'held-model','review_provider':'xai-oauth'}))
        assert review.resolve_review_model(conn,task,{})==('available-model','openai-codex','review_unpinned')
        monkeypatch.setattr(review,'independent_routes',lambda *a,**k:iter([]))
        with pytest.raises(review.ReviewModelsExhausted): review.resolve_review_model(conn,task,{})
