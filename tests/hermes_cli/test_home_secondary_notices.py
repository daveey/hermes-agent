"""Secondary notice destinations and parent-held candidates respect home custody.

RED-only regressions for review6091081506. Real prepared/pending/malformed
custody must skip only the frozen write, not roll back a healthy child's notice.
"""
from contextlib import contextmanager

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_hold_notices as notices
from hermes_cli import kanban_home_transfer as transfer
from hermes_cli import kanban_parent_held as parents
from tests.hermes_cli import test_home_repair_allowance_custody as custody
from tests.hermes_cli import test_kanban_hold_failure_isolation as holds

kanban_home = custody.kanban_home
primary = custody.primary
# Export the imported autouse fixture: importing its module alone does not run it.
isolated_cursors = custody.isolated_cursors
KINDS = custody.KINDS


def snapshot(conn, task):
    return custody.snapshot(conn, task) + [
        [tuple(r) for r in conn.execute(
            "SELECT * FROM kanban_notify_subs WHERE task_id=? ORDER BY platform,chat_id", (task,))]]


def events(conn, task, kind):
    return [e for e in kb.list_events(conn, task) if e.kind == kind]


def setup_cards(conn, operation, *, creator=None):
    if operation == 'hold':
        child = holds._held(conn, 'runs', creator=creator)
        healthy = holds._held(conn, 'runs')
    else:
        parent = holds._held(conn, 'runs')
        child = kb.create_task(conn, title='waiting child', creator_task_id=creator)
        healthy = kb.create_task(conn, title='healthy waiting neighbour')
        kb.link_tasks(conn, parent, child)
        kb.link_tasks(conn, parent, healthy)
    return child, healthy


def sweep(conn, operation):
    (notices if operation == 'hold' else parents).sweep(conn)


def notice_kind(operation):
    return 'spend_budget_decision_ready' if operation == 'hold' else 'parent_held'


@pytest.mark.parametrize('operation', ['hold', 'parent'])
def test_control_unfrozen_creator_receives_one_notice(primary, operation):
    owner = kb.create_task(primary, title='creator')
    child, healthy = setup_cards(primary, operation, creator=owner)
    sweep(primary, operation)
    sweep(primary, operation)
    assert len(events(primary, child, notice_kind(operation))) == 1
    assert len(events(primary, healthy, notice_kind(operation))) == 1
    assert len(kb.list_comments(primary, owner)) == 1
    assert len(events(primary, owner, 'hold_creator_notified')) == 1


@pytest.mark.parametrize('kind', KINDS)
def test_creator_pending_declines_frozen_destination(primary, kind):
    owner = kb.create_task(primary, title='creator')
    child, _ = setup_cards(primary, 'hold', creator=owner)
    custody.freeze(primary, owner, kind)
    before = snapshot(primary, owner)
    assert transfer.frozen(primary, owner)
    assert not notices.creator_pending(primary, child, 'new-revision')
    assert snapshot(primary, owner) == before


@pytest.mark.parametrize('operation', ['hold', 'parent'])
@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('seam', ['existing', 'writer'])
def test_frozen_creator_does_not_roll_back_child_publication(
        primary, monkeypatch, caplog, operation, kind, seam):
    owner = kb.create_task(primary, title='creator in transfer')
    child, healthy = setup_cards(primary, operation, creator=owner)
    before = []
    original_writer = kb.write_txn
    armed = [seam == 'writer']

    def take_custody():
        custody.freeze(primary, owner, kind)
        assert transfer.frozen(primary, owner)
        before.append(snapshot(primary, owner))

    @contextmanager
    def writer(conn, *args, **kwargs):
        if armed[0]:
            armed[0] = False
            take_custody()
        with original_writer(conn, *args, **kwargs):
            yield

    if seam == 'existing':
        take_custody()
    monkeypatch.setattr(kb, 'write_txn', writer)
    sweep(primary, operation)
    # Check neighbour first: the missing child notice is not a whole-pass failure.
    assert len(events(primary, healthy, notice_kind(operation))) == 1
    assert snapshot(primary, owner) == before[0]
    assert len(events(primary, child, notice_kind(operation))) == 1
    assert not [r for r in caplog.records if r.exc_info]
    sweep(primary, operation)
    assert len(events(primary, child, notice_kind(operation))) == 1
    assert snapshot(primary, owner) == before[0]


@pytest.mark.parametrize('kind', KINDS)
def test_parent_sweep_skips_frozen_candidate_before_pending_read(
        primary, monkeypatch, caplog, kind):
    child, healthy = setup_cards(primary, 'parent')
    custody.freeze(primary, child, kind)
    assert transfer.frozen(primary, child)
    before = snapshot(primary, child)
    real_pending = parents._pending
    reads = []

    def pending(conn, task):
        reads.append(task)
        yield from real_pending(conn, task)

    monkeypatch.setattr(parents, '_pending', pending)
    parents.sweep(primary)
    assert len(events(primary, healthy, 'parent_held')) == 1
    assert snapshot(primary, child) == before
    assert child not in reads
    assert not [r for r in caplog.records if r.exc_info]


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('seam', ['callback', 'writer'])
def test_parent_sweep_rechecks_custody_after_selection(
        primary, monkeypatch, caplog, kind, seam):
    child, healthy = setup_cards(primary, 'parent')
    original_pending = parents._pending
    original_writer = kb.write_txn
    armed = [False]
    before = []
    attempted = []
    original_append = kb._append_event

    def take_custody():
        custody.freeze(primary, child, kind)
        assert transfer.frozen(primary, child)
        before.append(snapshot(primary, child))

    def pending(conn, task):
        result = list(original_pending(conn, task))
        if task == child and not before:
            if seam == 'callback':
                take_custody()
            else:
                armed[0] = True
        yield from result

    @contextmanager
    def writer(conn, *args, **kwargs):
        if armed[0]:
            armed[0] = False
            take_custody()
        with original_writer(conn, *args, **kwargs):
            yield

    def append(conn, task, kind, *args, **kwargs):
        if task == child and kind == 'parent_held':
            attempted.append(task)
        return original_append(conn, task, kind, *args, **kwargs)

    monkeypatch.setattr(parents, '_pending', pending)
    monkeypatch.setattr(kb, 'write_txn', writer)
    monkeypatch.setattr(kb, '_append_event', append)
    parents.sweep(primary)
    assert before, 'race must reach the post-selection custody boundary'
    assert len(events(primary, healthy, 'parent_held')) == 1
    assert snapshot(primary, child) == before[0]
    assert attempted == [], 'writer must skip a newly frozen candidate, not rely on SQL rejection'
    assert not [r for r in caplog.records if r.exc_info]


def release(conn, task, kind):
    """Authenticated exact custody resolution: abort for prepared, destination feed for pending."""
    if kind == 'prepared':
        with kb.write_txn(conn):
            transfer.abort_source(conn, dict(task=task, source='worker-a3', destination='hub',
                nonce='repair-test-nonce-0001', expected_version=7, phase='aborted', rows=[]))
    else:
        custody.feed_support.feed(conn, task, version=9, nonce=custody.NONCE)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (kb.get_task(conn, task).status, task))
    assert not transfer.frozen(conn, task)


@pytest.mark.parametrize('operation', ['hold', 'parent'])
@pytest.mark.parametrize('kind', ['prepared', 'pending'])
def test_creator_notice_recovers_once_after_custody_release(primary, operation, kind):
    owner = kb.create_task(primary, title='creator in transfer')
    child, _healthy = setup_cards(primary, operation, creator=owner)
    custody.freeze(primary, owner, kind)
    before = snapshot(primary, owner)
    sweep(primary, operation)
    # Child notice is published while the creator is frozen; handoff is deferred.
    assert len(events(primary, child, notice_kind(operation))) == 1
    assert snapshot(primary, owner) == before
    sweep(primary, operation)
    assert snapshot(primary, owner) == before
    release(primary, owner, kind)
    for _ in range(3):  # replay after release, then idempotent
        sweep(primary, operation)
    assert len(events(primary, child, notice_kind(operation))) == 1
    handoffs = [e for e in events(primary, owner, 'hold_creator_notified')
                if e.payload['source_task_id'] == child]
    assert len(handoffs) == 1
    assert len([c for c in kb.list_comments(primary, owner) if child in c.body]) == 1
