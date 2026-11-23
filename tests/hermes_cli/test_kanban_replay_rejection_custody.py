"""Selective rejection retirement is disjoint from unresolved issued custody."""
from contextlib import contextmanager
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_replay_journal as journal, kanban_sync_client as client
from hermes_cli import kanban_sync_rejections as rejections, kanban_sync_dependency_delete as dependency
from tests.hermes_cli import test_kanban_replay_sender as sender
from tests.hermes_cli import test_kanban_sync_rejections as rejected
from tests.hermes_cli import test_kanban_sync_dependency_delete as links

live = rejected.live
svc = rejected.svc
kanban_home = rejected.kanban_home
case = links.case


def pending(conn):
    with kbc.write_txn(conn):
        journal.initialize(conn, sender.binding(), board_id=conn.execute('SELECT board_id FROM kanban_sync_state').fetchone()[0])
    kb.create_task(conn, title='independent issued custody', assignee='worker')
    receiver = sender.Receiver()
    receiver.lose_reply = True
    with pytest.raises(OSError, match='reply lost'):
        client.push(conn, receiver, 'default', 'macbook', primary=False, max_batches=1)
    return receiver


def capture(conn):
    wire = journal.pending_wire(conn)
    op = json.loads(wire)['client_op_id']
    return wire, dict(conn.execute('SELECT * FROM kanban_sync_outbox WHERE op_id=?', (op,)).fetchone())


def fail_after_write(original):
    @contextmanager
    def fail(conn):
        with original(conn):
            yield
            raise RuntimeError('retirement before commit')
    return fail


@pytest.mark.parametrize('rollback', [False, True])
def test_superseded_rejection_never_consumes_issued_pending(live, svc, monkeypatch, rollback):
    rejected.reject_status(live, svc)
    hub = svc.hub('macbook')
    # Settle the current image through the existing normal primary pull.
    client.pull(live, hub, 'default', observers=False)
    receiver = pending(live)
    frozen, original = capture(live)
    rows = [tuple(r) for r in live.execute('SELECT * FROM kanban_sync_outbox ORDER BY op_id')]
    if rollback:
        with monkeypatch.context() as patch:
            patch.setattr(rejections, 'write_txn', fail_after_write(rejections.write_txn))
            with pytest.raises(RuntimeError, match='before commit'):
                rejections.resolve_rejections(live, hub, 'default')
        assert [tuple(r) for r in live.execute('SELECT * FROM kanban_sync_outbox ORDER BY op_id')] == rows
    reports = rejections.resolve_rejections(live, hub, 'default')
    assert reports and all(r['action'] == 'retired' for r in reports), reports
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE state='rejected'").fetchone()
    assert capture(live) == (frozen, original)
    receiver.lose_reply = False
    client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    assert len(receiver.sent) == 1 and journal.pending_wire(live) is None


@pytest.mark.parametrize('rollback', [False, True])
def test_dependency_receipt_retires_only_rejected_while_other_slot_waits(case, monkeypatch, rollback):
    conn, hub, _, uid, _, _ = case
    preview = dependency.plan(conn, hub, 'default', uid, 'retain canonical dependency')
    assert preview['eligible'], preview
    applied = dependency.apply(conn, hub, 'default', uid, preview['token'], 'retain canonical dependency')
    assert applied['action'] == 'applied', applied
    receiver = pending(conn)
    frozen, original = capture(conn)
    rows = [tuple(r) for r in conn.execute('SELECT * FROM kanban_sync_outbox ORDER BY op_id')]
    if rollback:
        with monkeypatch.context() as patch:
            patch.setattr(dependency, 'write_txn', fail_after_write(dependency.write_txn))
            with pytest.raises(RuntimeError, match='before commit'):
                rejections.resolve_rejections(conn, hub, 'default')
        assert [tuple(r) for r in conn.execute('SELECT * FROM kanban_sync_outbox ORDER BY op_id')] == rows
    reports = rejections.resolve_rejections(conn, hub, 'default')
    assert len(reports) == len(links.OP_IDS) and all(r['action'] == 'retired' for r in reports), reports
    assert not conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE state='rejected'").fetchone()
    assert capture(conn) == (frozen, original)
    receiver.lose_reply = False
    client.push(conn, receiver, 'default', 'macbook', primary=False, max_batches=1)
    assert len(receiver.sent) == 1 and journal.pending_wire(conn) is None
