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
