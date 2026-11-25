"""[daveey] A run requeued by an owner restart (``owner_restart_lost``) resumes the lost run's conversation.

Real board, real dispatcher sweep (the restart loss and the crash are booked by ``detect_crashed_workers``
from the worker's exit code), real ``SessionDB`` and the real ``run_task`` admission; only the owner's local
session creation is a stand-in that creates the fresh session row the way the owner does.
"""
import asyncio
from contextlib import closing
import json
import os
import time
from types import SimpleNamespace

import sqlite3

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


def _lose_run(task_id, *, code, state_db, session_id=PRIOR, pid=81001, monkeypatch, accounted=True):
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
            kb._append_event(conn, task_id, 'worker_bound',
                             {'pid': os.getpid(), 'claim_lock': task.claim_lock}, run_id=task.current_run_id)
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
    if accounted:
        # Transcript-resume tests require independently settled spend, not an
        # accounting bypass. The unknown-interruption case below starts without it.
        from gateway.session_kanban import _record_worker_result
        _record_worker_result({'db': str(kb.kanban_db_path(board='owned')), 'task_id': task_id,
                               'run_id': task.current_run_id, 'claim_lock': task.claim_lock}, 0, '',
                              agent=SimpleNamespace(session_input_tokens=10, session_output_tokens=2,
                                                    session_cache_read_tokens=123, session_cache_write_tokens=0))
    return task.current_run_id, row[0]


def test_unknown_restart_accounting_blocks_transcript_readmission(env):
    from hermes_cli.kanban_db_sync import pop_claim_deferral
    tid = _task(env)
    _prior_session(env.db)
    run_id, outcome = _lose_run(tid, code=KANBAN_OWNER_RESTART_EXIT_CODE,
                                state_db=env.db.db_path, monkeypatch=env.monkeypatch, accounted=False)
    assert outcome == 'reclaimed'
    with _connect() as conn:
        assert kb.claim_task(conn, tid) is None
        assert pop_claim_deferral(tid) == 'usage_accounting_pending'
        assert conn.execute('SELECT current_run_id FROM tasks WHERE id=?', (tid,)).fetchone()[0] is None
        assert conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0] == 1
        assert conn.execute('SELECT id FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0] == run_id


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
    from gateway.session_contract import SessionActor
    actor = SessionActor('owner', str(env.tmp), frozenset({'session:create'}), 'transport')
    connection = SimpleNamespace(authority=authority, actor=actor, native_owner=True, resume=resume)
    params = dict(board='owned', task_id=task_id, run_id=task.current_run_id, claim_lock=task.claim_lock)
    from gateway import session_kanban
    env.monkeypatch.setattr(session_kanban, 'asdict', lambda r: {'state': r.state})
    asyncio.run(session_kanban.run_task(connection, params))
    assert [c[0] for c in calls] == ['submit']  # transcript viewers are not part of admission
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


def _broken_reader(env):
    real = env.db.get_messages_as_conversation

    def broken(sid, *a, **kw):
        if sid == PRIOR:
            raise ValueError('corrupt transcript')
        return real(sid, *a, **kw)
    env.monkeypatch.setattr(env.db, 'get_messages_as_conversation', broken)


def _broken_estimate(env):
    def boom(*a, **kw):
        raise TypeError('odd row')
    env.monkeypatch.setattr('agent.usage_anchor.persisted_anchor_tokens', boom)


def _rotated(env):
    env.db._execute_write(lambda conn: conn.execute("UPDATE sessions SET end_reason='compression' WHERE id=?", (PRIOR,)))


def _broken_seed(env):
    def boom(*a, **kw):
        raise sqlite3.OperationalError('disk I/O error')
    env.monkeypatch.setattr('gateway.session_kanban_resume.seed_resumed_session', boom)


def _tiny_threshold(env):
    env.monkeypatch.setattr('gateway.session_kanban_resume.compaction_threshold_tokens', lambda policy: 10)


# case -> (prior session setup, owner_admitted db relative to the real one, extra patch, expected fallback,
#          whether the transcript-tail reader still renders a tail for it)
_FALLBACKS = {
    'missing': (None, None, None, 'prior_session_missing', False),  # names a session absent from state.db
    'empty': ({'messages': []}, None, None, 'prior_session_empty', False),
    'foreign_home': ({}, 'other-home/state.db', None, 'foreign_home', False),
    'foreign_profile': ({'profile': 'other'}, None, None, 'foreign_profile', False),
    'unreadable': ({}, None, _broken_reader, 'prior_session_unreadable', True),
    'plan_raises': ({}, None, _broken_estimate, 'plan_failed', True),
    'rotated': ({}, None, _rotated, 'prior_session_rotated', True),
    'seed_raises': ({}, None, _broken_seed, 'seed_failed', True),
}


@pytest.mark.parametrize('case', sorted(_FALLBACKS))
def test_each_fallback_keeps_the_tail_only_fresh_session(env, case):
    from gateway.session_kanban_resume import worker_prompt_context
    session, other_db, patch, expected, tail = _FALLBACKS[case]
    if session is not None:
        _prior_session(env.db, **session)
    if patch is not None:
        patch(env)
    state_db = env.tmp / other_db if other_db else env.db.db_path
    tid = _task(env)
    _lose_run(tid, code=KANBAN_OWNER_RESTART_EXIT_CODE, state_db=state_db, monkeypatch=env.monkeypatch)
    run = _admit_next(env, tid)
    assert run.events['restart_resume']['fallback'] == expected
    assert 'resumed_session_id' not in run.events['restart_resume']
    assert 'resumed_session_id' not in run.events['owner_admitted']
    assert run.history == []  # a fresh session
    prompt = worker_prompt_context(run.context, run.history)
    # Today's context, unchanged: the tail reader applies its own checks and renders a tail where it can.
    assert prompt == run.context['context']
    assert ('Interrupted worker continuation' in prompt) == tail and 'Resumed after gateway restart' not in prompt


def _resume_events(task_id, run_id):
    with _connect() as conn:
        return [json.loads(p) for (p,) in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='restart_resume' ORDER BY id",
            (task_id, run_id))]


class _WorkerAgent:
    """The managed worker's agent at the seam ``_run_task_turns`` drives: its real session store, an in-place
    ``_compress_context`` stand-in (``mode``: rewrite the session to a summary, raise, or return the input
    unchanged as an aborted compaction does) and a ``run_conversation`` that records the first model call."""

    def __init__(self, db, mode='compact', enabled=True):
        self.session_id, self._session_db, self.compression_enabled = NEW, db, enabled
        self.mode, self.calls = mode, []

    def _compress_context(self, messages, system_message, **kw):
        self.calls.append(('compress', len(messages), kw.get('trigger')))
        if self.mode == 'raise':
            raise RuntimeError('summary model unavailable')
        if self.mode == 'abort':
            return messages, 'SP'
        summary = [{'role': 'user', 'content': '[CONTEXT COMPACTION] summary: PROGRESS-MARKER-42, a.py patched'},
                   messages[-1]]
        self._session_db.archive_and_compact(self.session_id, summary)  # in-place compaction's commit
        return summary, 'SP'

    def run_conversation(self, text, **kw):
        self.calls.append(('model', text, kw['conversation_history']))
        return {'final_response': 'ok'}


def _over_threshold_admission(env):
    _prior_session(env.db)
    _tiny_threshold(env)  # 4 messages are far past 85% of a 10-token threshold
    tid = _task(env)
    prior_run, _ = _lose_run(tid, code=KANBAN_OWNER_RESTART_EXIT_CODE, state_db=env.db.db_path,
                             monkeypatch=env.monkeypatch)
    env.monkeypatch.setattr('agent.kanban_turn_recovery.recover_failed_kanban_turns', lambda *a, **kw: None)
    return tid, prior_run, _admit_next(env, tid)


def test_an_over_threshold_prior_session_resumes_and_is_compacted_before_the_first_model_call(env):
    from gateway import session_kanban
    tid, prior_run, run = _over_threshold_admission(env)
    # Admission resumes instead of falling back to the tail: the whole transcript is copied, compaction pending.
    admitted = run.events['restart_resume']
    assert admitted['resumed_session_id'] == PRIOR and admitted['compaction'] == 'pending'
    assert admitted['copied_messages'] == 4 and 'fallback' not in admitted and admitted['threshold'] == 10
    assert run.events['owner_admitted']['resumed_session_id'] == PRIOR and len(run.history) == 4
    agent = _WorkerAgent(env.db)
    session_kanban._run_task_turns(agent, {'text': f'work kanban task {tid}'}, run.history, run.context)
    # Compaction ran first, then the one model call got the compacted history and the resume prompt.
    assert [c[0] for c in agent.calls] == ['compress', 'model']
    assert agent.calls[0] == ('compress', 4, 'turn_start_threshold')
    _, prompt, sent = agent.calls[1]
    assert sent[0]['content'].startswith('[CONTEXT COMPACTION]') and len(sent) == 2
    assert 'Resumed after gateway restart' in prompt and 'Interrupted worker continuation' not in prompt
    assert [m['content'] for m in env.db.get_messages_as_conversation(NEW)] == [m['content'] for m in sent]
    # The run's outcome event: resumed AND compacted.
    outcome = _resume_events(tid, run.run_id)[-1]
    assert outcome['compacted'] is True and outcome['resumed_session_id'] == PRIOR
    assert outcome['prior_run_id'] == prior_run and outcome['compacted_messages'] == 2 and 'fallback' not in outcome
    assert len(env.db.get_messages_as_conversation(PRIOR)) == 4  # the prior session is still never written


@pytest.mark.parametrize('mode,enabled,reason', [('raise', True, 'compaction_failed'),
                                                 ('abort', True, 'compaction_failed'),
                                                 ('compact', False, 'compaction_unavailable')])
def test_a_failed_compaction_falls_back_to_the_tail_only_fresh_session(env, mode, enabled, reason):
    from gateway import session_kanban
    tid, _prior, run = _over_threshold_admission(env)
    agent = _WorkerAgent(env.db, mode=mode, enabled=enabled)
    session_kanban._run_task_turns(agent, {'text': f'work kanban task {tid}'}, run.history, run.context)
    assert [c[0] for c in agent.calls] == (['model'] if not enabled else ['compress', 'model'])
    _, prompt, sent = agent.calls[-1]
    # Today's fallback: no copied rows left, the transcript-tail context instead of the resume note.
    assert sent == [] and env.db.get_messages_as_conversation(NEW) == []
    assert prompt.startswith(run.context['context']) and 'Interrupted worker continuation' in prompt
    assert 'Resumed after gateway restart' not in prompt
    outcome = _resume_events(tid, run.run_id)[-1]
    assert outcome['fallback'] == reason and 'resumed_session_id' not in outcome and 'compacted' not in outcome
    assert ('error' in outcome) == (mode == 'raise')


def test_hub_ev_380043_below_threshold_but_past_headroom_resumes_then_compacts(env):
    """The exact hub case (t_01a19a4f03a78fea, run 26639): 222,858 tokens, threshold 231,200. That is BELOW the
    threshold but past ``RESUME_HEADROOM`` (0.85 x 231,200 = 196,520), the comparison that used to fall back to
    the tail (``over_compaction_threshold``) and discard 23.7 run-min. It must resume its own session and compact."""
    from gateway import session_kanban
    from gateway.session_kanban_resume import RESUME_HEADROOM
    tokens, threshold = 222_858, 231_200
    assert threshold * RESUME_HEADROOM <= tokens < threshold
    env.monkeypatch.setattr('gateway.session_kanban_resume.compaction_threshold_tokens', lambda policy: 231_200)
    env.monkeypatch.setattr('agent.usage_anchor.persisted_anchor_tokens', lambda *a, **kw: 222_858)
    _prior_session(env.db)
    tid = _task(env)
    _lose_run(tid, code=KANBAN_OWNER_RESTART_EXIT_CODE, state_db=env.db.db_path, monkeypatch=env.monkeypatch)
    env.monkeypatch.setattr('agent.kanban_turn_recovery.recover_failed_kanban_turns', lambda *a, **kw: None)
    run = _admit_next(env, tid)
    admitted = run.events['restart_resume']
    assert (admitted['tokens'], admitted['threshold']) == (222_858, 231_200)
    assert 'fallback' not in admitted and admitted['resumed_session_id'] == PRIOR and admitted['compaction'] == 'pending'
    agent = _WorkerAgent(env.db)
    session_kanban._run_task_turns(agent, {'text': f'work kanban task {tid}'}, run.history, run.context)
    assert [c[0] for c in agent.calls] == ['compress', 'model']
    outcome = _resume_events(tid, run.run_id)[-1]
    assert outcome['compacted'] is True and outcome['resumed_session_id'] == PRIOR and outcome['tokens'] == 222_858


def test_an_under_headroom_resume_is_not_compacted(env):
    from gateway import session_kanban
    _prior_session(env.db)
    tid = _task(env)
    _lose_run(tid, code=KANBAN_OWNER_RESTART_EXIT_CODE, state_db=env.db.db_path, monkeypatch=env.monkeypatch)
    env.monkeypatch.setattr('agent.kanban_turn_recovery.recover_failed_kanban_turns', lambda *a, **kw: None)
    run = _admit_next(env, tid)
    assert 'compaction' not in run.events['restart_resume']
    agent = _WorkerAgent(env.db)
    session_kanban._run_task_turns(agent, {'text': f'work kanban task {tid}'}, run.history, run.context)
    assert [c[0] for c in agent.calls] == ['model'] and len(agent.calls[0][2]) == 4
    assert len(_resume_events(tid, run.run_id)) == 1


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
    assert 'restart_resume' not in run.events and 'restart_candidate' not in run.context
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
