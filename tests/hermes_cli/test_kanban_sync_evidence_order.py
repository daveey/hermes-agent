"""A card's own lifecycle event is sent ahead of the status change it explains. [daveey]

Observed (cohort run1044206): t_01a2142540c71842 read ``scheduled`` with no park event for one sample because
the status row left the writer before its ``continued`` event; t_01a20fdfde58e6ba read ``blocked`` with its ask
cleared because the Board answered a ``ready`` whose ``unblocked`` origin it had not seen with a merged row.
"""
import json

import pytest

from hermes_cli import kanban_continuation as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from hermes_cli.kanban_actionability_sources import describe
from hermes_cli.kanban_sync_evidence_order import COLUMN
from hermes_cli.kanban_sync_parent_order import parent_first
from hermes_cli.kanban_sync_quarantine import pending_rows
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

ME, BOARD = 'worker', 'default'
CLOCK = {'kind': 'clock', 'why': 'the release window opens then'}
ASK = {'action': 'Reply A or B in this chat. Unblock = build the pick; archive = drop it.',
       'why_human': 'only the owner knows the preference', 'interaction': 'reply', 'scope': 'one pick',
       'evidence': 'both options pass the tests'}


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(path, True)
    return path


@pytest.fixture
def conn(home):
    db = kbc.connect(home / 'kanban.db')
    ksc.ensure_identity(db, BOARD, ME)
    yield db
    db.close()


@pytest.fixture
def svc():
    return FakeService()


def _push(conn, svc):
    """One drain; returns the ops in the order they left."""
    before = len(svc.calls)
    ksc.push(conn, svc.hub(ME), BOARD, ME, primary=True)
    return [op for call in svc.calls[before:] if call[0] == 'ops' for op in call[2]]


def _acked(conn, svc, title='card'):
    tid = kb.create_task(conn, title=title, assignee='default')
    _push(conn, svc)
    assert conn.execute('SELECT sync_version FROM tasks WHERE id=?', (tid,)).fetchone()[0] is not None
    return tid


def _running(conn, svc, tid):
    assert kb.claim_task(conn, tid) is not None
    _push(conn, svc)
    assert not conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE state='pending'").fetchone()


def _index(ops, tid, *, status=None, kind=None):
    for i, op in enumerate(ops):
        fields = op.get('fields') or {}
        if status and op['table'] == 'tasks' and op['uid'] == tid and fields.get('status') == status:
            return i
        if kind and op['table'] == 'task_events' and fields.get('task_id') == tid and fields.get('kind') == kind:
            return i
    raise AssertionError(f'no {status or kind} op for {tid} in {[(o["table"], o.get("fields", {}).get("kind")) for o in ops]}')


def _status_op(conn, tid, status):
    return next(dict(r) for r in conn.execute(
        "SELECT * FROM kanban_sync_outbox WHERE table_name='tasks' AND row_uid=? ORDER BY rowid", (tid,))
        if json.loads(r['fields']).get('status') == status)


def _event_uid(conn, tid, kind):
    return conn.execute('SELECT uid FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1',
                        (tid, kind)).fetchone()[0]


def _park_by_continue(conn, svc):
    tid = _acked(conn, svc)
    _running(conn, svc, tid)
    result = kc.continue_task(conn, tid, checkpoint='pushed; waiting for CI', resume_after_seconds=900, awaiting=CLOCK)
    assert result['ok'] and kb.get_task(conn, tid).status == 'scheduled', result
    return tid


def _park_by_schedule(conn, svc):
    tid = _acked(conn, svc)
    assert kb.schedule_task(conn, tid, reason='SCHEDULED_UNTIL=2099-01-20T00:00:00Z release window', awaiting=CLOCK,
                            clock_only=True)
    return tid


def _park_by_block(conn, svc):
    tid = _acked(conn, svc)
    assert kb.block_task(conn, tid, reason='pick one', kind='needs_input', human_request=ASK)
    return tid


PARKS = [(_park_by_continue, 'scheduled', 'continued'), (_park_by_schedule, 'scheduled', 'scheduled'),
         (_park_by_block, 'blocked', 'blocked')]


@pytest.mark.parametrize('park,status,kind', PARKS)
def test_park_event_leaves_before_its_status(conn, svc, park, status, kind):
    tid = park(conn, svc)
    assert _status_op(conn, tid, status)[COLUMN] == _event_uid(conn, tid, kind)
    ops = _push(conn, svc)
    assert _index(ops, tid, kind=kind) < _index(ops, tid, status=status)
    assert not conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE state='pending'").fetchone()


@pytest.mark.parametrize('park,status,kind', PARKS)
def test_no_feed_prefix_shows_a_parked_card_without_its_deadline(conn, svc, park, status, kind):
    """A replica that has applied any prefix of the park's feed never reads the card as deadline-missing."""
    tid = park(conn, svc)
    start = len(svc.board(BOARD)['changes'])
    _push(conn, svc)
    replica = kbc.connect(ksc.shadow_path(BOARD))
    try:
        seen = []
        for n, change in enumerate(svc.board(BOARD)['changes']):
            adapted = ksc.adapt(change)
            if adapted is None:
                continue
            sync.apply_remote_changes(replica, [adapted], new_cursor=change['seq'], board=BOARD, observers=False)
            row = replica.execute('SELECT status FROM tasks WHERE id=?', (tid,)).fetchone()
            if row is None or n < start:  # before the park: creation and the claim have their own, older order
                continue
            read = describe(replica, tid)
            seen.append((change['seq'], row[0], tuple(read['violations'])))
        assert seen and seen[-1][1] == status and seen[-1][2] == ()
        assert [s for s in seen if 'escalation_deadline_missing' in s[2]] == []
    finally:
        replica.close()


def test_block_ask_travels_in_the_blocked_status_op_and_a_run_end_stays_behind_it(conn, svc):
    tid = _park_by_block(conn, svc)
    ops = _push(conn, svc)
    patches = [op['fields'] for op in ops if op['table'] == 'tasks' and op['uid'] == tid]
    assert [set(p) & {'status', 'human_request'} for p in patches] == [{'status', 'human_request'}]
    assert patches[0]['status'] == 'blocked' and json.loads(patches[0]['human_request'])['action'] == ASK['action']
    assert _index(ops, tid, kind='blocked') < _index(ops, tid, status='blocked')
    tid = _park_by_continue(conn, svc)
    ops = _push(conn, svc)
    end = next(i for i, op in enumerate(ops) if op['table'] == 'task_runs' and op['fields'].get('ended_at'))
    assert _index(ops, tid, kind='continued') < _index(ops, tid, status='scheduled') < end


def test_unblock_event_leaves_before_the_ready_status_bound_to_it(conn, svc):
    tid = _park_by_block(conn, svc)
    _push(conn, svc)
    assert kb.unblock_task(conn, tid)
    event = _event_uid(conn, tid, 'unblocked')
    op = _status_op(conn, tid, 'ready')
    assert op['status_origin'] == event and op[COLUMN] == event
    ops = _push(conn, svc)
    assert _index(ops, tid, kind='unblocked') < _index(ops, tid, status='ready')


def test_claim_and_completion_keep_the_legacy_order(conn, svc):
    """The claim's event names a run born in its own transaction; a terminal pair belongs to terminal ingress."""
    tid = _acked(conn, svc)
    assert kb.claim_task(conn, tid) is not None
    assert _status_op(conn, tid, 'running')[COLUMN] is None
    ops = _push(conn, svc)
    assert _index(ops, tid, status='running') < _index(ops, tid, kind='claimed')
    assert kb.complete_task(conn, tid, summary='done')
    assert _status_op(conn, tid, 'done')[COLUMN] is None
    ops = _push(conn, svc)
    assert _index(ops, tid, status='done') < _index(ops, tid, kind='completed')


def test_unacknowledged_card_keeps_its_full_row_first(conn, svc):
    tid = kb.create_task(conn, title='never pushed', assignee='default')
    assert kb.schedule_task(conn, tid, reason='SCHEDULED_UNTIL=2099-01-20T00:00:00Z release window', awaiting=CLOCK,
                            clock_only=True)
    assert _status_op(conn, tid, 'scheduled')[COLUMN] is None
    ops = _push(conn, svc)
    first = next(i for i, op in enumerate(ops) if op['table'] == 'tasks' and op['uid'] == tid)
    assert first < _index(ops, tid, kind='scheduled')
    assert not conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE state='pending'").fetchone()


def test_two_status_captures_in_one_transaction_stay_unbound(conn, svc):
    tid = _acked(conn, svc)
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (tid,))
        kb._append_event(conn, tid, 'scheduled', {})
    assert [r[0] for r in conn.execute(f"SELECT {COLUMN} FROM kanban_sync_outbox WHERE table_name='tasks'")] == [None, None]


def test_rolled_back_park_binds_nothing_and_later_transactions_start_clean(conn, svc):
    tid = _acked(conn, svc)
    with pytest.raises(ValueError), kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (tid,))
        kb._append_event(conn, tid, 'scheduled', {})
        raise ValueError('rollback')
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (tid,))
    assert _status_op(conn, tid, 'scheduled')[COLUMN] is None


def _selected(conn):
    now = int(ksc.time.time())
    return parent_first(conn, pending_rows(conn, now), now, ksc.PUSH_BATCH)


def test_status_waits_behind_its_event_in_backoff_without_holding_other_cards(conn, svc):
    tid = _park_by_schedule(conn, svc)
    other = _park_by_schedule(conn, svc)
    with kbc.write_txn(conn):
        conn.execute("UPDATE kanban_sync_outbox SET retry_after=? WHERE table_name='task_events' AND row_uid=?",
                     (int(ksc.time.time()) + 600, _event_uid(conn, tid, 'scheduled')))
    selected = _selected(conn)
    assert not [r for r in selected if r['table_name'] == 'tasks' and r['row_uid'] == tid]
    assert [r for r in selected if r['table_name'] == 'tasks' and r['row_uid'] == other]
    assert conn.execute('SELECT MAX(attempts) FROM kanban_sync_outbox').fetchone()[0] == 0


def test_status_travels_alone_once_its_event_was_rejected(conn, svc):
    tid = _park_by_schedule(conn, svc)
    with kbc.write_txn(conn):
        conn.execute("UPDATE kanban_sync_outbox SET state='rejected' WHERE table_name='task_events' AND row_uid=?",
                     (_event_uid(conn, tid, 'scheduled'),))
    assert [r['row_uid'] for r in _selected(conn) if r['table_name'] == 'tasks'] == [tid]


def test_event_is_found_after_a_transient_retry_rotated_its_op_id(conn, svc):
    tid = _park_by_schedule(conn, svc)
    uid = _event_uid(conn, tid, 'scheduled')
    with kbc.write_txn(conn):
        conn.execute("UPDATE kanban_sync_outbox SET op_id=?, created_at=created_at+5000 "
                     "WHERE table_name='task_events' AND row_uid=?", (sync.uuid7(), uid))
    selected = _selected(conn)
    order = [(r['table_name'], r['row_uid']) for r in selected]
    assert order.index(('task_events', uid)) < order.index(('tasks', tid))
    assert len({r['op_id'] for r in selected}) == len(selected)


def test_promotion_rewrites_no_envelope_clock_or_fifo_timestamp(conn, svc):
    tid = _park_by_schedule(conn, svc)
    before = [tuple(r) for r in conn.execute(
        'SELECT op_id, fields, hlc, created_at, wire_json, status_origin FROM kanban_sync_outbox ORDER BY rowid')]
    _selected(conn)
    assert before == [tuple(r) for r in conn.execute(
        'SELECT op_id, fields, hlc, created_at, wire_json, status_origin FROM kanban_sync_outbox ORDER BY rowid')]
    ops = _push(conn, svc)
    assert all(COLUMN not in op and COLUMN not in (op.get('fields') or {}) for op in ops)
    assert _index(ops, tid, kind='scheduled') < _index(ops, tid, status='scheduled')
