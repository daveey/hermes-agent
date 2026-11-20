"""Goal judge parks use bounded supervisor recovery, never human asks."""
from pathlib import Path
import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_resume_exec as rx, kanban_resume_requester as rr
from hermes_cli import kanban_triage_sweep as ts


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    (home / 'config.yaml').write_text('model:\n  provider: openai-codex\n  default: gpt-5\n')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(home))
    for key in ('HERMES_KANBAN_DB', 'HERMES_KANBAN_TASK', 'HERMES_KANBAN_RUN_ID', 'HERMES_SESSION_ID'):
        monkeypatch.delenv(key, raising=False)
    kb._INITIALIZED_PATHS.clear()
    assert kb.kanban_db_path().is_relative_to(tmp_path)
    kb.init_db()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='Deliver exact goal', body='Acceptance must stay fixed.',
                             assignee='default', goal_mode=True)
        assert kb.claim_task(conn, tid)
        yield conn, tid, kb.get_task(conn, tid)


@pytest.mark.parametrize('entry', ['kanban_complete', 'kanban_request_review'])
def test_judge_park_creates_fresh_bounded_requester_without_changing_goal(board, monkeypatch, entry):
    from tools import kanban_tools as kt
    conn, tid, task = board
    monkeypatch.setenv('HERMES_KANBAN_TASK', tid)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(task.current_run_id))
    monkeypatch.setattr(kt, '_goal_judge_available', lambda: True)
    monkeypatch.setattr(kt, 'judge_goal', lambda **kw: ('blocked', 'Prerequisite unavailable', False, None, False))
    with pytest.raises(kt._Reject):
        kt._goal_gate(entry, task, tid, 'evidence')
    parked = kb.get_task(conn, tid)
    assert parked.status == 'triage'
    assert parked.human_request is None and parked.current_run_id is None
    assert parked.body == task.body
    events = kb.list_events(conn, tid)
    assert any(e.kind == 'goal_unachievable' and e.run_id == task.current_run_id for e in events)
    assert next(e for e in reversed(events) if e.kind == 'blocked').payload['rekind_reason'] == 'goal_unachievable'
    monkeypatch.delenv('HERMES_KANBAN_TASK')
    monkeypatch.delenv('HERMES_KANBAN_RUN_ID')
    cards = rr.ensure_recovery_cards(conn)
    assert len(cards) == 1
    assert rr.ensure_recovery_cards(conn) == []
    assert rx.refusal(conn, cards[0], tid) is None
    assert ts.sweep_stale_triage(conn, now=10**10) == []
    assert kb.get_task(conn, tid).body == task.body


@pytest.mark.parametrize('handoff', ['completion', 'review handoff'])
def test_cli_gate_parks(board, monkeypatch, handoff):
    from hermes_cli import kanban, goals
    from agent import auxiliary_client
    conn, tid, task = board
    monkeypatch.setenv('HERMES_KANBAN_TASK', tid)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(task.current_run_id))
    monkeypatch.setattr(auxiliary_client, 'get_text_auxiliary_client', lambda *a: (object(), 'judge'))
    monkeypatch.setattr(goals, 'judge_goal', lambda **kw: ('blocked', 'missing prerequisite', False, None, False))
    assert kanban._goal_gate_error(conn, tid, 'evidence', handoff, 'block', 'continue')
    assert kb.get_task(conn, tid).status == 'triage'


@pytest.mark.parametrize('surface', ['cli', 'gateway'])
def test_goal_loop_blocked_callback_parks(board, monkeypatch, surface):
    from types import SimpleNamespace
    from hermes_cli import goals
    conn, tid, task = board
    monkeypatch.setenv('HERMES_KANBAN_TASK', tid)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(task.current_run_id))
    monkeypatch.setenv('HERMES_KANBAN_GOAL_MODE', '1')
    monkeypatch.setattr(goals, 'judge_goal', lambda *a, **kw: ('blocked', 'missing prerequisite', False, None, False))
    if surface == 'cli':
        from hermes_cli.cli_single_query import _run_kanban_goal_loop_q
        _run_kanban_goal_loop_q(SimpleNamespace(), 'evidence', run_turn=lambda _: '')
    else:
        from gateway.session_kanban import _run_task_turns
        agent = SimpleNamespace(session_id='test', run_conversation=lambda *a, **kw: {'final_response': 'evidence'})
        _run_task_turns(agent, {'text': 'work'}, [], dict(skills=[], context='', task_id=tid,
                        run_id=task.current_run_id, goal_mode=True, goal_text=task.body, goal_max_turns=3))
    assert kb.get_task(conn, tid).status == 'triage'
    assert rx.exhaustion_refusal(conn, tid) is None


def test_goal_recovery_pins_and_no_triage_cycle_after_two_resumes(board, monkeypatch):
    from hermes_cli.kanban_goal_recovery import park
    from hermes_cli import kanban_supervisor_requests as ksr
    conn, tid, task = board
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET model_override=?, provider_override=? WHERE id=?', ('gpt-5', 'openai-codex', tid))
    for attempt in range(3):
        task = kb.get_task(conn, tid)
        assert park(conn, task, 'prerequisite unavailable', expected_run_id=task.current_run_id)
        cards = rr.ensure_recovery_cards(conn)
        if attempt == 2:
            assert cards == []
            assert ts.sweep_stale_triage(conn, now=10**10) == []
            break
        rec = kb.get_task(conn, cards[0])
        assert (rec.model_override, rec.provider_override) == ('gpt-5', 'openai-codex')
        assert 'prerequisite' in rec.body and 'acceptance' in rec.body
        assert rx.refusal(conn, rec.id, tid) is None
        assert kb.claim_task(conn, rec.id)
        monkeypatch.setenv('HERMES_KANBAN_TASK', rec.id)
        monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(kb.get_task(conn, rec.id).current_run_id))
        import json
        from tools import kanban_tools as kt
        from hermes_cli import kanban_db_dispatch as dispatch
        out = json.loads(kt._handle_supervisor_request({'ops': [{'op': 'resume', 'task_id': tid}],
                                                       'reason': 'Prerequisite repaired and verified'}))
        assert out['ok'], out
        monkeypatch.delenv('HERMES_KANBAN_TASK')
        monkeypatch.delenv('HERMES_KANBAN_RUN_ID')
        assert dispatch.dispatch_once(conn, max_spawn=0).supervisor_requests == [(out['request_id'], 'done')]
        assert ksr.get_request(conn, out['request_id'])['result'][0]['outcome'] == 'applied'
        assert kb.claim_task(conn, tid)
    assert kb.get_task(conn, tid).body == task.body


TIMED = 'nightly data refresh lands later; SCHEDULED_UNTIL=2099-01-01T00:00:00Z'


def _goal_loop(surface, conn, tid, task, max_turns):
    from types import SimpleNamespace
    if surface == 'cli':
        from hermes_cli.cli_single_query import _run_kanban_goal_loop_q
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET goal_max_turns=? WHERE id=?', (max_turns, tid))
        _run_kanban_goal_loop_q(SimpleNamespace(), 'evidence', run_turn=lambda _: '')
    else:
        from gateway.session_kanban import _run_task_turns
        agent = SimpleNamespace(session_id='test', run_conversation=lambda *a, **kw: {'final_response': 'evidence'})
        _run_task_turns(agent, {'text': 'work'}, [], dict(skills=[], context='', task_id=tid,
                        run_id=task.current_run_id, goal_mode=True, goal_text=task.body, goal_max_turns=max_turns))


@pytest.mark.parametrize('verdict', ['blocked', 'continue'])
@pytest.mark.parametrize('surface', ['cli', 'gateway'])
def test_goal_judge_timed_park_names_its_wake_reason(board, monkeypatch, surface, verdict):
    """A judge reason carrying SCHEDULED_UNTIL: the turn-budget block (``block_fn``, verdict continue) takes
    block_task's timed branch on both the CLI and the gateway loop and declares the clock with the judge's reason
    (before, the >2 h guard refused it and the card stayed running). An unachievable park (``park``, verdict
    blocked) is a goal_unachievable triage park, never timed, and stays one. [daveey]"""
    from hermes_cli import goals
    conn, tid, task = board
    monkeypatch.setenv('HERMES_KANBAN_TASK', tid)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(task.current_run_id))
    monkeypatch.setenv('HERMES_KANBAN_GOAL_MODE', '1')
    monkeypatch.setattr(goals, 'judge_goal', lambda *a, **kw: (verdict, TIMED, False, None, False))
    _goal_loop(surface, conn, tid, task, 1)
    if verdict == 'blocked':
        assert kb.get_task(conn, tid).status == 'triage'
        return
    assert kb.get_task(conn, tid).status == 'scheduled'
    park = next(e for e in reversed(kb.list_events(conn, tid)) if e.kind == 'scheduled').payload
    assert park['awaiting']['kind'] == 'clock' and park['awaiting']['why'].startswith('goal judge: '), park
    assert 'nightly data refresh' in park['awaiting']['why']


def test_goal_awaiting_only_for_timed_reasons():
    from hermes_cli.kanban_goal_recovery import goal_awaiting
    assert goal_awaiting('missing prerequisite') is None
    assert goal_awaiting(TIMED) == {'kind': 'clock', 'why': 'goal judge: ' + TIMED}
    from hermes_cli.kanban_goal_recovery import goal_block_kwargs
    assert goal_block_kwargs('missing prerequisite') == {}
    assert goal_block_kwargs(TIMED) == {'awaiting': goal_awaiting(TIMED), 'clock_only': True}
