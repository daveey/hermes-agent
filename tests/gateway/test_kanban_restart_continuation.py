"""A managed Kanban run continues across its owner gateway's restart: no reclaim. [daveey]

120 runs in 7 days on hermes-hub ended at a gateway restart (``hermes update``): the owner booked the
turn as a restart loss, the worker exited 76 and the sweep requeued it ``reclaimed``/``owner_restart_lost``
into a fresh run that copied the transcript. Now the stopping owner checkpoints the turn, the worker
keeps its claim and waits for the next owner, and that owner continues the same turn in the same session.
"""
import asyncio
from contextlib import asynccontextmanager, closing
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from hermes_cli import gateway_client, kanban_worker_client
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_db_dispatch_exit_code import KANBAN_OWNER_RESTART_EXIT_CODE

_FORBIDDEN = {'reclaimed', 'crashed', 'owner_restart_lost', 'gave_up', 'timed_out'}


# --- real owners: dispatcher argv, two gateway.run processes on one disposable home ---------------

@pytest.mark.parametrize('mid_tool', [False, True], ids=['mid_model_call', 'mid_tool_call'])
def test_a_real_owner_restart_mid_turn_continues_the_same_run(tmp_path, mid_tool):
    import subprocess
    import sys
    repo = Path(__file__).resolve().parents[2]
    from tests.gateway.fixtures.local_recovery_probe import child_env
    env = child_env()
    env.update(HOME=str(tmp_path / 'home'), USERPROFILE=str(tmp_path / 'home'), HERMES_HOME=str(tmp_path / 'state'),
               PYTHONPATH=f"{repo}{__import__('os').pathsep}{repo / 'tests' / 'gateway' / 'fixtures'}",
               PROBE_MID_TOOL='1' if mid_tool else '0')
    result = subprocess.run([sys.executable, str(Path(__file__).parent / 'fixtures' / 'kanban_restart_continue_probe.py')],
                            env=env, cwd=repo, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=420,
                            check=False)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
    receipt = json.loads((tmp_path / 'state' / 'receipt.json').read_text())
    print(json.dumps(receipt))
    assert receipt['owner_pids'][0] != receipt['owner_pids'][1]
    # The gap: the stopped owner checkpointed the turn and the run kept its claim and worker.
    assert receipt['checkpointed'] and receipt['client_alive_in_gap'], receipt
    assert (receipt['gap_status'], receipt['gap_run'], receipt['gap_claim'], receipt['gap_worker_pid']) == (
        'running', receipt['run_id'], receipt['claim_lock'], receipt['worker_pid']), receipt
    # The next owner continued it: same run, one worker, exit 0, no reclaim/crash/failure.
    assert receipt['client_exit'] == ['clean_exit', 0] and receipt['clients'] == 1, receipt
    assert receipt['task_status'] == 'done' and receipt['failures'] == 0, receipt
    assert receipt['runs'] == [[receipt['run_id'], 'completed', 'done']], receipt
    assert not _FORBIDDEN & set(receipt['kinds']), receipt['kinds']
    assert 'restart_continued' in receipt['kinds'], receipt['kinds']
    # One session: the stopped admission settled interrupted and its continuation completed there.
    sessions = {row[0] for row in receipt['admissions']}
    assert len(sessions) == 1 and [row[2:] for row in receipt['admissions']] == [
        ['terminal', 'interrupted'], ['terminal', 'completed']], receipt['admissions']
    assert receipt['admissions'][1][1].startswith(receipt['admissions'][0][1] + '#continue:')
    assert receipt['continued_in_history'], 'the continued turn did not carry the first turn as history'
    if mid_tool:
        # The tool call the Stop cut off reaches the continued turn with exactly the update-pause result.
        import re
        assert len(receipt['mid_tool_results']) == 1, receipt['mid_tool_results']
        assert re.fullmatch(r'\[interrupted by Hermes update at \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ: re-run if still needed\]',
                            receipt['mid_tool_results'][0]), receipt['mid_tool_results']


# --- the worker: a fake owner that restarts mid-turn -------------------------------------------

@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.delenv('HERMES_TUI_GATEWAY_URL', raising=False)
    monkeypatch.setenv('HERMES_RESTART_AFTER_TURN_TIMEOUT', '1800')
    monkeypatch.setenv('HERMES_KANBAN_CRASH_GRACE_SECONDS', '0')
    monkeypatch.setattr(kbd, '_worker_final_output', lambda *a, **kw: '')
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='card mid-turn at the restart', assignee='default')
        host = kb._claimer_id().split(':', 1)[0]
        kb.claim_task(conn, tid, claimer=f'{host}:w81001')
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET worker_pid=?, started_at=? WHERE id=?', (81001, int(time.time()) - 600, tid))
        task = kb.get_task(conn, tid)
        assert task is not None
        params = dict(board='default', task_id=tid, run_id=task.current_run_id, claim_lock=task.claim_lock)
        request = 'kanban:' + json.dumps(['default', tid, task.current_run_id], separators=(',', ':'))
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'owner_admitted', {'session_id': 's1', 'request_id': request},
                             run_id=task.current_run_id)
    return SimpleNamespace(db=str(tmp_path / 'kanban.db'), params=params, request=request, home=tmp_path)


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(kanban_worker_client, 'time', SimpleNamespace(monotonic=lambda: now[0]))

    async def _sleep(seconds):
        now[0] += seconds
    monkeypatch.setattr(kanban_worker_client.asyncio, 'sleep', _sleep)
    return now


def _book(board, payload, kind='worker_result'):
    with kbc.connect() as conn, kb.write_txn(conn):
        kb._append_event(conn, board.params['task_id'], kind, payload, run_id=board.params['run_id'])


def _checkpoint(board, request_id):
    """What the stopping owner books (gateway.session_kanban_restart, checkpoint form)."""
    _book(board, {'reason': 'core_update_pause', 'checkpoint': 'tok1', 'session_id': 's1', 'request_id': request_id,
                  'admission_id': 'a1', 'claim_lock': board.params['claim_lock']}, kind='restart_checkpoint')
    _book(board, {'claim_lock': board.params['claim_lock'], 'exit_code': KANBAN_OWNER_RESTART_EXIT_CODE,
                  'owner_restart': True, 'continuable': True, 'checkpoint': 'tok1',
                  'last_output': 'Kanban turn checkpointed at a gateway restart/shutdown'})


class _RestartingOwner:
    """Dial 1: the old owner admits the turn (a1), then its restart Stop checkpoints it and cuts the socket.
    Dials until ``gap``: refused (the owner is down). After: the new owner reports a1 ``terminal`` and,
    asked ``kanban.run`` again, continues the turn (a2) to ``final`` (or refuses with ``refusal``)."""

    def __init__(self, board, clock, *, gap=90.0, refusal=None):
        self.board, self.clock, self.gap, self.refusal = board, clock, gap, refusal
        self.runs, self.dials = [], 0

    def connect(self):
        owner = self

        @asynccontextmanager
        async def _connect(*_, **__):
            owner.dials += 1
            first = owner.dials == 1
            if not first and owner.clock[0] < owner.gap:
                raise ConnectionRefusedError('owner gone')

            class _Client:
                events = asyncio.Queue()

                async def rpc(self, method, **params):
                    if method == 'kanban.run':
                        owner.runs.append(params)
                        if first:
                            return {'session_id': 's1', 'receipt': {'status': 'started', 'admission_id': 'a1'}}
                        if owner.refusal:
                            raise gateway_client.GatewayRPCError(owner.refusal)
                        from gateway.session_kanban_continue import pending_admission
                        target, checkpoint = pending_admission(owner.board.db, owner.board.params, owner.board.request)
                        assert checkpoint is not None and target == owner.board.request + '#continue:tok1'
                        _book(owner.board, {'session_id': 's1', 'request_id': target}, kind='owner_admitted')
                        # The continued turn finishes the card's work: the child books its own result.
                        _book(owner.board, {'claim_lock': owner.board.params['claim_lock'], 'exit_code': 0,
                                            'last_output': 'CONTINUED_DONE'})
                        return {'session_id': 's1', 'receipt': {'status': 'terminal', 'admission_id': 'a2'}}
                    if first:
                        _checkpoint(owner.board, owner.board.request)
                        raise gateway_client.GatewayClientError('Gateway disconnected; turn outcome is unknown')
                    assert params['admission_id'] == 'a1'
                    return {'status': 'terminal', 'outcome': 'interrupted'}

            yield _Client()

        return _connect


def _env(monkeypatch, board):
    for key, value in {'HERMES_KANBAN_BOARD': 'default', 'HERMES_KANBAN_TASK': board.params['task_id'],
                       'HERMES_KANBAN_RUN_ID': str(board.params['run_id']),
                       'HERMES_KANBAN_CLAIM_LOCK': board.params['claim_lock'], 'HERMES_KANBAN_DB': board.db}.items():
        monkeypatch.setenv(key, value)


def _sweep(monkeypatch, rc):
    """The dispatcher books the worker's exit the way it would after reaping it."""
    monkeypatch.setattr(kb, '_pid_alive', lambda pid: False)
    # The owner-reclaim pause is the old owner's admission evidence (a real pid/bound interpreter this
    # fixture does not fake); test_kanban_worker_owner_restart covers it. Here: the exit's booking.
    from hermes_cli import kanban_owner_recovery
    monkeypatch.setattr(kanban_owner_recovery, 'owner_reclaim_blocked', lambda *a, **k: False)
    monkeypatch.setattr(kbd, '_recent_worker_exits', {81001: ((rc & 0xFF) << 8, time.time())})
    with kbc.connect() as conn:
        kbd.detect_crashed_workers(conn)


def test_a_worker_whose_owner_restarts_mid_turn_continues_the_same_run(board, clock, monkeypatch, capsys):
    owner = _RestartingOwner(board, clock)
    monkeypatch.setattr(gateway_client, 'connect_gateway', owner.connect())
    _env(monkeypatch, board)

    rc = kanban_worker_client.main()

    assert rc == 0, capsys.readouterr().err
    assert 'CONTINUED_DONE' in capsys.readouterr().out
    assert len(owner.runs) == 2  # the same attempt asked again, never a new one
    assert owner.runs[0]['run_id'] == owner.runs[1]['run_id'] == board.params['run_id']
    assert owner.runs[1]['claim_lock'] == board.params['claim_lock']
    with kbc.connect() as conn:
        task = kb.get_task(conn, board.params['task_id'])
        assert task is not None
        # Through the gap the run kept its claim: a live worker is never reclaimed.
        assert (task.status, task.current_run_id, task.claim_lock, task.consecutive_failures) == (
            'running', board.params['run_id'], board.params['claim_lock'], 0)
        kinds = [r[0] for r in conn.execute('SELECT kind FROM task_events WHERE task_id=?', (task.id,))]
    assert not _FORBIDDEN & set(kinds), kinds


def test_the_continued_turn_waits_out_a_restart_longer_than_owner_wait(board, clock, monkeypatch):
    # The worker learned of the checkpoint (a1 terminal) before the owner went away: the gap may last
    # up to CONTINUE_WAIT_S (> OWNER_WAIT_S), covering the old owner's exit and the new one's start.
    owner = _RestartingOwner(board, clock, gap=kanban_worker_client.OWNER_WAIT_S + 60)
    monkeypatch.setattr(gateway_client, 'connect_gateway', owner.connect())
    _checkpoint(board, board.request)

    async def _first(client, params, board_db, claim):
        claim.session_id, claim.admission_id = 's1', 'a1'
        return {'status': 'terminal'}
    real = kanban_worker_client._await_receipt
    calls = []

    async def _await(client, params, board_db, claim):
        calls.append(1)
        return await (_first if len(calls) == 1 else real)(client, params, board_db, claim)
    monkeypatch.setattr(kanban_worker_client, '_await_receipt', _await)
    assert kanban_worker_client.CONTINUE_WAIT_S <= 300
    assert asyncio.run(kanban_worker_client.run(board.params, board.db)) == 0
    assert clock[0] >= kanban_worker_client.OWNER_WAIT_S


def test_an_update_gap_longer_than_owner_wait_still_continues(board, clock, monkeypatch, capsys):
    # ``hermes update``: the old owner checkpoints the turn but its restart wait refuses us, then it exits
    # and the tree moves. No terminal receipt is ever seen before the gap; the board shows the checkpoint.
    owner = _RestartingOwner(board, clock, gap=kanban_worker_client.OWNER_WAIT_S + 90)
    monkeypatch.setattr(gateway_client, 'connect_gateway', owner.connect())
    assert asyncio.run(kanban_worker_client.run(board.params, board.db)) == 0
    assert clock[0] >= kanban_worker_client.OWNER_WAIT_S + 90
    assert len(owner.runs) == 2


def test_an_owner_gap_with_no_checkpoint_still_fails_at_owner_wait(board, clock, monkeypatch):
    # Without a checkpoint on the board the old bound holds: silence past OWNER_WAIT_S ends the worker.
    owner = _RestartingOwner(board, clock, gap=kanban_worker_client.OWNER_WAIT_S + 90)
    monkeypatch.setattr(kanban_worker_client, '_checkpoint', lambda *a: None)
    monkeypatch.setattr(gateway_client, 'connect_gateway', owner.connect())
    with pytest.raises(gateway_client.GatewayClientError, match='silent'):
        asyncio.run(kanban_worker_client.run(board.params, board.db))
    assert clock[0] < kanban_worker_client.OWNER_WAIT_S + 30


def test_after_the_continuation_is_admitted_owner_errors_are_final_again(board, clock, monkeypatch):
    # Once the next owner admitted the continued turn, a final owner answer is that answer (not
    # a restart loss requeued uncounted), as for any first admission.
    owner = _RestartingOwner(board, clock)
    base = owner.connect()

    @asynccontextmanager
    async def _connect(*a, **k):
        async with base(*a, **k) as client:
            if owner.dials == 1:
                yield client
                return
            real = client.rpc

            async def rpc(method, **params):
                if method == 'kanban.run':
                    owner.runs.append(params)
                    return {'session_id': 's1', 'receipt': {'status': 'started', 'admission_id': 'a2'}}
                if params.get('admission_id') == 'a2':
                    raise gateway_client.GatewayRPCError('kanban_claim_mismatch')
                return await real(method, **params)
            client.rpc = rpc
            yield client
    monkeypatch.setattr(gateway_client, 'connect_gateway', _connect)
    with pytest.raises(gateway_client.GatewayRPCError, match='kanban_claim_mismatch') as raised:
        asyncio.run(kanban_worker_client.run(board.params, board.db))
    assert not isinstance(raised.value, kanban_worker_client.OwnerRestartLost)
    assert len(owner.runs) == 2


def test_an_owner_that_will_not_continue_falls_back_to_the_uncounted_requeue(board, clock, monkeypatch, capsys):
    owner = _RestartingOwner(board, clock, refusal='kanban_continuation_unsafe')
    monkeypatch.setattr(gateway_client, 'connect_gateway', owner.connect())
    _env(monkeypatch, board)

    rc = kanban_worker_client.main()

    assert rc == KANBAN_OWNER_RESTART_EXIT_CODE, capsys.readouterr().err
    _sweep(monkeypatch, rc)
    with kbc.connect() as conn:
        task = kb.get_task(conn, board.params['task_id'])
        assert task is not None
        assert (task.status, task.consecutive_failures) == ('ready', 0)
        outcome = conn.execute('SELECT outcome FROM task_runs WHERE id=?', (board.params['run_id'],)).fetchone()[0]
        assert outcome == 'reclaimed'
        kinds = [r[0] for r in conn.execute('SELECT kind FROM task_events WHERE task_id=?', (task.id,))]
    assert kinds.count('owner_restart_lost') == 1 and 'crashed' not in kinds and 'gave_up' not in kinds


def test_a_checkpoint_is_continued_at_most_once(board, clock, monkeypatch):
    # The same token again (the new owner never booked a newer result) is the final result: exit 76.
    claim = kanban_worker_client._Claim(answered=0.0)
    _checkpoint(board, board.request)
    assert kanban_worker_client._checkpoint(board.params, board.db, claim) == 'tok1'
    claim.continue_after('tok1')
    assert kanban_worker_client._checkpoint(board.params, board.db, claim) is None


def test_a_checkpoint_token_without_continuable_is_final(board):
    _book(board, {'claim_lock': board.params['claim_lock'], 'exit_code': KANBAN_OWNER_RESTART_EXIT_CODE,
                  'owner_restart': True, 'checkpoint': 'tok1'})
    assert kanban_worker_client._checkpoint(board.params, board.db, kanban_worker_client._Claim(answered=0.0)) is None


def test_a_restart_storm_stops_continuing_after_max_continuations(board):
    # Each restart books a new token; past MAX_CONTINUATIONS the run is requeued uncounted instead.
    claim = kanban_worker_client._Claim(answered=0.0)
    for i in range(kanban_worker_client.MAX_CONTINUATIONS):
        claim.continue_after(f'old{i}')
    _checkpoint(board, board.request)
    assert kanban_worker_client._checkpoint(board.params, board.db, claim) is None
    claim.checkpoints = claim.checkpoints[1:]
    assert kanban_worker_client._checkpoint(board.params, board.db, claim) == 'tok1'


def test_a_lost_turn_is_not_a_checkpoint(board):
    # The ordinary restart-loss booking (no clean result: the child was lost) carries no checkpoint.
    _book(board, {'claim_lock': board.params['claim_lock'], 'exit_code': KANBAN_OWNER_RESTART_EXIT_CODE,
                  'owner_restart': True})
    claim = kanban_worker_client._Claim(answered=0.0)
    assert kanban_worker_client._checkpoint(board.params, board.db, claim) is None


# --- the owners: checkpoint booking, then the continuation's safety checks ----------------------

def _policy(board):
    return SimpleNamespace(kanban_json=json.dumps({'db': board.db, **board.params}))


def test_the_stopping_owner_books_a_continuable_checkpoint(board):
    from gateway.session_kanban_restart import record_restart_interruption
    record_restart_interruption(_policy(board), checkpoint={'session_id': 's1', 'request_id': board.request,
                                                            'admission_id': 'a1', 'cause': 'restart'})
    from gateway.session_kanban import worker_result
    result = worker_result(board.db, board.params)
    assert result['exit_code'] == KANBAN_OWNER_RESTART_EXIT_CODE and result['continuable'] is True
    from gateway.session_kanban_continue import pending_admission
    target, checkpoint = pending_admission(board.db, board.params, board.request)
    assert checkpoint is not None and target == board.request + '#continue:' + result['checkpoint']
    assert checkpoint['reason'] == 'core_update_pause' and checkpoint['session_id'] == 's1'
    # A retried ask after the continuation was admitted resubmits that same continuation.
    _book(board, {'session_id': 's1', 'request_id': target}, kind='owner_admitted')
    assert pending_admission(board.db, board.params, board.request) == (target, None)


def test_a_lost_child_is_booked_without_a_checkpoint(board):
    from gateway.session_kanban_restart import record_restart_interruption
    record_restart_interruption(_policy(board))
    from gateway.session_kanban import worker_result
    assert 'continuable' not in worker_result(board.db, board.params)
    from gateway.session_kanban_continue import pending_admission
    assert pending_admission(board.db, board.params, board.request) == (board.request, None)


def _session_db(tmp_path, history):
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / 'state.db')
    db.create_session('s1', 'kanban')
    for message in history:
        db.append_message('s1', **message)
    return db


def _prepare(board, db, checkpoint, monkeypatch, admission=None):
    import hermes_state_runtime
    from gateway import session_kanban_continue as cont
    admission = admission or {'status': 'terminal', 'outcome': 'interrupted', 'admission_id': 'a1'}
    monkeypatch.setattr(hermes_state_runtime, 'find_session_admission', lambda *a, **k: admission)
    target = cont.continuation_request_id(board.request, checkpoint['checkpoint'])
    return cont.prepare(SimpleNamespace(db=db), principal_id='p', session_id='s1', context={'db': board.db, **board.params},
                        checkpoint=checkpoint, request_id=target, owner_db=str(board.home / 'state.db'))


_MID_TOOL = [{'role': 'user', 'content': 'work kanban task'},
             {'role': 'assistant', 'content': None, 'tool_calls': [
                 {'id': 'call-1', 'type': 'function', 'function': {'name': 'terminal', 'arguments': '{}'}}]}]


def test_the_new_owner_answers_the_interrupted_tool_call_and_records_the_continuation(board, tmp_path, monkeypatch):
    from gateway.session_kanban_restart import record_restart_interruption
    record_restart_interruption(_policy(board), checkpoint={'session_id': 's1', 'request_id': board.request,
                                                            'admission_id': 'a1', 'cause': 'restart'})
    from gateway.session_kanban_continue import pending_admission
    _target, checkpoint = pending_admission(board.db, board.params, board.request)
    db = _session_db(tmp_path, [{'role': m['role'], 'content': m['content'], 'tool_calls': m.get('tool_calls')}
                                for m in _MID_TOOL])
    assert _prepare(board, db, checkpoint, monkeypatch)['answered'] == 1
    history = db.get_messages_as_conversation('s1')
    tool = history[-1]
    assert tool['role'] == 'tool' and tool['tool_call_id'] == 'call-1'
    import re
    assert re.fullmatch(r'\[interrupted by Hermes update at \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ: re-run if still needed\]',
                        tool['content'])
    with closing(__import__('sqlite3').connect(board.db)) as conn:
        kinds = [r[0] for r in conn.execute('SELECT kind FROM task_events WHERE task_id=?', (board.params['task_id'],))]
    assert kinds[-2:] == ['owner_admitted', 'restart_continued'], kinds
    # Proven continuations only once: the same checkpoint again is a retry, not a second continuation.
    assert pending_admission(board.db, board.params, board.request)[1] is None


@pytest.mark.parametrize('stopped, rewritten', [
    ('[Tool execution cancelled \u2014 terminal was abandoned: interrupt requested]', True),
    ('[Tool execution skipped \u2014 terminal was not started. Turn interrupted]', True),
    ('{"output": "[Command interrupted]", "exit_code": 130, "error": null}', True),
    ('{"output": "done", "exit_code": 0, "error": null}', False),  # a call that finished keeps its result
], ids=['cancelled', 'skipped', 'killed_command', 'finished'])
def test_the_stopping_agents_tool_result_becomes_the_update_text(board, tmp_path, monkeypatch, stopped, rewritten):
    # What a real restart Stop leaves: the agent answered the cut-off call itself, then closed its turn.
    from gateway.session_kanban_restart import record_restart_interruption
    from gateway.session_kanban_continue import pending_admission
    record_restart_interruption(_policy(board), checkpoint={'session_id': 's1', 'request_id': board.request,
                                                            'admission_id': 'a1', 'cause': 'restart'})
    _target, checkpoint = pending_admission(board.db, board.params, board.request)
    assert checkpoint is not None
    db = _session_db(tmp_path, [*[{'role': m['role'], 'content': m['content'], 'tool_calls': m.get('tool_calls')}
                                  for m in _MID_TOOL],
                                {'role': 'tool', 'content': stopped, 'tool_call_id': 'call-1'},
                                {'role': 'assistant', 'content': ''},
                                {'role': 'assistant', 'content': 'An automated delivery was interrupted.'}])
    assert _prepare(board, db, checkpoint, monkeypatch)['answered'] == int(rewritten)
    tools = [m for m in db.get_messages_as_conversation('s1') if m['role'] == 'tool']
    assert len(tools) == 1 and tools[0]['tool_call_id'] == 'call-1'  # rewritten in place, never a second row
    assert tools[0]['content'].startswith('[interrupted by Hermes update at ') is rewritten


def test_a_tool_result_before_a_later_user_turn_is_never_rewritten(board, tmp_path, monkeypatch):
    from gateway.session_kanban_restart import record_restart_interruption
    from gateway.session_kanban_continue import pending_admission
    record_restart_interruption(_policy(board), checkpoint={'session_id': 's1', 'request_id': board.request,
                                                            'admission_id': 'a1', 'cause': 'restart'})
    _target, checkpoint = pending_admission(board.db, board.params, board.request)
    assert checkpoint is not None
    old = '{"output": "[Command interrupted]", "exit_code": 130, "error": null}'
    db = _session_db(tmp_path, [*[{'role': m['role'], 'content': m['content'], 'tool_calls': m.get('tool_calls')}
                                  for m in _MID_TOOL],
                                {'role': 'tool', 'content': old, 'tool_call_id': 'call-1'},
                                {'role': 'assistant', 'content': 'stopped earlier'},
                                {'role': 'user', 'content': 'a later turn'}])
    assert _prepare(board, db, checkpoint, monkeypatch)['answered'] == 0
    assert [m['content'] for m in db.get_messages_as_conversation('s1') if m['role'] == 'tool'] == [old]


def test_a_retried_ask_racing_the_first_writes_one_interrupted_result(board, tmp_path, monkeypatch):
    import threading
    from gateway import session_kanban_continue as cont
    from gateway.session_kanban_restart import record_restart_interruption
    record_restart_interruption(_policy(board), checkpoint={'session_id': 's1', 'request_id': board.request,
                                                            'admission_id': 'a1', 'cause': 'restart'})
    _target, checkpoint = cont.pending_admission(board.db, board.params, board.request)
    assert checkpoint is not None
    db = _session_db(tmp_path, [{'role': m['role'], 'content': m['content'], 'tool_calls': m.get('tool_calls')}
                                for m in _MID_TOOL])
    real, both = cont._unanswered, threading.Barrier(2)

    def unanswered(history):
        calls = real(history)
        try:
            both.wait(timeout=1.5)  # both read the history before either writes, unless serialized
        except threading.BrokenBarrierError:
            pass
        return calls
    monkeypatch.setattr(cont, '_unanswered', unanswered)
    import hermes_state_runtime
    monkeypatch.setattr(hermes_state_runtime, 'find_session_admission',
                        lambda *a, **k: {'status': 'terminal', 'outcome': 'interrupted', 'admission_id': 'a1'})
    target = cont.continuation_request_id(board.request, checkpoint['checkpoint'])
    kwargs = dict(principal_id='p', session_id='s1', context={'db': board.db, **board.params}, checkpoint=checkpoint,
                  request_id=target, owner_db=str(board.home / 'state.db'))
    threads = [threading.Thread(target=cont.prepare, args=(SimpleNamespace(db=db),), kwargs=kwargs) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert [m['tool_call_id'] for m in db.get_messages_as_conversation('s1') if m['role'] == 'tool'] == ['call-1']


@pytest.mark.parametrize('case', ['session_mismatch', 'admission_not_interrupted', 'session_empty', 'attempt_superseded',
                                  'checkpoint_admission_mismatch', 'session_rotated'])
def test_an_unsafe_continuation_is_refused_and_booked(board, tmp_path, monkeypatch, case):
    from gateway import session_kanban_continue as cont
    from hermes_state_runtime import RuntimeStoreError
    from gateway.session_kanban_restart import record_restart_interruption
    record_restart_interruption(_policy(board), checkpoint={
        'session_id': 'other' if case == 'session_mismatch' else 's1',
        'request_id': 'kanban:foreign' if case == 'checkpoint_admission_mismatch' else board.request,
        'admission_id': 'a1', 'cause': 'restart'})
    _target, checkpoint = cont.pending_admission(board.db, board.params, board.request)
    assert checkpoint is not None
    # The superseded case leaves a tool call open: a refused continuation must not answer it.
    db = _session_db(tmp_path, [] if case == 'session_empty' else [
        {'role': m['role'], 'content': m['content'], 'tool_calls': m.get('tool_calls')} for m in _MID_TOOL])
    if case == 'session_rotated':
        db.end_session('s1', 'compression')  # a compression split moved the transcript to a child session
    admission = {'status': 'terminal', 'outcome': 'completed', 'admission_id': 'a1'} \
        if case == 'admission_not_interrupted' else None
    if case == 'attempt_superseded':
        with kbc.connect() as conn, kb.write_txn(conn):
            conn.execute('UPDATE tasks SET claim_lock=? WHERE id=?', ('newer:1', board.params['task_id']))
    with pytest.raises(RuntimeStoreError, match=cont.UNSAFE):
        _prepare(board, db, checkpoint, monkeypatch, admission)
    with closing(__import__('sqlite3').connect(board.db)) as conn:
        refused = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                               (board.params['task_id'], cont.REFUSED_EVENT)).fetchall()
        admitted = conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='owner_admitted'",
                                (board.params['task_id'],)).fetchone()[0]
    assert [json.loads(r[0])['reason'] for r in refused] == [case]
    assert admitted == 1  # nothing was admitted for the refused continuation
    # Nothing was written to the session either.
    assert all(m['role'] != 'tool' for m in db.get_messages_as_conversation('s1'))


def test_the_restart_safe_comment_describes_continuation():
    source = Path(kanban_worker_client.__file__).read_text(encoding='utf-8')
    head = source[source.index('# [daveey] The promise hermes-sync'):source.index('GATEWAY_RESTART_SAFE = True')]
    assert 'CONTINUES' in head and 'session_kanban_continue' in head
    assert kanban_worker_client.GATEWAY_RESTART_SAFE is True
