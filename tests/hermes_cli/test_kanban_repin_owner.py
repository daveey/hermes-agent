"""Orphaned repin waits get one locally runnable supervisor, never an automatic repin."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worker_placement as wp
from tests.hermes_cli import test_kanban_repin_notice as notices

conn = notices.conn
_no_pull = notices._no_pull


def supervisors(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM tasks WHERE created_by='owner-wake'")]


def test_unowned_wait_creates_one_local_supervisor(conn):
    tid = notices._card(conn)
    hub = notices._hub()
    for _ in range(4):
        wp.reevaluate(conn, hub, "default", "hub", cfg=notices.CFG)
    owners = supervisors(conn)
    assert len(owners) == 1
    owner = owners[0]
    assert owner['title'].startswith('Owner wake: supervise ')
    assert f'- default/{tid}' in owner['body']
    assert owner['status'] == 'ready'
    assert owner['assignee'] == 'default'
    assert owner['placement_policy'] is None
    assert json.loads(owner['runs_on']) == ['machine:hub']
    assert wp.execution_refusal(conn, owner) is None
    assert len(notices._notice_comments(conn, tid)) == 1
    original = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
    assert original['placement_policy'] == wp.POLICY
    assert original['runs_on'] is None
    hub.repin.assert_not_called()


def test_live_chat_blocks_recovery_then_disappearance_is_rechecked(conn):
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB
    db = SessionDB(db_path=get_hermes_home() / 'state.db')
    try:
        db.create_session('owner-chat', source='desktop')
        tid = notices._card(conn)
        notices._subscribe(conn, tid, 'owner-chat')
        wp.reevaluate(conn, notices._hub(), 'default', 'hub', cfg=notices.CFG)
        assert supervisors(conn) == []
        db.end_session('owner-chat', end_reason='reaped')
        wp.reevaluate(conn, notices._hub(), 'default', 'hub', cfg=notices.CFG)
        assert len(supervisors(conn)) == 1
        assert len(notices._notice_comments(conn, tid)) == 1
    finally:
        db.close()


def test_open_creator_is_owner(conn):
    creator = kb.create_task(conn, title='Creator', assignee='default')
    tid = notices._card(conn)
    with kb.write_txn(conn):
        conn.execute("UPDATE task_events SET payload=json_set(payload, '$.creator_task_id', ?) "
                     "WHERE task_id=? AND kind='created'", (creator, tid))
    wp.reevaluate(conn, notices._hub(), 'default', 'hub', cfg=notices.CFG)
    assert supervisors(conn) == []


def test_supervisor_body_is_recognized_by_core_requeue(conn):
    from hermes_cli.kanban_requeue import owner_wake_listed_refs
    tid = notices._card(conn)
    wp.reevaluate(conn, notices._hub(), 'default', 'hub', cfg=notices.CFG)
    owner = supervisors(conn)[0]
    assert owner_wake_listed_refs(kb.get_task(conn, owner['id'])) == [('default', tid)]


def test_unowned_preclaim_and_stuck_summary_name_supervisor(conn, monkeypatch):
    from hermes_cli import kanban_worker_node
    monkeypatch.setattr(kanban_worker_node, 'registry', lambda: None)
    from hermes_cli.kanban_db_dispatch import DispatchResult
    from hermes_cli.kanban_db_dispatch_preclaim import eligible_rows
    from hermes_cli.kanban_db_dispatch_stuck import stuck_warning, describe_suppression
    tid = notices._card(conn)
    wp.reevaluate(conn, notices._hub(), 'default', 'hub', cfg=notices.CFG)
    owner = supervisors(conn)[0]['id']
    result = DispatchResult()
    rows = conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchall()
    assert eligible_rows(conn, rows, 'ready', result) == []
    assert result.skipped_ineligible == [(tid, f'waiting_for_manual_repin:no_owner:{owner}:ready')]
    line = stuck_warning(6, [result])
    assert f'Waiting for repin supervision: {tid} (no owner; supervisor {owner} (ready))' in line
    assert f'waiting for a manual repin: {tid}' not in line  # orphaned, not an owned manual wait
    assert 'Board lease refused or deferred' not in line
    assert 'skipped_ineligible' not in describe_suppression([result])


@pytest.mark.parametrize('closed', ['done', 'archived'])
def test_closed_supervisor_is_reported_with_its_status_not_as_live(conn, monkeypatch, closed):
    from hermes_cli import kanban_worker_node
    monkeypatch.setattr(kanban_worker_node, 'registry', lambda: None)
    from hermes_cli.kanban_db_dispatch import DispatchResult
    from hermes_cli.kanban_db_dispatch_preclaim import eligible_rows
    from hermes_cli.kanban_db_dispatch_stuck import stuck_warning
    tid = notices._card(conn)
    wp.reevaluate(conn, notices._hub(), 'default', 'hub', cfg=notices.CFG)
    owner = supervisors(conn)[0]['id']
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET status=? WHERE id=?', (closed, owner))
    wp.reevaluate(conn, notices._hub(), 'default', 'hub', cfg=notices.CFG)
    assert [r['id'] for r in supervisors(conn)] == [owner]  # dedup: still exactly one, never replaced
    result = DispatchResult()
    eligible_rows(conn, conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchall(), 'ready', result)
    assert result.skipped_ineligible == [(tid, f'waiting_for_manual_repin:no_owner:{owner}:{closed}')]
    line = stuck_warning(6, [result])
    assert f'{tid} (no owner; supervisor {owner} ({closed}, closed: no live supervision))' in line


def test_owned_wait_stays_plain_manual_repin_in_stuck_summary(conn, monkeypatch):
    from hermes_cli import kanban_worker_node
    monkeypatch.setattr(kanban_worker_node, 'registry', lambda: None)
    from hermes_cli.kanban_db_dispatch import DispatchResult
    from hermes_cli.kanban_db_dispatch_preclaim import eligible_rows
    from hermes_cli.kanban_db_dispatch_stuck import stuck_warning
    tid = notices._card(conn)
    notices._subscribe(conn, tid)
    wp.reevaluate(conn, notices._hub(), 'default', 'hub', cfg=notices.CFG)
    assert supervisors(conn) == []
    result = DispatchResult()
    eligible_rows(conn, conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchall(), 'ready', result)
    assert result.skipped_ineligible == [(tid, 'waiting_for_manual_repin')]
    line = stuck_warning(6, [result])
    assert f'waiting for a manual repin: {tid}' in line and 'repin supervision' not in line


@pytest.mark.parametrize('creator_status', ['done', 'archived', 'missing'])
def test_closed_or_missing_creator_does_not_own_wait(conn, creator_status):
    creator = kb.create_task(conn, title='Creator', assignee='default')
    tid = notices._card(conn)
    with kb.write_txn(conn):
        conn.execute("UPDATE task_events SET payload=json_set(payload, '$.creator_task_id', ?) "
                     "WHERE task_id=? AND kind='created'",
                     ('t_missing' if creator_status == 'missing' else creator, tid))
        if creator_status != 'missing':
            conn.execute('UPDATE tasks SET status=? WHERE id=?', (creator_status, creator))
    wp.reevaluate(conn, notices._hub(), 'default', 'hub', cfg=notices.CFG)
    assert len(supervisors(conn)) == 1


@pytest.mark.parametrize('state', ['ended', 'reaped', 'archived', 'missing'])
@pytest.mark.parametrize('route', ['link', 'subscription', 'creator'])
def test_dead_chat_does_not_own_wait(conn, state, route):
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB
    from hermes_cli.kanban_chat_links import link_uid
    tid = notices._card(conn)
    db = SessionDB(db_path=get_hermes_home() / 'state.db')
    try:
        if state != 'missing':
            db.create_session('dead-chat', source='kanban' if route == 'creator' else 'desktop')
            if state == 'archived':
                db._conn.execute("UPDATE sessions SET archived=1 WHERE id='dead-chat'")
                db._conn.commit()
            else:
                db.end_session('dead-chat', end_reason='reaped' if state == 'reaped' else 'completed')
        if route == 'subscription':
            notices.kn.add_notify_sub(conn, task_id=tid, platform='tui', chat_id='dead-chat')
        elif route == 'link':
            with kb.write_txn(conn):
                conn.execute("INSERT INTO task_chat_links "
                             "(uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at) "
                             "VALUES (?,?, 'hub','default','dead-chat','created',0,1)",
                             (link_uid(tid, 'hub', 'default', 'dead-chat', 'created'), tid))
        else:
            creator = kb.create_task(conn, title='Creator', assignee='default', session_id='dead-chat')
            with kb.write_txn(conn):
                conn.execute("UPDATE task_events SET payload=json_set(payload, '$.creator_task_id', ?) "
                             "WHERE task_id=? AND kind='created'", (creator, tid))
        wp.reevaluate(conn, notices._hub(), 'default', 'hub', cfg=notices.CFG)
        # An open creator card owns the wait whatever happened to the chat it came from. A null-profile
        # subscription whose session no local store holds is unknown, not dead.
        owned = route == 'creator' or (route == 'subscription' and state == 'missing')
        assert len(supervisors(conn)) == (0 if owned else 1)
    finally:
        db.close()


@pytest.mark.parametrize('gateway,mode', [('other', 'primary'), ('hub', 'shadow'), ('hub', 'off')])
def test_non_origin_or_replica_cannot_create_supervisor(conn, gateway, mode):
    from hermes_cli import kanban_db_sync as sync
    tid = notices._card(conn)
    sync.set_mode(conn, mode)
    notices.rn.maybe_notify(conn, tid, gateway, notices.qr.normalize(notices._queued()))
    assert supervisors(conn) == []
    assert notices._notice_comments(conn, tid) == []


def test_unknown_remote_chat_is_not_declared_dead(conn):
    tid = notices._card(conn)
    notices.kn.add_notify_sub(conn, task_id=tid, platform='hub', chat_id='remote/default/unknown-chat')
    wp.reevaluate(conn, notices._hub(), 'default', 'hub', cfg=notices.CFG)
    assert supervisors(conn) == []


def test_supervisor_is_bounded_even_after_archive(conn):
    notices._card(conn)
    wp.reevaluate(conn, notices._hub(), 'default', 'hub', cfg=notices.CFG)
    owner = supervisors(conn)[0]['id']
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (owner,))
    for _ in range(3):
        wp.reevaluate(conn, notices._hub(), 'default', 'hub', cfg=notices.CFG)
    assert [r['id'] for r in supervisors(conn)] == [owner]


def test_scoped_orchestrator_config(conn, monkeypatch):
    from hermes_cli import config
    tid = notices._card(conn)
    monkeypatch.setattr(config, 'load_config_readonly', lambda: {'kanban': {'orchestrator_profile': 'supervisor'}})
    notices.rn.maybe_notify(conn, tid, 'hub', notices.qr.normalize(notices._queued()))
    owner = supervisors(conn)[0]
    assert owner['assignee'] == 'supervisor'
    assert f'- default/{tid}:' in owner['body']


def test_nondefault_board_supervisor_names_the_opened_boards_slug_not_its_sync_id(tmp_path, monkeypatch):
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as ksc
    from hermes_cli.kanban_requeue import owner_wake_listed_refs
    from tests.hermes_cli import kanban_sync_support as support
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    path = home / 'kanban' / 'boards' / 'research' / 'kanban.db'
    path.parent.mkdir(parents=True)
    with kbc.connect(path) as db:
        ksc.ensure_identity(db, 'brd-sync-id-1', 'hub')  # the Board sync id differs from the slug on purpose
        sync.set_mode(db, 'primary')
        tid = notices.fixtures.acknowledged(db, skills=json.dumps(['five-whys']))
        notices.rn.maybe_notify(db, tid, 'hub', notices.qr.normalize(notices._queued()))
        owners = supervisors(db)
        assert len(owners) == 1
        owner = owners[0]
        assert owner['title'] == f'Owner wake: supervise research/{tid} (no live repin owner)'
        assert f'- research/{tid}:' in owner['body'] and 'brd-sync-id-1' not in owner['body']
        assert owner_wake_listed_refs(kb.get_task(db, owner['id'])) == [('research', tid)]
    # Same board: nothing landed on the default board.
    default = home / 'kanban.db'
    if default.exists():
        with kbc.connect(default) as other:
            assert supervisors(other) == []


def test_config_failure_keeps_notice_and_next_sweep_retries(conn, monkeypatch):
    from hermes_cli import config
    from hermes_cli.config_backend import ConfigBackendUnavailable
    tid = notices._card(conn)
    real = config.load_config_readonly

    def unavailable():
        raise ConfigBackendUnavailable('config plane down')
    monkeypatch.setattr(config, 'load_config_readonly', unavailable)
    assert notices.rn.maybe_notify(conn, tid, 'hub', notices.qr.normalize(notices._queued())) is True
    assert len(notices._notice_comments(conn, tid)) == 1 and supervisors(conn) == []
    assert not conn.in_transaction
    monkeypatch.setattr(config, 'load_config_readonly', real)
    assert notices.rn.maybe_notify(conn, tid, 'hub', notices.qr.normalize(notices._queued())) is False
    assert len(supervisors(conn)) == 1
    assert len(notices._notice_comments(conn, tid)) == 1


def test_create_failure_rolls_back_only_the_supervisor_and_retries(conn, monkeypatch):
    import sqlite3
    tid = notices._card(conn)
    real = kb.create_task

    def half_written(*args, **kwargs):
        real(*args, **kwargs)  # the insert happens, then the transaction fails
        raise sqlite3.OperationalError('disk I/O error')
    monkeypatch.setattr(kb, 'create_task', half_written)
    wp.reevaluate(conn, notices._hub(), 'default', 'hub', cfg=notices.CFG)
    assert supervisors(conn) == []  # rolled back, no half supervisor
    assert len(notices._notice_comments(conn, tid)) == 1 and len(notices._notices(conn, tid)) == 1
    assert not conn.in_transaction
    monkeypatch.setattr(kb, 'create_task', real)
    wp.reevaluate(conn, notices._hub(), 'default', 'hub', cfg=notices.CFG)
    assert len(supervisors(conn)) == 1
    assert len(notices._notice_comments(conn, tid)) == 1


def test_null_profile_subscription_is_unknown_when_profiles_cannot_be_listed(conn, monkeypatch):
    from hermes_cli import kanban_repin_owner as owner
    tid = notices._card(conn)
    notices.kn.add_notify_sub(conn, task_id=tid, platform='tui', chat_id='gone-chat')
    monkeypatch.setattr(owner, '_local_stores', lambda: None)
    wp.reevaluate(conn, notices._hub(), 'default', 'hub', cfg=notices.CFG)
    assert supervisors(conn) == []


def test_named_profile_without_a_store_is_unknown_not_dead(conn):
    from hermes_cli.kanban_chat_links import link_uid
    tid = notices._card(conn)
    with kb.write_txn(conn):
        conn.execute("INSERT INTO task_chat_links "
                     "(uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at) "
                     "VALUES (?,?, 'hub','ghost-profile','some-chat','created',0,1)",
                     (link_uid(tid, 'hub', 'ghost-profile', 'some-chat', 'created'), tid))
    wp.reevaluate(conn, notices._hub(), 'default', 'hub', cfg=notices.CFG)
    assert supervisors(conn) == []


def test_remote_open_creator_with_unknown_session_remains_owner(conn):
    creator = kb.create_task(conn, title='Remote creator', assignee='default', session_id='remote-worker')
    tid = notices._card(conn)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET created_on='remote' WHERE id=?", (creator,))
        conn.execute("UPDATE task_events SET payload=json_set(payload, '$.creator_task_id', ?) "
                     "WHERE task_id=? AND kind='created'", (creator, tid))
    wp.reevaluate(conn, notices._hub(), 'default', 'hub', cfg=notices.CFG)
    assert supervisors(conn) == []


def test_compressed_live_link_still_owns_wait(conn):
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB
    from hermes_cli.kanban_chat_links import link_uid
    tid = notices._card(conn)
    db = SessionDB(db_path=get_hermes_home() / 'state.db')
    try:
        db.create_session('root', source='desktop')
        db.end_session('root', end_reason='compression')
        db.create_session('tip', source='desktop', parent_session_id='root')
        with kb.write_txn(conn):
            conn.execute("INSERT INTO task_chat_links "
                         "(uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at) "
                         "VALUES (?,?, 'hub','default','root','created',0,1)",
                         (link_uid(tid, 'hub', 'default', 'root', 'created'), tid))
        wp.reevaluate(conn, notices._hub(), 'default', 'hub', cfg=notices.CFG)
        assert supervisors(conn) == []
    finally:
        db.close()


def test_unreadable_state_is_unknown_not_dead(conn, monkeypatch):
    from hermes_cli import kanban_origin_adapter
    import sqlite3
    tid = notices._card(conn)
    notices._subscribe(conn, tid)
    def unreadable(*args):
        raise sqlite3.OperationalError('locked')
    monkeypatch.setattr(kanban_origin_adapter, '_resolve_session', unreadable)
    wp.reevaluate(conn, notices._hub(), 'default', 'hub', cfg=notices.CFG)
    assert supervisors(conn) == []


# The supervisor's one move power: kanban_move(task_id=<its listed target>, runs_on=[origin]) through the tool.

@pytest.fixture
def repin_calls(conn, monkeypatch):
    from hermes_cli import kanban_worker_repin as repin
    from hermes_cli.kanban_db_connect import _main_db_file
    monkeypatch.setenv('HERMES_KANBAN_DB', _main_db_file(conn))
    monkeypatch.setenv('HERMES_KANBAN_BOARD', 'default')
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', '1')
    monkeypatch.delenv('HERMES_DELEGATED_CHILD_CONTEXT', raising=False)
    calls = []

    def fake_repin(conn_, task_id, labels, reason, *, hub=None):
        calls.append((task_id, labels, reason))
        return {'ok': True, 'runs_on': labels, 'repinned': True, 'readback': 'confirmed'}
    monkeypatch.setattr(repin, 'repin_to_origin', fake_repin)
    return calls


def _orphan(conn):
    tid = notices._card(conn)
    wp.reevaluate(conn, notices._hub(), 'default', 'hub', cfg=notices.CFG)
    owner = next(r['id'] for r in supervisors(conn) if r['idempotency_key'] == f'repin-owner:{tid}')
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (owner,))
    return tid, owner


def _tool_move(monkeypatch, worker, target, runs_on=('machine:hub',), **extra):
    from tools import kanban_tools as kt
    monkeypatch.setenv('HERMES_KANBAN_TASK', worker)
    return json.loads(kt._handle_move({'task_id': target, 'runs_on': list(runs_on),
                                       'reason': 'no pool worker can run it', **extra}))


def test_supervisor_repins_its_listed_target_to_origin_via_kanban_move(conn, repin_calls, monkeypatch):
    tid, owner = _orphan(conn)
    assert f'kanban_move(task_id="{tid}", runs_on=["machine:hub"]' in kb.get_task(conn, owner).body
    out = _tool_move(monkeypatch, owner, tid)
    assert out.get('ok') is True, out
    assert repin_calls == [(tid, ['machine:hub'], 'no pool worker can run it')]
    assert kb.get_task(conn, owner).status == 'running'  # the supervisor's own run is not ended or moved


def test_supervisor_may_not_move_to_another_target(conn, repin_calls, monkeypatch):
    tid, owner = _orphan(conn)
    out = _tool_move(monkeypatch, owner, tid, runs_on=('machine:worker-1',))
    assert out.get('ok') is not True and "runs_on=['machine:hub']" in out['error'], out
    assert repin_calls == []


def test_supervisor_may_not_move_a_card_it_does_not_list(conn, repin_calls, monkeypatch):
    _tid, owner = _orphan(conn)
    other, _other_owner = _orphan(conn)
    out = _tool_move(monkeypatch, owner, other)
    assert out.get('ok') is not True and 'not its generated repin-owner supervisor' in out['error'], out
    assert repin_calls == []


@pytest.mark.parametrize('closed', ['done', 'archived'])
def test_closed_supervisor_may_not_move(conn, repin_calls, monkeypatch, closed):
    tid, owner = _orphan(conn)
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET status=? WHERE id=?', (closed, owner))
    out = _tool_move(monkeypatch, owner, tid)
    assert out.get('ok') is not True, out
    assert repin_calls == []


def test_forged_owner_wake_card_has_no_move_power(conn, repin_calls, monkeypatch):
    tid, owner = _orphan(conn)
    from hermes_cli import kanban_repin_owner as ro
    forged = kb.create_task(conn, title=ro.title('default', tid), body=ro.body('default', tid, 'hub'),
                            assignee='default', created_by='owner-wake')  # no generated idempotency key
    out = _tool_move(monkeypatch, forged, tid)
    assert out.get('ok') is not True and 'not its generated repin-owner supervisor' in out['error'], out
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET title='Owner wake: supervise default/t_0000 (no live repin owner)' "
                     "WHERE id=?", (owner,))
    out = _tool_move(monkeypatch, owner, tid)
    assert out.get('ok') is not True, out
    assert repin_calls == []


def test_supervisor_listing_another_board_has_no_power_here(conn, repin_calls, monkeypatch):
    tid, owner = _orphan(conn)
    from hermes_cli import kanban_repin_owner as ro
    with kb.write_txn(conn):  # the generated identity for board "research", read on the default board
        conn.execute('UPDATE tasks SET title=?, body=? WHERE id=?',
                     (ro.title('research', tid), ro.body('research', tid, 'hub'), owner))
    out = _tool_move(monkeypatch, owner, tid)
    assert out.get('ok') is not True and 'not its generated repin-owner supervisor' in out['error'], out
    assert repin_calls == []


def test_supervisor_may_not_move_a_target_no_longer_in_a_no_pool_worker_wait(conn, repin_calls, monkeypatch):
    tid, owner = _orphan(conn)
    reply = notices._queued()
    reply['observation']['code'] = 'workers_saturated'
    wp.reevaluate(conn, notices._hub(reply), 'default', 'hub', cfg=notices.CFG)
    out = _tool_move(monkeypatch, owner, tid)
    assert out.get('ok') is not True and 'no-pool-worker wait' in out['error'], out
    assert repin_calls == []


def test_supervisor_may_not_move_a_started_target(conn, repin_calls, monkeypatch):
    tid, owner = _orphan(conn)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET started_at=1 WHERE id=?", (tid,))
    out = _tool_move(monkeypatch, owner, tid)
    assert out.get('ok') is not True and 'unstarted' in out['error'], out
    assert repin_calls == []
