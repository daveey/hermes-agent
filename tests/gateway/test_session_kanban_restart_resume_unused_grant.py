"""A Board lease grant released unused sits between an owner-restart loss and the next run without having started
anything; it must not hide the restart loss from the restart-resume selector. [daveey]

Live (hermes-hub, 2026-11-10 14:23 PST, t_01a1a1dea9c64d6a): run 163883 was lost to the gateway restart, the
Board then granted run 164239 and the gateway's final gate returned it (``unused_grant: true``), and run 164274
started with ``restart_candidate`` looking only at run 164239: no ``restart_resume`` event, the transcript was
not restored. Five cards had the same shape.

These tests use the real path that produces such a row: ``claim_gate`` acquires through ``HubLeaseClient`` against
the Board fake (``lease_runs=True``, release with ``unused_grant``), the release goes out through
``ClaimGate.__exit__``, and the run row and its events come back through ``ksc.sync_board``'s pull and
``apply_remote_changes``. Admission is the real ``run_task`` of ``test_session_kanban_restart_resume``.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from hermes_cli.kanban_db_dispatch_exit_code import KANBAN_OWNER_RESTART_EXIT_CODE
from tests.gateway import test_session_kanban_restart_resume as base
from tests.hermes_cli.kanban_sync_fake_board import FakeService

env = base.env  # the shared fixture, assigned locally (not imported by name)
PRIOR, NEW = base.PRIOR, base.NEW
BOARD, ME = 'owned', 'hub'


@pytest.fixture(autouse=True)
def _sync_installed(tmp_path):
    """``kanban.sync.enabled`` is read when the board DB is created, so it precedes the shared ``env`` fixture."""
    (tmp_path / 'config.yaml').write_text('kanban:\n  sync:\n    enabled: true\n')


class _FailureService(FakeService):
    """Model the Board's run-scoped release command, not a speculative local crash."""

    def row_op(self, b, op, gw):
        board, gateway = b, gw
        if op['op'] != 'release':
            return super().row_op(board, op, gateway)
        assert op['outcome'] == 'crashed' and not op['force_trip'] and not op['infrastructure']
        tid, run_uid = op['task'], op['run_uid']
        task = board['rows']['tasks'][tid]
        run = board['rows']['task_runs'][run_uid]
        assert run['task_id'] == tid and run['ended_at'] is None
        assert task['current_run_uid'] == run_uid
        assert board['leases'][tid]['run_uid'] == run_uid
        failures = int(task.get('consecutive_failures') or 0) + 1
        assert failures < (task.get('max_retries') or op['failure_limit'])
        stamp, op_id = task['hlc'], op['client_op_id']
        self._put(board, 'task_runs', run_uid,
                  {'status': 'crashed', 'outcome': 'crashed', 'ended_at': int(time.time()),
                   'error': op['error'], 'metadata': json.dumps(op['extra'])}, gateway, op_id, stamp)
        self._put(board, 'task_events', f'ev-crash-{run_uid}',
                  {'task_id': tid, 'run_uid': run_uid, 'kind': 'crashed',
                   'payload': json.dumps({'error': op['error'], 'failure_notice': 'canonical', **op['extra']}),
                   'created_at': int(time.time())}, gateway, op_id, stamp)
        board['leases'].pop(tid)
        row = self._put(board, 'tasks', tid,
                        {'status': 'ready', 'current_run_uid': None, 'lease_holder': None, 'lease_expires': None,
                         'consecutive_failures': failures, 'last_failure_error': op['error']}, gateway, op_id, stamp)
        return {'status': 'applied', 'row': row}


@pytest.fixture
def svc():
    service = _FailureService(lease_runs=True)
    previous = sync.set_lease_client(ksc.HubLeaseClient(service.hub(ME)))
    ksc.reset_board_watch()
    try:
        yield service
    finally:
        sync.set_lease_client(previous)
        ksc.reset_board_watch()


def _round(svc):
    res = ksc.sync_board(BOARD, kb.kanban_db_path(board=BOARD), svc.hub(ME), ME)
    assert res.error is None and (res.push is None or res.push.rejected == 0), res
    return res


def _go_primary(env, svc, tid):
    """Switch the board to a synced primary like production and let the Board learn the card and its history."""
    with base._connect() as conn:
        ksc.ensure_identity(conn, BOARD, ME)
        sync.set_mode(conn, 'primary')
    for _ in range(3):
        _round(svc)
    assert tid in svc.board(BOARD)['rows']['tasks']


def _unused_grant(svc, tid, *, release_as_unused=True):
    """One grant the gateway never used, as ``claim_gate`` + the final gate's refusal do: ``with gate: pass``."""
    with base._connect() as conn:
        gate = sync.claim_gate(conn, tid, int(time.time()) + 900)
        assert gate.lease is not None and not gate.deferred
        if release_as_unused:
            with gate:
                pass
        else:  # an ordinary release of the same grant (no ``unused_grant``)
            board_id, gateway_id, _ = gate._ids
            sync._client().release(board_id=board_id, task_uid=tid, run_uid=gate.lease.run_uid, gateway_id=gateway_id)
    _round(svc)  # the Board's run row and events reach this DB through the real pull/apply path


def _imported_grants(tid):
    with base._connect() as conn:
        return [r['id'] for r in conn.execute(
            "SELECT id FROM task_runs WHERE task_id=? AND metadata LIKE '%unused_grant%' ORDER BY id", (tid,))]


def _lost_then_grants(env, svc, n, *, session_kwargs=None):
    base._prior_session(env.db, **(session_kwargs or {}))
    tid = base._task(env)
    prior_run, outcome = base._lose_run(tid, code=KANBAN_OWNER_RESTART_EXIT_CODE, state_db=env.db.db_path,
                                        monkeypatch=env.monkeypatch)
    assert outcome == 'reclaimed'
    _go_primary(env, svc, tid)
    for _ in range(n):
        _unused_grant(svc, tid)
    grants = _imported_grants(tid)
    assert len(grants) == n and all(g > prior_run for g in grants)
    return tid, prior_run, grants


def test_the_imported_grant_row_has_the_live_shape(env, svc):
    """Anchor to the production rows (run 164239): released, ``{"unused_grant":true}``, claimed + released only."""
    _tid, _prior, [grant] = _lost_then_grants(env, svc, 1)
    with base._connect() as conn:
        row = conn.execute('SELECT * FROM task_runs WHERE id=?', (grant,)).fetchone()
        kinds = [k for (k,) in conn.execute('SELECT kind FROM task_events WHERE run_id=? ORDER BY id', (grant,))]
    assert (row['status'], row['outcome'], json.loads(row['metadata'])) == ('released', 'released', {'unused_grant': True})
    assert row['ended_at'] is not None and not row['claim_lock'] and not row['worker_pid']
    assert kinds == ['claimed', 'released']


@pytest.mark.parametrize('grants', [1, 3])
def test_unused_grants_after_a_restart_loss_still_resume_the_exact_transcript(env, svc, grants):
    from gateway.session_kanban_resume import worker_prompt_context
    tid, prior_run, skipped = _lost_then_grants(env, svc, grants)
    run = base._admit_next(env, tid)
    assert [(m['role'], m.get('content')) for m in run.history] == [(m['role'], m['content'])
                                                                    for m in base._work_transcript()]
    assert run.history[2]['tool_call_id'] == 'call_edit1'
    resume = run.events['restart_resume']
    assert resume['resumed_session_id'] == PRIOR and resume['prior_run_id'] == prior_run
    assert resume['copied_messages'] == 4 and 'fallback' not in resume
    assert resume['skipped_unused_grant_runs'] == skipped
    # New claim ownership: its own run, claim and session; the lost run's session is read, never written.
    assert run.run_id not in (prior_run, *skipped)
    assert run.events['owner_admitted']['session_id'] == NEW
    assert run.events['owner_admitted']['resumed_session_id'] == PRIOR
    assert len(env.db.get_messages_as_conversation(PRIOR)) == 4
    prompt = worker_prompt_context(run.context, run.history)
    assert 'Resumed after gateway restart' in prompt and f'run {prior_run}' in prompt
    assert f'run {skipped[0]}' not in prompt
    assert len(base._resume_events(tid, run.run_id)) == 1


def test_a_pending_tool_call_still_gets_its_synthetic_result_after_grants(env, svc):
    from gateway.session_kanban_resume import INTERRUPTED_TOOL_RESULT
    calls = [{'id': 'call_push9', 'type': 'function', 'function': {'name': 'terminal', 'arguments': '{}'}}]
    tid, _prior, _grants = _lost_then_grants(env, svc, 2, session_kwargs={'messages': [
        {'role': 'user', 'content': 'work kanban task'},
        {'role': 'assistant', 'content': 'Pushing.', 'tool_calls': calls}]})
    run = base._admit_next(env, tid)
    assert [m['role'] for m in run.history] == ['user', 'assistant', 'tool']
    assert run.history[-1]['content'] == INTERRUPTED_TOOL_RESULT
    assert run.events['restart_resume']['interrupted_calls'] == 1


def test_an_over_threshold_transcript_resumes_and_compacts_after_grants(env, svc):
    from gateway import session_kanban
    base._tiny_threshold(env)
    env.monkeypatch.setattr('agent.kanban_turn_recovery.recover_failed_kanban_turns', lambda *a, **kw: None)
    tid, prior_run, skipped = _lost_then_grants(env, svc, 2)
    run = base._admit_next(env, tid)
    assert run.events['restart_resume']['compaction'] == 'pending'
    assert run.events['restart_resume']['skipped_unused_grant_runs'] == skipped
    agent = base._WorkerAgent(env.db)
    session_kanban._run_task_turns(agent, {'text': f'work kanban task {tid}'}, run.history, run.context)
    assert [c[0] for c in agent.calls] == ['compress', 'model']
    outcome = base._resume_events(tid, run.run_id)[-1]
    assert outcome['compacted'] is True and outcome['prior_run_id'] == prior_run
    assert outcome['resumed_session_id'] == PRIOR


@pytest.mark.parametrize('case', ['foreign_home', 'foreign_profile', 'missing'])
def test_profile_and_home_isolation_still_falls_back_behind_grants(env, svc, case):
    session, other_db, _patch, expected, _tail = base._FALLBACKS[case]
    if session is not None:
        base._prior_session(env.db, **session)
    tid = base._task(env)
    state_db = env.tmp / other_db if other_db else env.db.db_path
    base._lose_run(tid, code=KANBAN_OWNER_RESTART_EXIT_CODE, state_db=state_db, monkeypatch=env.monkeypatch)
    _go_primary(env, svc, tid)
    _unused_grant(svc, tid)
    run = base._admit_next(env, tid)
    assert run.events['restart_resume']['fallback'] == expected
    assert 'resumed_session_id' not in run.events['owner_admitted'] and run.history == []


def _candidate(tid):
    from gateway.session_kanban_resume import restart_candidate
    with base._connect() as conn:
        last = conn.execute('SELECT MAX(id) FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0]
        return restart_candidate(conn, tid, last + 1)


def _tamper(tid, sql, *args):
    with base._connect() as conn, kb.write_txn(conn):
        conn.execute(sql, args)


def _event(tid, run_id, kind, payload=None):
    with base._connect() as conn, kb.write_txn(conn):
        kb._append_event(conn, tid, kind, payload, run_id=run_id)


# Every condition that makes a grant row an ordinary run again: each case alters the one imported grant row.
_UNUSED = '{"unused_grant":true'
_NOT_AN_UNUSED_GRANT = {
    'no_unused_flag': ('metadata=?', ('{}',)),
    'flag_false': ('metadata=?', ('{"unused_grant":false}',)),
    'flag_string': ('metadata=?', ('{"unused_grant":"true"}',)),
    'metadata_pid': ('metadata=?', (_UNUSED + ',"pid":7}',)),
    'metadata_claimer': ('metadata=?', (_UNUSED + ',"claimer":"h:1"}',)),
    'metadata_exit_code': ('metadata=?', (_UNUSED + ',"exit_code":0}',)),
    'metadata_owner_restart': ('metadata=?', (_UNUSED + ',"owner_restart":true}',)),
    'metadata_output': ('metadata=?', (_UNUSED + ',"worker_output":"x"}',)),
    'metadata_note': ('metadata=?', (_UNUSED + ',"attempt_note":"x"}',)),
    'metadata_unknown_key': ('metadata=?', (_UNUSED + ',"session_id":"s"}',)),
    'not_ended': ('ended_at=NULL', ()),
    'status_crashed': ('status=?', ('crashed',)),
    'outcome_crashed': ('outcome=?', ('crashed',)),
    'outcome_none': ('outcome=NULL', ()),
    'error': ('error=?', ('boom',)),
    'summary': ('summary=?', ('did things',)),
    'worker_pid': ('worker_pid=?', (4242,)),
    'heartbeat': ('last_heartbeat_at=?', (1794349800,)),
    'claim_lock': ('claim_lock=?', ('hub:1',)),
}


@pytest.mark.parametrize('case', sorted(_NOT_AN_UNUSED_GRANT))
def test_a_grant_row_altered_into_a_started_or_ordinary_run_blocks_the_resume(env, svc, case):
    tid, _prior, [grant] = _lost_then_grants(env, svc, 1)
    assert _candidate(tid) is not None  # control: the untouched row is skipped
    assignment, params = _NOT_AN_UNUSED_GRANT[case]
    _tamper(tid, f'UPDATE task_runs SET {assignment} WHERE id=?', *params, grant)
    assert _candidate(tid) is None


_EVIDENCE_EVENTS = {
    'spawned': ('spawned', {'pid': 9}),
    'owner_admitted': ('owner_admitted', {'db': 'x', 'session_id': 's', 'request_id': 'r'}),
    'worker_bound': ('worker_bound', {'pid': 9}),
    'heartbeat': ('heartbeat', None),
    'worker_result': ('worker_result', {'exit_code': 1}),
    'restart_resume': ('restart_resume', {'prior_run_id': 1}),
    'model_tier': ('model_tier', {'model': 'm'}),
    'local_claim': ('claimed', {'lock': 'hub:1', 'expires': 1, 'run_id': 1}),
    'released_not_unused': ('released', {'unused_grant': False}),
    'released_without_flag': ('released', {'error': None, 'retry_status': 'ready'}),
}


@pytest.mark.parametrize('case', sorted(_EVIDENCE_EVENTS))
def test_execution_or_admission_evidence_on_a_grant_blocks_the_resume(env, svc, case):
    tid, _prior, [grant] = _lost_then_grants(env, svc, 1)
    assert _candidate(tid) is not None
    kind, payload = _EVIDENCE_EVENTS[case]
    _event(tid, grant, kind, payload)
    assert _candidate(tid) is None


def _crash_run(env, svc, tid):
    run_id, outcome = base._lose_run(tid, code=1, state_db=env.db.db_path, pid=81002,
                                     monkeypatch=env.monkeypatch)
    # A primary queues the command but cannot close the run or reclaim locally.
    assert outcome is None
    with base._connect() as conn:
        run_uid = sync.run_uid(conn, run_id)
        commands = list(conn.execute("SELECT fields FROM kanban_sync_outbox WHERE op='release'"))
        assert len(commands) == 1 and json.loads(commands[0][0])['run_uid'] == run_uid
        assert conn.execute('SELECT status FROM tasks WHERE id=?', (tid,)).fetchone()[0] == 'running'
        assert conn.execute('SELECT ended_at FROM task_runs WHERE id=?', (run_id,)).fetchone()[0] is None
    _round(svc)  # push the command (a sync round pulls before it pushes)
    _round(svc)  # import the authoritative task/run/event feed
    with base._connect() as conn:
        task = conn.execute('SELECT status, lease_holder, lease_expires FROM tasks WHERE id=?', (tid,)).fetchone()
        run = conn.execute('SELECT status, outcome, ended_at FROM task_runs WHERE id=?', (run_id,)).fetchone()
        assert tuple(task) == ('ready', None, None)
        assert tuple(run[:2]) == ('crashed', 'crashed') and run['ended_at'] is not None
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE run_id=? AND kind='crashed'",
                            (run_id,)).fetchone()[0] == 1
        assert not list(conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE op='release'"))
        # The feed applying our own queued release clears the dead local claim in the same
        # transaction (kanban_release_claim), so the leftover sweep has nothing left to release.
        claim = conn.execute('SELECT claim_lock, worker_pid FROM tasks WHERE id=?', (tid,)).fetchone()
        assert tuple(claim) == (None, None)
        from hermes_cli import kanban_db_dispatch_claims as claims
        after_grace = time.time() + claims.ORPHAN_CLAIM_GRACE_SECONDS + 1
        with env.monkeypatch.context() as patch:
            patch.setattr(claims, 'time', SimpleNamespace(time=lambda: after_grace))
            assert claims.release_leftover_local_claims(conn) == []
    assert tid not in svc.board(BOARD)['leases']
    return run_id


def test_an_executed_run_between_two_unused_grants_blocks_the_resume(env, svc):
    tid, _prior, _ = _lost_then_grants(env, svc, 1)
    _crash_run(env, svc, tid)
    with base._connect() as conn:
        kb.recompute_ready(conn)
    _unused_grant(svc, tid)
    assert _candidate(tid) is None
    run = base._admit_next(env, tid)
    assert 'restart_resume' not in run.events and run.history == []


@pytest.mark.parametrize('kind', ['crash', 'timeout', 'ordinary_release', 'reclaim'])
def test_a_real_run_after_the_grants_blocks_the_resume(env, svc, kind):
    tid, _prior, _ = _lost_then_grants(env, svc, 2)
    if kind == 'crash':
        _crash_run(env, svc, tid)
    elif kind == 'ordinary_release':
        _unused_grant(svc, tid, release_as_unused=False)  # released through the real wire, not as unused
    else:
        with base._connect() as conn:
            kb.recompute_ready(conn)
            assert kb.claim_task(conn, tid) is not None
            if kind == 'timeout':
                with kb.write_txn(conn):
                    kb._end_run(conn, tid, outcome='timed_out', status='timed_out', error='max_runtime',
                                metadata={'exit_code': 124})
                    conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL WHERE id=?", (tid,))
            else:
                assert kb.reclaim_task(conn, tid, reason='operator')
    if kind == 'ordinary_release':
        with base._connect() as conn:
            row = conn.execute("SELECT metadata FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        assert 'unused_grant' not in (row[0] or '')  # the imported ordinary release carries no unused flag
    assert _candidate(tid) is None
    if kind == 'ordinary_release':
        # No explicit unused proof and no worker final: do not turn an
        # ambiguous cross-gateway release into either a resume or new spend.
        with base._connect() as conn:
            assert kb.claim_task(conn, tid) is None
            assert sync.pop_claim_deferral(tid) == 'usage_accounting_pending'
        return
    run = base._admit_next(env, tid)
    assert 'restart_resume' not in run.events and run.history == []


def test_a_grant_that_has_not_ended_yet_blocks_the_resume(env, svc):
    """A grant still held (the Board's run row not yet released) is somebody's live claim, never skipped."""
    tid, _prior, _ = _lost_then_grants(env, svc, 0)
    with base._connect() as conn:
        gate = sync.claim_gate(conn, tid, int(time.time()) + 900)
        assert gate.lease is not None
        gate.consumed = True  # held: no release goes out
    _round(svc)
    assert _candidate(tid) is None


def _insert_grants(tid, count):
    with base._connect() as conn, kb.write_txn(conn):
        for _ in range(count):
            run_id = conn.execute("INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at, metadata) "
                                  "VALUES (?, 'released', 'released', 1, 2, '{\"unused_grant\":true}')",
                                  (tid,)).lastrowid
            kb._append_event(conn, tid, 'claimed', {'holder': 'hub', 'run_uid': f'u{run_id}'}, run_id=run_id)
            kb._append_event(conn, tid, 'released', {'error': None, 'unused_grant': True}, run_id=run_id)


def test_the_skip_is_bounded(env):
    from gateway.session_kanban_resume import _MAX_UNUSED_GRANTS_SKIPPED as limit
    base._prior_session(env.db)
    tid = base._task(env)
    base._lose_run(tid, code=KANBAN_OWNER_RESTART_EXIT_CODE, state_db=env.db.db_path, monkeypatch=env.monkeypatch)
    _insert_grants(tid, limit)
    assert len(_candidate(tid)['skipped_unused_grant_runs']) == limit
    _insert_grants(tid, 1)
    assert _candidate(tid) is None


def test_a_card_with_only_unused_grants_has_no_candidate(env):
    base._prior_session(env.db)
    tid = base._task(env)
    _insert_grants(tid, 2)
    assert _candidate(tid) is None
