"""Real local admitted administrative actor protects an ended legacy target only."""
import json
import time

import pytest

from gateway.session_context import scoped_current_session_id
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_spend_decision as decision
from tests.hermes_cli import test_kanban_containment_actor as support

board = support.board


def fixture(conn, monkeypatch):
    actor, rid, session = support.bind(conn, monkeypatch)
    target = kb.create_task(conn, title='legacy target fixture', assignee='default')
    assert kb.claim_task(conn, target, claimer='default')
    old = kb.get_task(conn, target).current_run_id
    kb._end_run(conn, target, outcome='crashed', status='crashed')
    conn.execute("UPDATE tasks SET status='scheduled',current_run_id=NULL,claim_lock=NULL,claim_expires=NULL WHERE id=?", (target,))
    kb.link_tasks(conn, actor, target)
    from hermes_cli.kanban_containment_connections import bootstrap
    bootstrap(conn)  # assign local source-run identity before recording source
    kb._append_event(conn, target, 'failure_finalized', {'run_uid': conn.execute('SELECT uid FROM task_runs WHERE id=?', (old,)).fetchone()[0],
                     'reason_code': 'provider_refusal', 'tripped': True, 'final_status': 'triage', 'trigger_outcome': 'crashed'}, run_id=old)
    from hermes_cli.kanban_containment_connections import bootstrap
    bootstrap(conn)  # supported local-only UID bootstrap, never portable provenance
    source = conn.execute("SELECT uid FROM task_events WHERE task_id=? AND kind='failure_finalized'", (target,)).fetchone()[0]

    args = dict(task_id=target, source_event_uid=source,
                source_run_uid=conn.execute('SELECT uid FROM task_runs WHERE id=?', (old,)).fetchone()[0],
                expected_status='scheduled', expected_current_run_uid=None, expected_updated_at=None, expected_episode=None,
                requested_owner=actor, next_step='Resolve supported administrative authority; no original work',
                recovery_trigger='An explicit supported scope decision', escalate_at=int(time.time())+86400,
                evidence=['fixture legacy source; original provider provenance unattested'])
    return actor, rid, session, target, old, args


def test_local_adoption_stops_only_target_without_fabricating_provider_authority(board, monkeypatch):
    from hermes_cli import kanban_protect_stopped as protect
    actor, rid, session, target, old, args = fixture(board, monkeypatch)
    edges = [tuple(r) for r in board.execute('SELECT * FROM task_links')]
    run = dict(board.execute('SELECT * FROM task_runs WHERE id=?', (old,)).fetchone())
    with scoped_current_session_id(session):
        result = protect.protect(board, board='default', **args)
        replay = protect.protect(board, board='default', **args)
    assert result == replay
    assert result['terminal_for_run'] is False and result['dependency_satisfied'] is False
    assert kb.get_task(board, actor).status == 'running'
    assert kb.get_task(board, actor).current_run_id == rid
    assert kb.get_task(board, target).status == 'triage'
    assert kb.get_task(board, target).current_run_id is None
    assert dict(board.execute('SELECT * FROM task_runs WHERE id=?', (old,)).fetchone()) == run
    assert [tuple(r) for r in board.execute('SELECT * FROM task_links')] == edges
    assert decision.execution_restricted(board, target)
    assert not kb.promote_task(board, target, actor='fixture', reason='comment not release')[0]
    events = board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='decision_protected_stopped'", (target,)).fetchall()
    assert len(events) == 1
    assert json.loads(events[0][0])['proof_class'] == 'administrative_adoption;historical_provider_unattested'


@pytest.mark.parametrize('fault', ['scope', 'source', 'active', 'cas', 'self', 'owner'])
def test_local_adoption_refuses_without_mutating_target_or_ending_actor(board, monkeypatch, fault):
    from hermes_cli import kanban_protect_stopped as protect
    actor, rid, session, target, old, args = fixture(board, monkeypatch)
    if fault == 'scope':
        kb.unlink_tasks(board, actor, target)
    if fault == 'source':
        board.execute("UPDATE task_events SET payload='{}' WHERE uid=?", (args['source_event_uid'],))
    if fault == 'active':
        board.execute('UPDATE task_runs SET ended_at=NULL WHERE id=?', (old,))
    if fault == 'cas':
        args['expected_status'] = 'ready'
    if fault == 'self':
        args['task_id'] = actor
    if fault == 'owner':
        args['requested_owner'] = target
    before = dict(board.execute('SELECT * FROM tasks WHERE id=?', (target,)).fetchone())
    reason = {'scope': 'protect_request_scope', 'source': 'protect_source_unconfirmed',
              'active': 'protect_target_changed', 'cas': 'protect_target_changed',
              'self': 'protect_request_scope', 'owner': 'protect_request_scope'}[fault]
    with scoped_current_session_id(session), pytest.raises(ValueError, match='^'+reason+'$'):
        protect.protect(board, board='default', **args)
    assert dict(board.execute('SELECT * FROM tasks WHERE id=?', (target,)).fetchone()) == before
    assert kb.get_task(board, actor).current_run_id == rid


def test_archived_protected_parent_never_releases_child(board, monkeypatch):
    from hermes_cli import kanban_protect_stopped as protect
    _, _, session, target, _, args = fixture(board, monkeypatch)
    child = kb.create_task(board, title='held verifier fixture', assignee='default', parents=[target])
    with scoped_current_session_id(session):
        protect.protect(board, board='default', **args)
    assert kb.archive_task(board, target)
    kb.recompute_ready(board)
    assert kb.claim_task(board, child, claimer='default') is None
    assert kb.get_task(board, child).status == 'todo'


@pytest.mark.parametrize('mutation', ['unlink', 'delete', 'source', 'run', 'receipt'])
def test_local_protection_preserves_edges_and_exact_evidence(board, monkeypatch, mutation):
    import sqlite3
    from hermes_cli import kanban_protect_stopped as protect
    _, _, session, target, old, args = fixture(board, monkeypatch)
    child = kb.create_task(board, title='held verifier fixture', assignee='default', parents=[target])
    with scoped_current_session_id(session):
        result = protect.protect(board, board='default', **args)
    actions = {
        'unlink': lambda: kb.unlink_tasks(board, target, child),
        'delete': lambda: kb.delete_task(board, target),
        'source': lambda: board.execute('DELETE FROM task_events WHERE uid=?', (args['source_event_uid'],)),
        'run': lambda: board.execute('UPDATE task_runs SET ended_at=NULL WHERE id=?', (old,)),
        'receipt': lambda: board.execute('DELETE FROM task_events WHERE uid=?', (result['event_uid'],)),
    }
    with pytest.raises(sqlite3.IntegrityError, match='protect_evidence_immutable'):
        actions[mutation]()
    assert kb.get_task(board, child).status == 'todo'


@pytest.mark.parametrize('conflict', [None, 'notice_owner', 'notice_unavailable', 'role'])
def test_exact_latest_decision_owner_without_direct_parent(board, monkeypatch, conflict):
    from hermes_cli import kanban_protect_stopped as protect
    actor, _, session, target, _, args = fixture(board, monkeypatch)
    kb.unlink_tasks(board, actor, target)
    kb._append_event(board, target, decision.EVENT, {'cause': 'budget', 'accounting_boundary': False})
    from hermes_cli.kanban_containment_connections import bootstrap
    bootstrap(board)
    stop = decision.pending_episode(board, target)
    assert stop is not None
    episode = stop['uid']
    kb._append_event(board, actor, 'administrative_decision', {'role': 'administrative_decision', 'root_task': target, 'root_episode': episode})
    kb._append_event(board, target, decision.NOTICE, {'episode': episode, 'producer': {'kind': 'supervisor', 'task_id': actor}})
    if conflict == 'role':
        kb._append_event(board, actor, 'administrative_decision', {'role': 'administrative_decision', 'root_task': 'wrong', 'root_episode': episode})
    elif conflict:
        producer = {'kind': 'supervisor', 'task_id': 'wrong'} if conflict == 'notice_owner' else {'kind': 'unavailable'}
        kb._append_event(board, target, decision.NOTICE, {'episode': episode, 'producer': producer})
    args['expected_episode'] = episode
    with scoped_current_session_id(session):
        if conflict:
            with pytest.raises(ValueError, match='^protect_request_scope$'):
                protect.protect(board, board='default', **args)
        else:
            assert protect.protect(board, board='default', **args)['ok']


def add_target(conn, actor, args):
    """A second ended legacy target under the same actor, same shape as `fixture`."""
    from hermes_cli.kanban_containment_connections import bootstrap
    target = kb.create_task(conn, title='second legacy target fixture', assignee='default')
    assert kb.claim_task(conn, target, claimer='default')
    old = kb.get_task(conn, target).current_run_id
    kb._end_run(conn, target, outcome='crashed', status='crashed')
    conn.execute("UPDATE tasks SET status='scheduled',current_run_id=NULL,claim_lock=NULL,claim_expires=NULL WHERE id=?", (target,))
    kb.link_tasks(conn, actor, target)
    bootstrap(conn)
    run_uid = conn.execute('SELECT uid FROM task_runs WHERE id=?', (old,)).fetchone()[0]
    kb._append_event(conn, target, 'failure_finalized', {'run_uid': run_uid, 'reason_code': 'provider_refusal', 'tripped': True,
                     'final_status': 'triage', 'trigger_outcome': 'crashed'}, run_id=old)
    bootstrap(conn)
    source = conn.execute("SELECT uid FROM task_events WHERE task_id=? AND kind='failure_finalized'", (target,)).fetchone()[0]
    return target, old, {**args, 'task_id': target, 'source_event_uid': source, 'source_run_uid': run_uid}


@pytest.mark.parametrize('mutation', ['move_id', 'replace_episode', 'replace_receipt', 'change_origin'])
def test_uid_bootstrap_does_not_allow_evidence_row_escape_or_replace(board, monkeypatch, mutation):
    import sqlite3
    from hermes_cli import kanban_protect_stopped as protect
    _, _, session, target, _, args = fixture(board, monkeypatch)
    with scoped_current_session_id(session):
        result = protect.protect(board, board='default', **args)
    episode = dict(board.execute("SELECT * FROM task_events WHERE task_id=? AND kind=?", (target, decision.EVENT)).fetchone())
    assert episode['uid'] is None
    if mutation == 'move_id':
        sql, params = "UPDATE task_events SET uid='escape',id=id+100000 WHERE id=?", (episode['id'],)
    elif mutation == 'change_origin':
        # Legacy unsynced schemas lack this optional column; the guarded bootstrap
        # must pin it too whenever a board later acquires it.
        if 'origin_gateway' not in {r[1] for r in board.execute('PRAGMA table_info(task_events)')}:
            board.execute('ALTER TABLE task_events ADD COLUMN origin_gateway TEXT')
        from hermes_cli import kanban_protect_guards
        kanban_protect_guards.install(board)
        sql, params = "UPDATE task_events SET uid='escape',origin_gateway='foreign' WHERE id=?", (episode['id'],)
    else:
        row = episode if mutation == 'replace_episode' else dict(board.execute('SELECT * FROM task_events WHERE uid=?', (result['event_uid'],)).fetchone())
        columns = list(row)
        row['kind'], row['payload'] = 'note', '{}'
        sql = f"INSERT OR REPLACE INTO task_events ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})"
        params = tuple(row[c] for c in columns)
    with pytest.raises(sqlite3.IntegrityError, match='protect_evidence_immutable'):
        board.execute(sql, params)
    assert decision.execution_restricted(board, target)


def test_two_protections_on_one_unsynced_board(board, monkeypatch):
    from hermes_cli import kanban_protect_stopped as protect
    from hermes_cli.kanban_containment_connections import bootstrap
    actor, _, session, first, _, args = fixture(board, monkeypatch)
    second, _, args2 = add_target(board, actor, args)
    with scoped_current_session_id(session):
        one = protect.protect(board, board='default', **args)
        two = protect.protect(board, board='default', **args2)
        assert protect.protect(board, board='default', **args2) == two
    assert one['event_uid'] != two['event_uid']
    assert kb.get_task(board, first).status == kb.get_task(board, second).status == 'triage'
    assert decision.execution_restricted(board, first) and decision.execution_restricted(board, second)
    bootstrap(board)


def test_existing_episode_protection_keeps_uid_bootstrap_and_containment_usable(board, monkeypatch):
    import sqlite3
    from hermes_cli import kanban_protect_stopped as protect
    from hermes_cli.kanban_containment_connections import bootstrap
    _, _, session, target, _, args = fixture(board, monkeypatch)
    kb._append_event(board, target, decision.EVENT, {'cause': 'budget', 'accounting_boundary': False})
    bootstrap(board)
    args['expected_episode'] = decision.pending_episode(board, target)['uid']
    # An unrelated unbootstrapped run/event on the board must still receive its uid after protection.
    with scoped_current_session_id(session):
        protect.protect(board, board='default', **args)
    kb._append_event(board, target, 'diagnostic_after', {})
    bootstrap(board)
    bootstrap(board)
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE uid IS NULL").fetchone()[0] == 0
    row = board.execute("SELECT id FROM task_events WHERE task_id=? AND kind='decision_hold_restricted'", (target,)).fetchone()
    assert row is not None
    with pytest.raises(sqlite3.IntegrityError, match='protect_evidence_immutable'):
        board.execute('DELETE FROM task_events WHERE id=?', (row[0],))
    with pytest.raises(sqlite3.IntegrityError, match='protect_evidence_immutable'):
        board.execute("UPDATE task_events SET payload='{}' WHERE id=?", (row[0],))
    episode = board.execute("SELECT id FROM task_events WHERE task_id=? AND kind=?", (target, decision.EVENT)).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match='protect_evidence_immutable'):
        board.execute('DELETE FROM task_events WHERE id=?', (episode,))
    assert decision.execution_restricted(board, target)


@pytest.mark.parametrize('bootstrapped', [False, True])
def test_default_path_restriction_episode_is_fenced_by_row_and_child_stays_held(board, monkeypatch, bootstrapped):
    import sqlite3
    from hermes_cli import kanban_protect_stopped as protect
    from hermes_cli.kanban_containment_connections import bootstrap
    _, _, session, target, _, args = fixture(board, monkeypatch)
    child = kb.create_task(board, title='held verifier fixture', assignee='default', parents=[target])
    with scoped_current_session_id(session):
        result = protect.protect(board, board='default', **args)
    if bootstrapped:
        bootstrap(board)
    assert kb.archive_task(board, target)
    row = board.execute("SELECT id FROM task_events WHERE task_id=? AND kind=?", (target, decision.EVENT)).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match='protect_evidence_immutable'):
        board.execute('DELETE FROM task_events WHERE id=?', (row,))
    kb.recompute_ready(board)
    assert kb.get_task(board, child).status == 'todo'
    assert decision.execution_restricted(board, target) and result['dependency_satisfied'] is False
