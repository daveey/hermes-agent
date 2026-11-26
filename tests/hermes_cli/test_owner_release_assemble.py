"""Actual HOME atomic effects; transport adapter is not the composed Board proof."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, kanban_db_sync as sync
from hermes_cli import kanban_release_certificate as cert, kanban_release_replica as replica
from tests.hermes_cli import test_kanban_scope_renewal as helpers
from tests.hermes_cli import test_owner_release_certificate as certificate_helpers

board = helpers.board
proof = certificate_helpers.proof


@pytest.fixture
def assembly(board, proof, request):
    from hermes_cli import kanban_release_application as app, kanban_release_home_queue as queue
    sync.apply_sync_schema_steps(board)
    board.commit()
    board.execute("INSERT OR REPLACE INTO kanban_sync_state(board_id,gateway_id,hlc_last,mode) VALUES ('example','home','','primary')")
    board.commit()
    sync.set_identity(board, 'example', 'home')
    sync.set_mode(board, 'primary')
    tid = kb.create_task(board, title='Original goal', assignee='default')
    receipt = copy.deepcopy(proof)
    options = dict(getattr(request, 'param', {}))
    extra_cover = options.pop('_extra_cover', False)
    receipt['intent'].update(options)
    receipt['intent']['task_id'] = tid
    receipt['consume']['task'] = tid
    receipt['renewal']['task_id'] = tid
    from hermes_cli import kanban_release_intent as intent
    receipt['digest'] = intent.digest(receipt['intent'])
    receipt['consume']['digest'] = receipt['digest']
    payload = json.loads(receipt['renewal']['payload'])
    payload['owner_release_digest'] = receipt['digest']
    if extra_cover:
        payload['covered_restriction_uids'].append('unrelated-scope')
        payload['covered_restriction_uids'].sort()
    receipt['renewal']['payload'] = json.dumps(payload)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='triage',created_on='home' WHERE id=?", (tid,))
        board.execute('DELETE FROM task_chat_links WHERE task_id=?', (tid,))
        board.execute("INSERT INTO task_chat_links(uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at) VALUES ('link-1',?,'origin','default','chat-1','created',0,1)", (tid,))
        for uid, kind, p in [('older-1','spend_budget_triaged',{'cause':'capability_unavailable'}),
                             ('stop-1','spend_budget_triaged',{'cause':'scope_stopped'}),
                             ('restriction-1','decision_hold_restricted',{'cause':'scope_stopped','episode':'stop-1'})]:
            board.execute('INSERT INTO task_events(uid,task_id,kind,payload,created_at,origin_gateway) VALUES (?,?,?,?,?,?)',
                          (uid,tid,kind,json.dumps(p),90,'home'))
        if extra_cover:
            board.execute("INSERT INTO task_events(uid,task_id,kind,payload,created_at,origin_gateway) VALUES ('unrelated-scope',?,'decision_hold_restricted',?,90,'home')", (tid,json.dumps({'cause':'scope_stopped','episode':'other-episode'})))
        e = receipt['renewal']
        board.execute('INSERT INTO task_events(uid,task_id,kind,payload,created_at,origin_gateway) VALUES (?,?,?,?,?,?)',
                      tuple(e[k] for k in ('uid','task_id','kind','payload','created_at','origin_gateway')))
    replica._store(board, receipt)
    app.install(board)
    queue.install(board)
    p = dict(version=1, carrier='owner_release', action='owner_release',request_uid='delivery-1',board='example',
             target_task_id=tid,home='home',intent=receipt['intent'],digest=receipt['digest'],seen_comments=[],nonce='a'*64)
    raw = cert.encode(p)
    with kb.write_txn(board):
        board.execute(f'INSERT INTO {queue.TABLE} VALUES (?,?,?,?,?,?,?,?,?,?)',
                      ('delivery-1','example',tid,raw,99,'intent-1','consumed_pending_apply',cert.encode(receipt['consume']),intent.digest(receipt['consume']),0))
        board.execute("INSERT INTO task_events(uid,task_id,kind,payload,created_at,origin_gateway) VALUES ('delivery-1',?,'operator_call_requested',?,99,'origin')", (tid,raw))
    return SimpleNamespace(conn=board, tid=tid, proof=receipt)


def apply(a):
    from hermes_cli import kanban_release_assemble as subject
    return subject.apply(a.conn, a.proof, delivery_uid='delivery-1')


def snapshot(a):
    return {name:[tuple(row) for row in a.conn.execute(f'SELECT * FROM {name} ORDER BY rowid')]
            for name in ('tasks','task_comments','task_events','owner_release_applied_receipts')}


def test_actual_application_commits_effects_and_replays_before_new_episode(assembly):
    from hermes_cli import kanban_release_application as app, kanban_spend_decision as decision
    a = assembly
    before_floor = decision.restricted_floor(a.conn, a.tid)
    result = apply(a)
    assert result['result']['outcome'] == 'released'
    assert kb.get_task(a.conn,a.tid).status == 'ready'
    assert not decision.execution_restricted(a.conn,a.tid)
    assert decision.restricted_floor(a.conn,a.tid) == before_floor
    assert app.verified_application(a.conn,'intent-1') is not None
    with kb.write_txn(a.conn):
        a.conn.execute("UPDATE tasks SET status='triage' WHERE id=?",(a.tid,))
        kb._append_event(a.conn,a.tid,'spend_budget_triaged',{'cause':'scope_stopped'})
    before = snapshot(a)
    assert apply(a) == result
    assert snapshot(a) == before
    assert decision.execution_restricted(a.conn,a.tid)


@pytest.mark.parametrize('damage', ['owner','ambiguous','owner_tuple','current','claim','run','lease','human','safety','admin','parent','comment','unselected','other_scope'])
def test_live_vetoes_preserve_every_effect(assembly,damage):
    a=assembly
    changes = {
        'owner_tuple': lambda: a.conn.execute("UPDATE task_chat_links SET chat_session='another' WHERE uid='link-1'"),
        'owner': lambda: a.conn.execute("UPDATE task_chat_links SET released=1 WHERE uid='link-1'"),
        'ambiguous': lambda: a.conn.execute("INSERT INTO task_chat_links(uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at) VALUES ('link-2',?,'other','default','other-chat','created',0,1)",(a.tid,)),
        'comment': lambda: kb.add_comment(a.conn,a.tid,'another','New instruction',system=True),
        'parent': lambda: a.conn.execute('INSERT INTO task_links VALUES (?,?)',
            (kb.create_task(a.conn,title='Open dependency',assignee='default'),a.tid)),
    }
    for name, (column, value) in {'claim':('claim_lock','locked'), 'run':('current_run_id',123),
                                 'lease':('lease_holder','other'), 'human':('human_request','{}')}.items():
        changes[name] = lambda col=column, val=value: a.conn.execute(f'UPDATE tasks SET {col}=? WHERE id=?',(val,a.tid))
    events = {
        'current':('spend_budget_triaged',{'cause':'scope_stopped'}),
        'safety':('decision_hold_restricted',{'cause':'safety_refusal','episode':'stop-1'}),
        'admin':('administrative_stop_restricted',{'episode':'admin-stop'}),
        'unselected':('spend_budget_triaged',{'cause':'capability_unavailable'}),
        'other_scope':('decision_hold_restricted',{'cause':'scope_stopped','episode':'different-stop'}),
    }
    for name, (kind, payload) in events.items():
        changes[name] = lambda k=kind, p=payload: kb._append_event(a.conn,a.tid,k,p)
    with kb.write_txn(a.conn):
        changes[damage]()
    before=snapshot(a)
    with pytest.raises(ValueError):
        apply(a)
    assert snapshot(a)==before


def test_late_record_failure_rolls_back_budget_release_promotion_and_comments(assembly):
    a=assembly
    a.conn.execute("CREATE TRIGGER fail_receipt AFTER INSERT ON owner_release_applied_receipts BEGIN SELECT RAISE(ABORT,'late receipt failure'); END")
    before=snapshot(a)
    import sqlite3
    with pytest.raises(sqlite3.DatabaseError,match='late receipt failure'):
        apply(a)
    assert snapshot(a)==before


def test_consumed_certificate_alone_cannot_enable_public_promotion(assembly):
    a=assembly
    assert kb.promote_task(a.conn,a.tid,actor='unqualified')[0] is False
    assert kb.get_task(a.conn,a.tid).status == 'triage'


@pytest.mark.parametrize('assembly', [{'capability_episodes': []}, {'capability_episodes': ['missing']}, {'_extra_cover': True}], indirect=True)
def test_omitted_or_invalid_historical_selection_never_releases(assembly):
    a = assembly
    before = snapshot(a)
    with pytest.raises(ValueError):
        apply(a)
    assert snapshot(a) == before


@pytest.mark.parametrize('assembly', [{'_extra_cover': True}], indirect=True)
def test_scope_projection_rejects_extra_certificate_identity_at_its_boundary(assembly):
    # Full apply also refuses downstream private history; isolate this contract so
    # that independent veto does not mask the exact scope-projection clause.
    from hermes_cli import kanban_release_assemble as subject
    with pytest.raises(ValueError, match='current scope evidence differs'):
        subject._scope(assembly.conn, assembly.proof)


def test_admission_is_writer_connection_and_task_bound(assembly):
    from hermes_cli import kanban_release_application as app, kanban_release_assemble as subject
    a = assembly
    with pytest.raises(ValueError, match='application writer'):
        subject._Admission(a.conn, a.proof, 'delivery-1')
    with app._writer(a.conn):
        admission = subject._Admission(a.conn, a.proof, 'delivery-1')
        assert subject._admission_refusal(a.conn, a.tid, admission) is None
        assert subject._admission_refusal(a.conn, 'another-task', admission)
        import sqlite3
        with sqlite3.connect(':memory:') as other:
            assert subject._admission_refusal(other, a.tid, admission)
        assert subject._admission_refusal(a.conn, a.tid, object())
    assert subject._admission_refusal(a.conn, a.tid, admission)
    with app._writer(a.conn):
        assert subject._admission_refusal(a.conn, a.tid, admission)


@pytest.mark.parametrize('kind,payload', [
    ('decision_hold_restricted', {'cause': 'scope_stopped', 'episode': 'stop-1'}),
    ('decision_hold_restricted', {'cause': 'safety_refusal', 'episode': 'stop-1'}),
    ('administrative_stop_restricted', {'episode': 'admin-stop'}),
])
def test_new_restriction_after_admission_is_not_covered(assembly, kind, payload):
    from hermes_cli import kanban_release_application as app, kanban_release_assemble as subject
    a = assembly
    before = snapshot(a)
    with pytest.raises(ValueError, match='rollback'):
        with app._writer(a.conn):
            admission = subject._Admission(a.conn, a.proof, 'delivery-1')
            kb._append_event(a.conn, a.tid, kind, payload)
            try:
                refused = subject._admission_refusal(a.conn, a.tid, admission)
            except ValueError:
                refused = True
            assert refused
            raise ValueError('rollback')
    assert snapshot(a) == before


def test_two_appliers_recheck_exact_replay_inside_writer(assembly, monkeypatch):
    from contextlib import contextmanager
    from hermes_cli import kanban_release_application as app
    a = assembly
    original = app._writer
    result = []
    @contextmanager
    def interleaved(conn):
        monkeypatch.setattr(app, '_writer', original)
        result.append(apply(a))  # the other applier wins just before our BEGIN
        with original(conn):
            yield
    monkeypatch.setattr(app, '_writer', interleaved)
    try:
        replay = apply(a)
    except ValueError as exc:
        raise AssertionError('concurrent exact replay must return its historical winner') from exc
    assert replay == result[0]
    assert a.conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='owner_release_applied'").fetchone()[0] == 1
