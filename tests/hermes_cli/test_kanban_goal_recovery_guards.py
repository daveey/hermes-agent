"""Safety and route contracts on an isolated board."""
from dataclasses import replace
import pytest
from tests.hermes_cli.test_kanban_goal_recovery import board as board  # noqa: PLC0414 -- pytest fixture re-export
from hermes_cli import kanban_db as kb, kanban_resume_exec as rx, kanban_resume_requester as rr
from hermes_cli.kanban_goal_recovery import park, GoalRejection, recovery_assignee


@pytest.mark.parametrize('defect', ['missing_run', 'stale_run', 'non_goal', 'human', 'forged', 'changed_goal'])
def test_receipt_refuses_invalid_ownership_or_subject(board, defect):
    conn, tid, task = board
    run = task.current_run_id
    if defect == 'missing_run':
        run = None
    elif defect == 'stale_run':
        run += 1
    elif defect in ('non_goal', 'human', 'changed_goal'):
        sql = {'non_goal': 'goal_mode=0', 'human': "human_request='{}'",
               'changed_goal': "body='Changed acceptance'"}[defect]
        with kb.write_txn(conn):
            conn.execute(f'UPDATE tasks SET {sql} WHERE id=?', (tid,))
    if defect == 'forged':
        assert not kb.block_task(conn, tid, kind='transient', reason='unavailable',
                                 expected_run_id=run, goal_rejection=GoalRejection('forged'))
    else:
        assert not park(conn, task, 'unavailable', expected_run_id=run)
    assert kb.get_task(conn, tid).status == 'running'
    assert not any(e.kind == 'goal_unachievable' for e in kb.list_events(conn, tid))


@pytest.mark.parametrize('defect', ['missing', 'forged', 'stale'])
def test_resume_denies_invalid_receipt(board, defect):
    conn, tid, task = board
    assert park(conn, task, 'unavailable', expected_run_id=task.current_run_id)
    with kb.write_txn(conn):
        if defect == 'missing':
            conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='goal_unachievable'", (tid,))
        elif defect == 'forged':
            conn.execute("UPDATE task_events SET payload='{}' WHERE task_id=? AND kind='goal_unachievable'", (tid,))
        else:
            conn.execute("UPDATE task_events SET run_id=NULL WHERE task_id=? AND kind='goal_unachievable'", (tid,))
    assert 'receipt' in rx.exhaustion_refusal(conn, tid)
    assert rr.ensure_recovery_cards(conn) == []


@pytest.mark.parametrize('surface', ['cli', 'tool'])
def test_unparsed_blocked_judge_never_creates_receipt(board, monkeypatch, surface):
    from hermes_cli import kanban, goals
    from agent import auxiliary_client
    from tools import kanban_tools as kt
    conn, tid, task = board
    monkeypatch.setenv('HERMES_KANBAN_TASK', tid)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(task.current_run_id))
    monkeypatch.setattr(auxiliary_client, 'get_text_auxiliary_client', lambda *a: (object(), 'judge'))
    monkeypatch.setattr(kt, '_goal_judge_available', lambda: True)
    def judge(*a, **kw):
        return 'blocked', 'unparsed', True, None, False
    monkeypatch.setattr(goals, 'judge_goal', judge)
    monkeypatch.setattr(kt, 'judge_goal', judge)
    if surface == 'cli':
        assert kanban._goal_gate_error(conn, tid, 'evidence', 'completion', 'block', 'continue')
    else:
        with pytest.raises(kt._Reject):
            kt._goal_gate('kanban_complete', task, tid, 'evidence', conn=conn)
    assert kb.get_task(conn, tid).status == 'running'
    assert not any(e.kind == 'goal_unachievable' for e in kb.list_events(conn, tid))


@pytest.mark.parametrize('fallback', ['default', 'orchestrator', 'none'])
def test_recovery_route_validates_existing_profiles(board, monkeypatch, tmp_path, fallback):
    from hermes_cli import config
    _conn, _tid, task = board
    task = replace(task, assignee='missing-profile')
    cfg = {'kanban': {'orchestrator_profile': 'supervisor'}}
    monkeypatch.setattr(config, 'load_config_readonly', lambda: cfg)
    if fallback == 'orchestrator':
        home = tmp_path / '.hermes' / 'profiles' / 'supervisor'
        home.mkdir(parents=True)
        (home / 'config.yaml').write_text('platform_toolsets:\n  cli: [terminal, file, kanban]\n')
    elif fallback == 'none':
        (tmp_path / '.hermes' / 'config.yaml').write_text('agent:\n  disabled_toolsets: [terminal, file, kanban]\n')
    assert recovery_assignee(task) == {'default': 'default', 'orchestrator': 'supervisor', 'none': None}[fallback]


@pytest.mark.parametrize('surface', ['cli', 'tool'])
@pytest.mark.parametrize('handoff', ['complete', 'request_review'])
@pytest.mark.parametrize('judgment', ['blocked', 'parse_failed', 'transport_failed', 'absent'])
def test_handoff_entry_receipt_contract(board, monkeypatch, surface, handoff, judgment):
    import argparse
    import json
    from hermes_cli import kanban, goals
    from tools import kanban_tools as kt
    from tools.registry import registry

    conn, tid, task = board
    monkeypatch.setenv('HERMES_KANBAN_TASK', tid)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(task.current_run_id))
    monkeypatch.setattr('agent.auxiliary_client.get_text_auxiliary_client',
                        lambda *a: (None, None) if judgment == 'absent' else (object(), 'judge'))
    monkeypatch.setattr(kt, '_goal_judge_available', lambda: judgment != 'absent')
    def judge(*a, **kw):
        return 'blocked', 'missing prerequisite', judgment == 'parse_failed', None, judgment == 'transport_failed'
    monkeypatch.setattr(goals, 'judge_goal', judge)
    monkeypatch.setattr(kt, 'judge_goal', judge)
    if surface == 'cli':
        args = argparse.Namespace(task_ids=[tid], task_id=tid, summary='evidence', result=None, metadata=None)
        rc = getattr(kanban, '_cmd_' + handoff)(args)
        assert (rc != 0) == (judgment in ('blocked', 'parse_failed'))
    else:
        result = json.loads(registry.get_entry('kanban_' + handoff).handler({'task_id': tid, 'summary': 'evidence'}))
        assert bool(result.get('error')) == (judgment in ('blocked', 'parse_failed')), result
    expected = {'blocked': 'triage', 'parse_failed': 'running'}.get(
        judgment, 'done' if handoff == 'complete' else 'review')
    assert kb.get_task(conn, tid).status == expected
    receipts = [e for e in kb.list_events(conn, tid) if e.kind == 'goal_unachievable']
    assert len(receipts) == (1 if judgment == 'blocked' else 0)
    assert kb.get_task(conn, tid).body == task.body


@pytest.mark.parametrize('surface', ['cli', 'gateway'])
@pytest.mark.parametrize('failure', ['parse', 'transport', 'changed_goal'])
def test_loop_failures_never_mint_receipts(board, monkeypatch, surface, failure):
    from types import SimpleNamespace
    from hermes_cli import goals
    conn, tid, task = board
    monkeypatch.setenv('HERMES_KANBAN_TASK', tid)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(task.current_run_id))
    def judge(*a, **kw):
        if failure == 'changed_goal':
            with kb.write_txn(conn):
                conn.execute('UPDATE tasks SET body=? WHERE id=?', ('New acceptance', tid))
        return 'blocked', 'missing prerequisite', failure == 'parse', None, failure == 'transport'
    monkeypatch.setattr(goals, 'judge_goal', judge)
    if surface == 'cli':
        from hermes_cli.cli_single_query import _run_kanban_goal_loop_q
        out = _run_kanban_goal_loop_q(SimpleNamespace(), 'evidence', run_turn=lambda _: '')
    else:
        from gateway.session_kanban import _run_task_turns
        agent = SimpleNamespace(session_id='test', run_conversation=lambda *a, **kw: {'final_response': 'evidence'})
        out = _run_task_turns(agent, {'text': 'work'}, [], dict(skills=[], context='', task_id=tid,
                              run_id=task.current_run_id, goal_mode=True, goal_text=task.body, goal_max_turns=3))
    assert out['failed']
    assert kb.get_task(conn, tid).status == 'running'
    assert not any(e.kind == 'goal_unachievable' for e in kb.list_events(conn, tid))


@pytest.mark.parametrize('change', ['human', 'acceptance', 'lifecycle'])
def test_goal_resume_rechecks_target_after_request(board, monkeypatch, change):
    import json
    from hermes_cli import kanban_supervisor_requests as ksr
    from tools import kanban_tools as kt
    conn, tid, task = board
    assert park(conn, task, 'missing prerequisite', expected_run_id=task.current_run_id)
    recovery, = rr.ensure_recovery_cards(conn)
    assert kb.claim_task(conn, recovery)
    monkeypatch.setenv('HERMES_KANBAN_TASK', recovery)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(kb.get_task(conn, recovery).current_run_id))
    out = json.loads(kt._handle_supervisor_request({'ops': [{'op': 'resume', 'task_id': tid}], 'reason': 'Repaired'}))
    assert out['ok'], out
    monkeypatch.delenv('HERMES_KANBAN_TASK')
    monkeypatch.delenv('HERMES_KANBAN_RUN_ID')
    with kb.write_txn(conn):
        if change == 'human':
            conn.execute("UPDATE tasks SET human_request='{}' WHERE id=?", (tid,))
        elif change == 'acceptance':
            conn.execute("UPDATE tasks SET body='Changed acceptance' WHERE id=?", (tid,))
        else:
            kb._append_event(conn, tid, 'reopened', {})
    req = ksr.get_request(conn, out['request_id'])
    assert rx.execute(conn, req, 'test-executor') == 'rejected'
    assert kb.get_task(conn, tid).status == 'triage'
    assert not rx._resume_events(conn, tid)


def test_no_eligible_recovery_profile_keeps_goal_parked(board, tmp_path):
    conn, tid, task = board
    (tmp_path / '.hermes' / 'config.yaml').write_text('agent:\n  disabled_toolsets: [terminal, file, kanban]\n')
    assert park(conn, task, 'missing prerequisite', expected_run_id=task.current_run_id)
    assert rr.ensure_recovery_cards(conn) == []
    assert kb.get_task(conn, tid).status == 'triage'
    from hermes_cli import kanban_triage_sweep
    assert kanban_triage_sweep.sweep_stale_triage(conn, now=10**10) == []


def test_goal_resume_preserves_dependency_gate(board):
    from hermes_cli import kanban_supervisor_requests as ksr
    conn, tid, task = board
    assert park(conn, task, 'missing prerequisite', expected_run_id=task.current_run_id)
    recovery, = rr.ensure_recovery_cards(conn)
    parent = kb.create_task(conn, title='Prerequisite owner', assignee='default')
    kb.link_tasks(conn, parent, tid)
    edges = list(conn.execute('SELECT parent_id, child_id FROM task_links'))
    assert kb.claim_task(conn, recovery)
    requester = kb.get_task(conn, recovery)
    assert requester is not None
    request = ksr.submit(conn, env_task=recovery, env_run=str(requester.current_run_id),
                         ops=[{'op': 'resume', 'task_id': tid}], reason='Repaired')
    assert rx.execute(conn, ksr.get_request(conn, request['request_id']), 'test-executor') == 'done'
    assert kb.get_task(conn, tid).status == 'todo'
    assert list(conn.execute('SELECT parent_id, child_id FROM task_links')) == edges
    assert kb.claim_task(conn, tid) is None

