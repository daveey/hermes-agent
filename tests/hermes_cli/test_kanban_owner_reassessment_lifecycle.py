"""Typed owner reassessment creation in schedule/delayed continue.

Real private custody, real park transaction, real dispatcher wake path. Nothing here
claims canonical confirmation, scanner suppression or execution authority.
"""
import json
import time

import pytest

from hermes_cli import kanban_continuation as continuation
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_owner_reassessment as r
from hermes_cli.kanban_awaiting import AwaitingError
from tests.hermes_cli import test_origin_worker_custody as custody

worker = custody.worker
PRODUCER = {'kind': 'external', 'ref': 'executor:example', 'why': 'queue reassessment of the example executor'}


def _until(seconds=1800):
    """An ordinary admitted backstop (SCHEDULED_UNTIL) that passes the existing park gates."""
    stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + seconds))
    return f'waiting on example queue SCHEDULED_UNTIL={stamp}'


def _turn(worker, monkeypatch, body, *, admitted=True):
    """Run ``body(cap, context, agent, evidence_uid)`` inside a real facade turn."""
    from agent.origin_worker_custody import capture

    agent, _, _, context, _ = worker
    out = []

    def loop(*args, **kwargs):
        cap = capture(context['task_id'])
        assert cap is not None
        evidence = None
        with kbc.connect_closing() as conn, kb.write_txn(conn):
            if admitted:
                kb._append_event(conn, context['task_id'], 'owner_admitted',
                                 {'session_id': agent.session_id}, run_id=context['run_id'])
            evidence = conn.execute("SELECT uid FROM task_events WHERE task_id=? AND kind='worker_bound'",
                                    (context['task_id'],)).fetchone()[0]
        out.append(body(cap, context, agent, evidence))
        return dict(final_response='ok', messages=[], failed=False)

    monkeypatch.setattr('agent.conversation_loop.run_conversation', loop)
    agent.run_conversation('bounded owner reassessment lifecycle fixture')
    assert len(out) == 1
    return out[0]


def _opt(evidence, at):
    return {'version': 1, 'reassess_at': at,
            'target': {'kind': 'queue', 'identity': 'executor:example', 'revision': 'queue-cut:42'},
            'evidence_refs': [{'table': 'task_events', 'uid': evidence}]}


def _state(tid):
    with kbc.connect_closing() as conn:
        task = conn.execute('SELECT status,current_run_id,claim_lock FROM tasks WHERE id=?', (tid,)).fetchone()
        return (tuple(task), conn.execute('SELECT COUNT(*) FROM task_events').fetchone()[0],
                r.pending(conn))


def test_schedule_creates_exact_pending_receipt_and_shortens_wake(worker, monkeypatch):
    def body(cap, ctx, agent, evidence):
        now = int(time.time())
        at = now + 120
        with kbc.connect_closing() as conn:
            assert kb.schedule_task(conn, ctx['task_id'], reason=_until(),
                                    expected_run_id=ctx['run_id'], awaiting=PRODUCER,
                                    owner_reassessment=_opt(evidence, at), owner_origin=cap)
        return at, evidence

    at, evidence = _turn(worker, monkeypatch, body)
    tid = worker[3]['task_id']
    with kbc.connect_closing() as conn:
        (journal,) = r.pending(conn, tid)
        receipt = json.loads(journal['receipt'])
        event = conn.execute("SELECT uid,payload,created_at,run_id FROM task_events WHERE task_id=? AND kind='scheduled'",
                             (tid,)).fetchone()
        payload = json.loads(event[1])
        run = conn.execute('SELECT uid,status FROM task_runs WHERE id=?', (event[3],)).fetchone()
        task = conn.execute('SELECT status,claim_lock,claim_expires FROM tasks WHERE id=?', (tid,)).fetchone()
        assert tuple(task) == ('scheduled', None, None) and run[1] == 'scheduled'
        # Exact event payload is stored once, byte for byte, with the shortened wake.
        assert receipt['park_event'] == {'uid': event[0], 'run_uid': run[0], 'kind': 'scheduled',
                                         'created_at': event[2], 'payload': event[1]}
        assert journal['event_uid'] == event[0] and journal['episode_uid'] == receipt['episode_uid']
        assert payload['wake_at'] == at == receipt['reassess_at']
        assert payload['owner_reassessment_episode_uid'] == receipt['episode_uid']
        assert payload['awaiting']['kind'] == 'external'
        assert receipt['backstop_at'] > at
        assert r._uid(receipt['episode_uid']) and r._uid(event[0])
        assert receipt['issuer']['run_uid'] == run[0]
        assert set(receipt['issuer']) == {'session_id', 'profile', 'run_uid', 'owner_admitted_uid', 'worker_bound_uid'}
        assert receipt['version'] == 1 and receipt['suppresses'] == ['too_long']
        assert receipt['execution_authority'] is False
        assert receipt['gateway'] == 'here' and receipt['board'] == worker[3]['board']
        assert receipt['producer'] == {k: PRODUCER[k] for k in ('kind', 'ref', 'why')}
        assert receipt['target'] == _opt(evidence, at)['target']
        assert receipt['evidence_refs'] == _opt(evidence, at)['evidence_refs']
        assert receipt['issued_at'] <= at
        # The stored journal is immutable.
        with pytest.raises(Exception, match='immutable'):
            conn.execute('UPDATE kanban_owner_reassessment_pending SET receipt=?', ('{}',))


def test_delayed_continue_creates_receipt_with_continued_kind(worker, monkeypatch):
    def body(cap, ctx, agent, evidence):
        at = int(time.time()) + 200
        with kbc.connect_closing() as conn:
            res = continuation.continue_task(
                conn, ctx['task_id'], checkpoint='waiting on example queue', resume_after_seconds=1800,
                expected_run_id=ctx['run_id'], awaiting=PRODUCER,
                owner_reassessment=_opt(evidence, at), owner_origin=cap)
        return res, at

    res, at = _turn(worker, monkeypatch, body)
    assert res['ok'] and res['status'] == 'scheduled' and res['wake_at'] == at
    tid = worker[3]['task_id']
    with kbc.connect_closing() as conn:
        (journal,) = r.pending(conn, tid)
        receipt = json.loads(journal['receipt'])
        event = conn.execute("SELECT uid,payload FROM task_events WHERE task_id=? AND kind='continued'", (tid,)).fetchone()
        assert receipt['park_event']['kind'] == 'continued' and receipt['park_event']['payload'] == event[1]
        assert receipt['backstop_at'] > at == receipt['reassess_at'] == json.loads(event[1])['wake_at']
        assert json.loads(event[1])['owner_reassessment_episode_uid'] == receipt['episode_uid']


@pytest.mark.parametrize('case', ['no_origin', 'no_awaiting', 'beyond_backstop', 'past', 'foreign_evidence', 'long_ungated_park',
                                  'wrong_run', 'no_admission', 'bad_request', 'non_producer'])
def test_schedule_refuses_without_changing_anything(worker, monkeypatch, case):
    tid = worker[3]['task_id']
    before = _state(tid)

    def body(cap, ctx, agent, evidence):
        now = int(time.time())
        opt = _opt(evidence, now + 120)
        kwargs = dict(reason=_until(), expected_run_id=ctx['run_id'], awaiting=PRODUCER)
        origin = None if case == 'no_origin' else cap
        changes = {
            'no_origin': lambda: None,
            'no_awaiting': lambda: kwargs.update(awaiting=None),
            # 24h default with no probe: the ordinary gate still refuses.
            'long_ungated_park': lambda: kwargs.update(reason='no explicit deadline'),
            'beyond_backstop': lambda: opt.update(reassess_at=now + 10 ** 7),
            'past': lambda: opt.update(reassess_at=now - 5),
            'foreign_evidence': lambda: opt['evidence_refs'][0].update(uid='018f051a-657d-7001-8001-000000000001'),
            'wrong_run': lambda: kwargs.update(expected_run_id=ctx['run_id'] + 99),
            'bad_request': lambda: opt.update(issuer={'session_id': 'forged'}),
            'non_producer': lambda: kwargs.update(awaiting={'kind': 'clock', 'why': 'later'}),
            'no_admission': lambda: None,
        }
        changes[case]()
        with kbc.connect_closing() as conn:
            with pytest.raises((ValueError, AwaitingError)) as raised:
                kb.schedule_task(conn, ctx['task_id'], owner_reassessment=opt, owner_origin=origin, **kwargs)
            return raised.value

    error = _turn(worker, monkeypatch, body, admitted=case != 'no_admission')
    after = _state(tid)
    assert after[0] == before[0] and after[2] == [], (case, error)
    assert after[0][0] == 'running'
    if case not in ('no_awaiting', 'long_ungated_park'):  # those two stop at the unchanged ordinary gates
        assert isinstance(error, r.ReassessmentRefused), error


def test_immediate_or_unadmitted_continue_is_refused(worker, monkeypatch):
    def body(cap, ctx, agent, evidence):
        with kbc.connect_closing() as conn:
            immediate = continuation.continue_task(
                conn, ctx['task_id'], checkpoint='now', resume_after_seconds=0, expected_run_id=ctx['run_id'],
                awaiting=PRODUCER, owner_reassessment=_opt(evidence, int(time.time()) + 50), owner_origin=cap)
            no_origin = continuation.continue_task(
                conn, ctx['task_id'], checkpoint='later', resume_after_seconds=1800, expected_run_id=ctx['run_id'],
                awaiting=PRODUCER, owner_reassessment=_opt(evidence, int(time.time()) + 50), owner_origin=None)
        return immediate, no_origin

    tid = worker[3]['task_id']
    before = _state(tid)
    immediate, no_origin = _turn(worker, monkeypatch, body)
    for res in (immediate, no_origin):
        assert res['ok'] is False and res['reason_code'] == 'owner_reassessment_refused'
    after = _state(tid)
    assert after[0] == before[0] and after[2] == []


def test_ordinary_park_is_unchanged_and_mints_nothing(worker, monkeypatch):
    def body(cap, ctx, agent, evidence):
        with kbc.connect_closing() as conn:
            assert kb.schedule_task(conn, ctx['task_id'], reason=_until(), expected_run_id=ctx['run_id'],
                                    awaiting=PRODUCER)

    _turn(worker, monkeypatch, body)
    with kbc.connect_closing() as conn:
        assert r.pending(conn) == []
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (r.PENDING_TABLE,)).fetchone() is None
        payload = conn.execute("SELECT payload FROM task_events WHERE kind='scheduled'").fetchone()[0]
        assert 'owner_reassessment' not in payload


def test_error_after_receipt_rolls_everything_back(worker, monkeypatch):
    def body(cap, ctx, agent, evidence):
        def boom(*args, **kwargs):
            raise RuntimeError('late failure')
        monkeypatch.setattr('hermes_cli.kanban_done_tail.post_notice', boom)
        with kbc.connect_closing() as conn:
            with pytest.raises(RuntimeError):
                kb.schedule_task(conn, ctx['task_id'], reason=_until(), expected_run_id=ctx['run_id'], awaiting=PRODUCER,
                                 owner_reassessment=_opt(evidence, int(time.time()) + 90), owner_origin=cap)

    tid = worker[3]['task_id']
    before = _state(tid)
    _turn(worker, monkeypatch, body)
    after = _state(tid)
    assert after[0] == before[0] and after[2] == []
    with kbc.connect_closing() as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (r.PENDING_TABLE,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM task_events WHERE kind='scheduled'").fetchone() is None


def test_tool_ingress_captures_private_origin_and_ignores_caller_issuer(worker, monkeypatch):
    from tools import kanban_tools

    def body(cap, ctx, agent, evidence):
        monkeypatch.setenv('HERMES_KANBAN_TASK', ctx['task_id'])
        monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(ctx['run_id']))
        forged = _opt(evidence, int(time.time()) + 120)
        forged['issuer'] = {'session_id': 'forged'}
        refused = json.loads(kanban_tools._handle_schedule(
            {'reason': _until(), 'awaiting': PRODUCER, 'owner_reassessment': forged}))
        with kbc.connect_closing() as conn:
            assert r.pending(conn) == []
        ok = json.loads(kanban_tools._handle_schedule(
            {'reason': _until(), 'awaiting': PRODUCER,
             'owner_reassessment': _opt(evidence, int(time.time()) + 120)}))
        return refused, ok

    refused, ok = _turn(worker, monkeypatch, body)
    assert 'error' in refused and ok.get('ok') is True, (refused, ok)
    with kbc.connect_closing() as conn:
        assert len(r.pending(conn)) == 1


def test_cold_dispatcher_wake_uses_shortened_deadline_and_missed_expiry_is_actionable(worker, monkeypatch):
    def body(cap, ctx, agent, evidence):
        at = int(time.time()) + 120
        with kbc.connect_closing() as conn:
            kb.schedule_task(conn, ctx['task_id'], reason=_until(), expected_run_id=ctx['run_id'],
                             awaiting=PRODUCER, owner_reassessment=_opt(evidence, at), owner_origin=cap)
        return at

    at = _turn(worker, monkeypatch, body)
    tid = worker[3]['task_id']
    # A cold connection, as after a dispatcher restart: no in-memory state from the park.
    with kbc.connect_closing() as conn:
        receipt = json.loads(r.pending(conn, tid)[0]['receipt'])
        park = continuation.pending_wake(conn, tid)
        assert park['wake_at'] == at < receipt['backstop_at']
        assert continuation.wake_due_continuations(conn, now=at - 1) == []
        assert conn.execute('SELECT status FROM tasks WHERE id=?', (tid,)).fetchone()[0] == 'scheduled'
        # Restart after the deadline was missed: the existing wake path still returns the card to work.
        assert continuation.wake_due_continuations(conn, now=at + 1800) == [tid]
        assert conn.execute('SELECT status FROM tasks WHERE id=?', (tid,)).fetchone()[0] in ('ready', 'review')
        assert continuation.wake_due_continuations(conn, now=at + 1801) == []
        # The wake neither extended the park nor altered the immutable receipt.
        assert json.loads(r.pending(conn, tid)[0]['receipt']) == receipt
