"""Administrative stop must preserve the board's ordinary transport progress."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_stop_context as ctx
from hermes_cli import kanban_stop_ended as stop
from tests.hermes_cli import test_kanban_stop_ended as fixtures
from tests.hermes_cli.kanban_sync_fake_board import FakeService

shadow = fixtures.shadow
ME, BOARD, SID = fixtures.ME, fixtures.BOARD, fixtures.SID


@pytest.mark.parametrize('acked_before_stop', [True, False])
def test_stop_push_ack_pull_preserves_rows_and_unrelated_progress(shadow, acked_before_stop):
    service = FakeService()
    hub = service.hub(ME)
    source, target, child = fixtures.ended_card(shadow)
    if acked_before_stop:
        ksc.push(shadow, hub, BOARD, ME, primary=False)
        ksc.pull(shadow, hub, BOARD, observers=False)
    with ctx.bound(SID, gateway=ME, profile='default', source='desktop', source_reader=fixtures.INTERACTIVE) as token:
        stop.apply(shadow, owner=token, **fixtures.args(shadow, target))
    protected = fixtures.state(shadow, target, source, child)
    ksc.push(shadow, hub, BOARD, ME, primary=False)
    ksc.pull(shadow, hub, BOARD, observers=False)
    assert shadow.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE state='pending'").fetchone()[0] == 0
    assert sync.sync_state(shadow)['cursor'] == service.board(BOARD)['seq']
    assert kb.get_task(shadow, target).status == 'triage'
    assert fixtures.state(shadow, target, source, child)['links'] == protected['links']
    assert shadow.execute('SELECT outcome FROM task_runs WHERE task_id=?', (target,)).fetchone()[0] == 'crashed'
    # A write already accepted before the marker arrived can still follow in a feed page.
    # FakeService has no new hub barrier; this injects that historical ordering, not a claim
    # that the deployed service accepts new foreign writes after the marker.
    board = service.board(BOARD)
    service._put(board, 'tasks', target, {'status': 'ready', 'title': 'late foreign'}, 'foreign', None, '9999999999999-0000-foreign')
    service._put(board, 'tasks', source, {'title': 'unrelated progress'}, ME, None, '9999999999999-0001-hub')
    run_uid = protected['runs'][0]['uid']
    event_uid = shadow.execute('SELECT uid FROM task_events WHERE task_id=? AND kind=?', (target, stop.KIND)).fetchone()[0]
    for table, uid in [('task_links', source + '>' + target), ('task_links', target + '>' + child),
                       ('task_runs', run_uid), ('task_events', event_uid), ('tasks', target)]:
        service._emit(board, table, uid, 'delete', None, 'foreign', None)
    ksc.pull(shadow, hub, BOARD, observers=False)
    assert fixtures.state(shadow, target, source, child)['links'] == protected['links']
    assert shadow.execute('SELECT COUNT(*) FROM task_runs WHERE uid=?', (run_uid,)).fetchone()[0] == 1
    assert shadow.execute('SELECT COUNT(*) FROM task_events WHERE uid=?', (event_uid,)).fetchone()[0] == 1
    assert sync.sync_state(shadow)['cursor'] == board['seq']
    assert kb.get_task(shadow, target).status == 'triage'
    assert kb.get_task(shadow, target).title == 'M5'
    assert kb.get_task(shadow, source).title == 'unrelated progress'


def _leftover_worker_start(conn, task_id):
    """Timed parks, spend triage and no-progress parks clear worker_pid and keep the start token."""
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_started_at='boot-1:40|25077876' WHERE id=?", (task_id,))


def _stop(conn, target):
    with ctx.bound(SID, gateway=ME, profile='default', source='desktop', source_reader=fixtures.INTERACTIVE) as token:
        return stop.apply(conn, owner=token, **fixtures.args(conn, target))


def _drained(conn, service):
    return (conn.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE state='pending'").fetchone()[0] == 0
            and sync.sync_state(conn)['cursor'] == service.board(BOARD)['seq'])


def test_stopping_a_card_with_a_leftover_worker_start_keeps_the_board_syncing(shadow):
    service = FakeService()
    hub = service.hub(ME)
    _source, target, _child = fixtures.ended_card(shadow)
    _leftover_worker_start(shadow, target)
    _stop(shadow, target)
    assert shadow.execute('SELECT worker_started_at FROM tasks WHERE id=?', (target,)).fetchone()[0] is None
    ksc.push(shadow, hub, BOARD, ME, primary=False)  # the ack stamps tasks.sync_version on the stopped row
    ksc.pull(shadow, hub, BOARD, observers=False)
    assert _drained(shadow, service)
    assert kb.get_task(shadow, target).status == 'triage'


def test_a_second_stop_on_an_already_fenced_board_accepts_a_leftover_worker_start(shadow):
    _source, first, _child = fixtures.ended_card(shadow)
    _stop(shadow, first)  # installs the fences
    _source2, second, _child2 = fixtures.ended_card(shadow)
    _leftover_worker_start(shadow, second)
    assert _stop(shadow, second)['status'] == 'triage'
    row = shadow.execute('SELECT status, worker_started_at FROM tasks WHERE id=?', (second,)).fetchone()
    assert tuple(row) == ('triage', None)


def test_a_round_heals_a_card_stopped_with_a_leftover_worker_start(shadow):
    """paintbot-tools M5: stopped by a core that kept the token; every round then failed on the ack."""
    from hermes_cli import kanban_stop_guards as guards
    from hermes_cli.kanban_sync_round_send import send
    service = FakeService()
    hub = service.hub(ME)
    source, target, child = fixtures.ended_card(shadow)
    _stop(shadow, target)
    _leftover_worker_start(shadow, source)  # not stopped: its token is not this heal's business
    with kb.write_txn(shadow):  # the row as the older stop left it: fenced, token still set
        shadow.execute('DROP TRIGGER stop_ended_task_update')
        shadow.execute("UPDATE tasks SET worker_started_at='boot-1:40|25077876' WHERE id=?", (target,))
        guards.install(shadow)
    protected = fixtures.state(shadow, target, source, child)
    send(shadow, hub, BOARD, ME, 'shadow', ksc.RoundResult(board=BOARD, mode='shadow'), after_pull=False, before_head=False)
    ksc.pull(shadow, hub, BOARD, observers=False)
    assert _drained(shadow, service)
    after = fixtures.state(shadow, target, source, child)
    assert after['task'].pop('worker_started_at') is None and protected['task'].pop('worker_started_at')
    for row in (after['task'], protected['task'], *after['runs'], *protected['runs']):
        for moved in ('sync_version', 'field_hlc'):  # the ack and the feed echo; nothing else may differ
            row.pop(moved, None)
    assert after['task'] == protected['task'] and after['runs'] == protected['runs'] and after['links'] == protected['links']
    assert shadow.execute('SELECT worker_started_at FROM tasks WHERE id=?', (source,)).fetchone()[0]
    assert guards.clear_stale_worker_start(shadow) == 0


def _replica_after_stop(shadow, service, hub):
    """The live file stops the card; the replica (sync-shadow/<board>.db) pulls the Board feed up to the stop."""
    _source, target, _child = fixtures.ended_card(shadow)
    _stop(shadow, target)
    ksc.push(shadow, hub, BOARD, ME, primary=False)
    replica = ksc.open_shadow(BOARD, ME)
    ksc.pull(replica, hub, BOARD, observers=False)
    return target, replica


def test_replica_applies_the_archive_of_a_stopped_card_but_live_stays_frozen(shadow):
    """t_01a21a1955976537: the replica froze paintbot-tools M5 at its pre-archive status for 40 h."""
    service = FakeService()
    hub = service.hub(ME)
    target, replica = _replica_after_stop(shadow, service, hub)
    try:
        assert sync.SHADOW_DIR in str(ksc.shadow_path(BOARD))
        board = service.board(BOARD)
        service._put(board, 'tasks', target, {'status': 'archived', 'title': 'M5'}, ME, None, '9999999999999-0000-hub')
        ksc.pull(replica, hub, BOARD, observers=False)
        assert kb.get_task(replica, target).status == 'archived'
        ksc.pull(shadow, hub, BOARD, observers=False)  # the live file keeps its stop fence
        assert kb.get_task(shadow, target).status == 'triage'
    finally:
        replica.close()


def test_replica_frozen_before_the_fix_is_resynced_once(shadow, monkeypatch):
    from hermes_cli import kanban_sync_replica_unfreeze as unfreeze
    service = FakeService()
    hub = service.hub(ME)
    target, replica = _replica_after_stop(shadow, service, hub)
    try:
        board = service.board(BOARD)
        service._put(board, 'tasks', target, {'status': 'archived', 'title': 'M5'}, ME, None, '9999999999999-0000-hub')
        path = ksc.shadow_path(BOARD)
        assert unfreeze.needed(replica, path)
        assert unfreeze.unfreeze(replica, hub, BOARD, path) > 0
        assert kb.get_task(replica, target).status == 'archived'
        assert not unfreeze.needed(replica, path)
        assert unfreeze.unfreeze(replica, hub, BOARD, path) == 0
    finally:
        replica.close()
        ksc.shadow_path(BOARD).with_name(ksc.shadow_path(BOARD).name + unfreeze.SUFFIX).unlink(missing_ok=True)
