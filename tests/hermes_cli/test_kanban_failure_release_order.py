"""Release commands must not overtake the fix card they reference."""
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync, kanban_failure_release as release
from tests.hermes_cli import test_kanban_failure_transport as support

live = support.live


def test_release_follows_same_transaction_fix_card(live):
    tid, rid = support._running(live)
    with kbc.write_txn(live):
        fix = kb.create_task(live, title='systemic owner fixture', assignee='worker')
        release.queue(live, tid, rid, outcome='crashed', error='fixture', extra={'fix_card': fix})
    rows = sync.outbox(live)
    parent = next(i for i, r in enumerate(rows) if r['table_name'] == 'tasks' and r['row_uid'] == fix)
    command = next(i for i, r in enumerate(rows) if r['op'] == 'release')
    assert parent < command, [(r['op'], r['row_uid'], r['created_at']) for r in rows]
    assert rows[parent]['created_at'] <= rows[command]['created_at']
