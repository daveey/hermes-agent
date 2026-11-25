import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as dbc, kanban_home_transfer as transfer
from tests.hermes_cli import test_home_transfer_capture as support
from tests.hermes_cli import test_kanban_lanes as lanes_test
from tests.hermes_cli.test_kanban_lane_daily_limits import limits
conn = support.board
configured = lanes_test.configured
person = lanes_test.person

@pytest.mark.parametrize('fenced', [False, 'prepared', 'pending'])
def test_daily_release_skips_prepared(conn, configured, fenced):
    limits(configured, cards_per_day=0)
    held = lanes_test._ready_in(conn, 'babybear', 'daily parked')
    assert kb.get_task(conn, held).status == 'triage'
    neighbour = kb.create_task(conn, title='healthy unrelated', tenant='other')
    with dbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status='todo',created_on='worker-a3' WHERE id=?", (neighbour,))
        conn.execute("UPDATE tasks SET created_on='worker-a3',sync_version=7 WHERE id=?", (held,))
        conn.execute("UPDATE kanban_sync_state SET gateway_id='worker-a3'")
        conn.execute("DELETE FROM kanban_sync_outbox")
        if fenced == 'prepared':
            transfer.prepare(conn, held, source='worker-a3', destination='hub', nonce='daily-review-000001', expected_version=7)
        elif fenced == 'pending':
            from hermes_cli.kanban_home_generation import mirror_feed
            mirror_feed(conn, held, {'home_generation': 'daily-review-000001', 'home_transfer_pending': 'daily-review-000001'}, 8)
    limits(configured, cards_per_day=1)
    kb.recompute_ready(conn)
    assert kb.get_task(conn, neighbour).status == 'ready'
    assert kb.get_task(conn, held).status == ('triage' if fenced else 'ready')
