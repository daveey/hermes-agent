"""[daveey] A run requeued by an owner restart (``owner_restart_lost``) resumes the lost run's conversation.

Real board, real dispatcher sweep (the restart loss and the crash are booked by ``detect_crashed_workers``
from the worker's exit code), real ``SessionDB`` and the real ``run_task`` admission; only the owner's local
session creation is a stand-in that creates the fresh session row the way the owner does.
"""
import asyncio
from contextlib import closing
import json
import time
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_db_dispatch_exit_code import KANBAN_OWNER_RESTART_EXIT_CODE

PRIOR = 'local-prior-session'
NEW = 'local-new-session'


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('HERMES_KANBAN_CRASH_GRACE_SECONDS', '0')
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.delenv('HERMES_DELEGATED_CHILD', raising=False)
    monkeypatch.setattr('hermes_cli.profiles.resolve_profile_env',
                        lambda name: str(tmp_path) if name == 'default' else str(tmp_path / name))
    monkeypatch.setattr('hermes_cli.kanban_capabilities._profile_capabilities',
                        lambda name, *_required: {'terminal', 'file', 'git', 'host_repo', 'kanban'})
    monkeypatch.setattr(kb, '_pid_alive', lambda pid: False)
    monkeypatch.setattr(kbd, '_worker_final_output', lambda *a, **kw: '')
    config = {'model': {'default': 'loop-model', 'provider': 'custom'}, 'platform_toolsets': {'cli': ['terminal']}}
    monkeypatch.setattr('gateway.run._load_gateway_config', lambda: config)
    # The real resolver is exercised in its own test; here the window is fixed so no lookup runs.
    monkeypatch.setattr('gateway.session_kanban_resume.compaction_threshold_tokens', lambda policy: 100_000)
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / 'state.db')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    kb.create_board('owned')
    yield SimpleNamespace(tmp=tmp_path, db=db, workspace=workspace, monkeypatch=monkeypatch)
    db.close()


def _connect():
    from hermes_cli.kanban_db_connect import connect
    return closing(connect(board='owned'))


def _prior_session(db, *, profile='default', messages=None):
    db.create_session(session_id=PRIOR, source='kanban', model='loop-model', profile_name=profile)
    for msg in messages if messages is not None else _work_transcript():
        db.append_message(PRIOR, **msg)


def _work_transcript():
    calls = [{'id': 'call_edit1', 'type': 'function', 'function': {'name': 'patch', 'arguments': '{"path": "a.py"}'}}]
    return [
        {'role': 'user', 'content': 'work kanban task (old context)'},
        {'role': 'assistant', 'content': 'Editing a.py first.', 'tool_calls': calls},
        {'role': 'tool', 'content': '{"ok": true, "diff": "+x = 1"}', 'tool_call_id': 'call_edit1', 'tool_name': 'patch'},
        {'role': 'assistant', 'content': 'PROGRESS-MARKER-42: a.py patched, tests next.'},
    ]


def _lose_run(task_id, *, code, state_db, session_id=PRIOR, pid=81001, monkeypatch):
    """Claim a run, admit it to ``session_id`` in ``state_db`` like the owner, then let the real sweep book
    its exit ``code`` (76 = the owner restarted under it)."""
    with _connect() as conn:
        kb.recompute_ready(conn)
        host = kb._claimer_id().split(':', 1)[0]
        task = kb.claim_task(conn, task_id, claimer=f'{host}:w{pid}')
        assert task is not None
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET worker_pid=?, started_at=? WHERE id=?', (pid, int(time.time()) - 600, task_id))
            conn.execute('UPDATE task_runs SET worker_pid=? WHERE id=?', (pid, task.current_run_id))
            from hermes_cli.kanban_worker_recovery import _SCHEMA
            conn.execute(_SCHEMA)  # the transcript-tail registry, so the fallback context is observable
            conn.execute('INSERT OR REPLACE INTO worker_recovery_sessions VALUES (?,?,?,?,?,?)',
                         (task.current_run_id, task_id, 'default', str(state_db), session_id, time.time()))
    if code == KANBAN_OWNER_RESTART_EXIT_CODE:
        # The owner's own booking when its Stop cut the turn off (gateway/session_kanban_restart.py).
        from gateway.session_kanban_restart import record_restart_interruption
        record_restart_interruption(SimpleNamespace(kanban_json=json.dumps(
            {'db': str(kb.kanban_db_path(board='owned')), 'task_id': task_id, 'run_id': task.current_run_id,
             'claim_lock': task.claim_lock})))
    else:
        with _connect() as conn, kb.write_txn(conn):
            kb._append_event(conn, task_id, 'worker_result', {'pid': pid, 'claim_lock': task.claim_lock,
                             'exit_code': code, 'last_output': 'boom'}, run_id=task.current_run_id)
    with _connect() as conn:
        monkeypatch.setattr(kbd, '_recent_worker_exits', {pid: ((code & 0xFF) << 8, time.time())})
        kbd.detect_crashed_workers(conn)
        # The owner's admission marker for the lost run, added after the sweep: with it in place the sweep
        # would consult the owner's session_admissions ledger, which this fixture does not stand up.
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, 'owner_admitted', {'db': str(state_db), 'session_id': session_id,
                             'request_id': 'kanban:old', 'pid': 1, 'birth': 1.0}, run_id=task.current_run_id)
        row = conn.execute('SELECT outcome FROM task_runs WHERE id=?', (task.current_run_id,)).fetchone()
    return task.current_run_id, row[0]


def _admit_next(env, task_id):
    """Claim the requeued card's next run and admit it through the real ``run_task``."""
    with _connect() as conn:
        kb.recompute_ready(conn)
        task = kb.claim_task(conn, task_id)
    assert task is not None
    created = {}

    def create_local_session(authority, actor, params, *, trusted_policy=None, trusted_secrets=None):
        env.db.create_session(session_id=NEW, source='kanban', model='loop-model', profile_name='default')
        created['policy'] = trusted_policy
        return SimpleNamespace(session_id=NEW, profile_id=str(env.tmp))
    env.monkeypatch.setattr('gateway.session_local.create_local_session', create_local_session)
    calls = []

    async def submit(actor, submission, **kwargs):
        calls.append(('submit', submission.payload))
        return SimpleNamespace(state='queued')

    async def resume(ref, params):
        calls.append(('resume', ref.session_id))
    authority = SimpleNamespace(profile_id=str(env.tmp), db=env.db, submit=submit)
    from gateway.session_contract import Principal
    actor = Principal('owner', str(env.tmp), frozenset({'session:create'}), 'transport')
    connection = SimpleNamespace(authority=authority, actor=actor, native_owner=True, resume=resume)
    params = dict(board='owned', task_id=task_id, run_id=task.current_run_id, claim_lock=task.claim_lock)
    from gateway import session_kanban
    env.monkeypatch.setattr(session_kanban, 'asdict', lambda r: {'state': r.state})
    asyncio.run(session_kanban.run_task(connection, params))
    assert [c[0] for c in calls] == ['resume', 'submit']
    context = json.loads(created['policy'].kanban_json)
    with _connect() as conn:
        events = {kind: json.loads(payload or 'null') for kind, payload in conn.execute(
            'SELECT kind, payload FROM task_events WHERE task_id=? AND run_id=? ORDER BY id', (task_id, task.current_run_id))}
    return SimpleNamespace(run_id=task.current_run_id, context=context, events=events,
                           history=env.db.get_messages_as_conversation(NEW))


def _task(env):
    with _connect() as conn:
        return kb.create_task(conn, title='Long card', body='Do the long thing', assignee='default',
                              workspace_kind='dir', workspace_path=str(env.workspace))


def test_requeue_after_owner_restart_resumes_the_prior_session(env):
    from gateway.session_kanban_resume import worker_prompt_context
    _prior_session(env.db)
    tid = _task(env)
    prior_run, outcome = _lose_run(tid, code=KANBAN_OWNER_RESTART_EXIT_CODE, state_db=env.db.db_path, monkeypatch=env.monkeypatch)
    assert outcome == 'reclaimed'
    run = _admit_next(env, tid)
    # The whole prior conversation is now this run's own session history, in order.
    assert [(m['role'], m.get('content')) for m in run.history] == [(m['role'], m['content']) for m in _work_transcript()]
    assert run.history[2]['tool_call_id'] == 'call_edit1'
    # Ownership is unchanged: a new run, admitted to its own new session.
    assert run.run_id != prior_run
    assert run.events['owner_admitted']['session_id'] == NEW
    assert run.events['owner_admitted']['resumed_session_id'] == PRIOR
    resume = run.events['restart_resume']
    assert resume['resumed_session_id'] == PRIOR and resume['prior_run_id'] == prior_run
    assert resume['copied_messages'] == 4 and 'fallback' not in resume
    # The worker's prompt: the resume note + current card state, never the duplicate transcript tail.
    prompt = worker_prompt_context(run.context, run.history)
    assert 'Resumed after gateway restart' in prompt and f'run {prior_run}' in prompt
    assert 'Interrupted worker continuation' not in prompt and 'Do the long thing' in prompt
    # The prior session is read, never written.
    assert len(env.db.get_messages_as_conversation(PRIOR)) == 4


def test_a_mid_tool_call_turn_gets_a_synthetic_interrupted_result(env):
    from gateway.session_kanban_resume import INTERRUPTED_TOOL_RESULT
    calls = [{'id': 'call_push9', 'type': 'function', 'function': {'name': 'terminal', 'arguments': '{"command": "git push"}'}}]
    _prior_session(env.db, messages=[{'role': 'user', 'content': 'work kanban task'},
                                     {'role': 'assistant', 'content': 'Pushing.', 'tool_calls': calls}])
    tid = _task(env)
    _lose_run(tid, code=KANBAN_OWNER_RESTART_EXIT_CODE, state_db=env.db.db_path, monkeypatch=env.monkeypatch)
    run = _admit_next(env, tid)
    assert [m['role'] for m in run.history] == ['user', 'assistant', 'tool']
    last = run.history[-1]
    assert last['tool_call_id'] == 'call_push9' and last['content'] == INTERRUPTED_TOOL_RESULT
    assert run.events['restart_resume']['interrupted_calls'] == 1
    assert run.events['owner_admitted']['resumed_session_id'] == PRIOR


@pytest.mark.parametrize('case', ['missing', 'empty', 'foreign_home', 'foreign_profile', 'unreadable', 'over_threshold'])
def test_each_fallback_keeps_the_tail_only_fresh_session(env, case):
    from gateway.session_kanban_resume import worker_prompt_context
    state_db = env.db.db_path
    if case == 'missing':
        pass  # owner_admitted names a session that is not in state.db
    elif case == 'empty':
        _prior_session(env.db, messages=[])
    elif case == 'foreign_home':
        _prior_session(env.db)
        state_db = env.tmp / 'other-home' / 'state.db'
    elif case == 'foreign_profile':
        _prior_session(env.db, profile='other')
    elif case == 'unreadable':
        _prior_session(env.db)
        real = env.db.get_messages_as_conversation

        def broken(sid, *a, **kw):
            if sid == PRIOR:
                raise ValueError('corrupt transcript')
            return real(sid, *a, **kw)
        env.monkeypatch.setattr(env.db, 'get_messages_as_conversation', broken)
    elif case == 'over_threshold':
        _prior_session(env.db)
        env.monkeypatch.setattr('gateway.session_kanban_resume.compaction_threshold_tokens', lambda policy: 10)
    tid = _task(env)
    _lose_run(tid, code=KANBAN_OWNER_RESTART_EXIT_CODE, state_db=state_db, monkeypatch=env.monkeypatch)
    run = _admit_next(env, tid)
    expected = {'missing': 'prior_session_missing', 'empty': 'prior_session_empty', 'foreign_home': 'foreign_home',
                'foreign_profile': 'foreign_profile', 'unreadable': 'prior_session_unreadable',
                'over_threshold': 'over_compaction_threshold'}[case]
    assert run.events['restart_resume']['fallback'] == expected
    assert 'resumed_session_id' not in run.events['restart_resume']
    assert 'resumed_session_id' not in run.events['owner_admitted']
    assert run.history == []  # a fresh session
    assert 'resume_context' not in run.context
    prompt = worker_prompt_context(run.context, run.history)
    if case in ('missing', 'empty', 'foreign_home', 'foreign_profile'):
        # The tail reader applies its own checks; whatever it renders is today's context, unchanged.
        assert prompt == run.context['context']
    else:
        assert 'Interrupted worker continuation' in prompt and 'Resumed after gateway restart' not in prompt


@pytest.mark.parametrize('kind', ['crash', 'timeout'])
def test_a_non_restart_requeue_is_unchanged(env, kind):
    _prior_session(env.db)
    tid = _task(env)
    if kind == 'crash':
        _, outcome = _lose_run(tid, code=1, state_db=env.db.db_path, monkeypatch=env.monkeypatch)
        assert outcome == 'crashed'
    else:
        with _connect() as conn:
            kb.recompute_ready(conn)
            assert kb.claim_task(conn, tid) is not None
            with kb.write_txn(conn):
                kb._end_run(conn, tid, outcome='timed_out', status='timed_out', error='max_runtime',
                            metadata={'exit_code': 124})
                conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL WHERE id=?", (tid,))
    run = _admit_next(env, tid)
    assert 'restart_resume' not in run.events and 'restart_resume' not in run.context
    assert 'resumed_session_id' not in run.events['owner_admitted']
    assert run.history == [] and 'resume_context' not in run.context


def test_a_crash_after_a_restart_loss_is_not_resumed(env):
    """Only the run IMMEDIATELY before counts: a crash after the restart loss is an ordinary requeue."""
    _prior_session(env.db)
    tid = _task(env)
    _lose_run(tid, code=KANBAN_OWNER_RESTART_EXIT_CODE, state_db=env.db.db_path, monkeypatch=env.monkeypatch)
    with _connect() as conn:
        kb.recompute_ready(conn)
    _lose_run(tid, code=1, state_db=env.db.db_path, pid=81002, monkeypatch=env.monkeypatch)
    run = _admit_next(env, tid)
    assert 'restart_resume' not in run.events and run.history == []


def test_seeding_is_idempotent_for_a_retried_admission(env):
    from gateway.session_kanban_resume import seed_resumed_session
    _prior_session(env.db)
    env.db.create_session(session_id=NEW, source='kanban', profile_name='default')
    plan = {'resumed_session_id': PRIOR, 'interrupted_calls': []}
    assert seed_resumed_session(env.db, NEW, plan) == 4
    assert seed_resumed_session(env.db, NEW, plan) == 0
    assert len(env.db.get_messages_as_conversation(NEW)) == 4
    assert env.db.get_session(NEW)['message_count'] == 4


def test_compaction_threshold_comes_from_the_compressor_config():
    from gateway.session_kanban_resume import compaction_threshold_tokens
    config = {'model': {'default': 'loop-model', 'context_length': 200_000}, 'compression': {'threshold': 0.5}}
    policy = SimpleNamespace(model='loop-model', provider='custom', base_url='', config=lambda: config)
    threshold = compaction_threshold_tokens(policy)
    assert threshold and 50_000 <= threshold < 200_000
    config['model']['context_length'] = 2_000_000
    assert (compaction_threshold_tokens(policy) or 0) > threshold


@pytest.mark.parametrize('history', [[], [{'role': 'assistant', 'content': 'PROGRESS-MARKER-42'}]])
def test_the_worker_turn_uses_the_resume_context_only_with_restored_history(monkeypatch, history):
    """The managed worker's first prompt: resume note when the copied history arrived, else the tail context."""
    from gateway import session_kanban
    monkeypatch.setattr('agent.kanban_turn_recovery.recover_failed_kanban_turns', lambda *a, **kw: None)
    seen = []
    agent = SimpleNamespace(session_id='sid', run_conversation=lambda text, **kw: seen.append((text, kw)) or {'final_response': 'ok'})
    context = {'skills': [], 'task_id': 't_x', 'goal_mode': False, 'context': 'TAIL-CONTEXT',
               'resume_context': 'RESUME-CONTEXT'}
    session_kanban._run_task_turns(agent, {'text': 'work kanban task t_x'}, history, context)
    prompt, kwargs = seen[0]
    assert kwargs['conversation_history'] is history
    assert ('RESUME-CONTEXT' in prompt) == bool(history) and ('TAIL-CONTEXT' in prompt) != bool(history)
    assert prompt.endswith('work kanban task t_x')
