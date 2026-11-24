"""Production push with controlled future membership reads, not live Board proof."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_replay_deferred as deferred
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_replay_observation as observation
from tests.hermes_cli import test_kanban_replay_deferred as helpers

live = helpers.live


class Observing(helpers.Deferred):
    def __init__(self):
        super().__init__()
        self.reads = []
        self.entries = []
        self.failure = None
        self.on_read = lambda: None

    def _call(self, method, path, body=None):
        if '/deferred?task=' not in path:
            return super()._call(method, path, body)
        from urllib.parse import unquote
        task = unquote(path.split('?task=')[1])
        self.reads.append(task)
        self.on_read()
        if self.failure:
            raise self.failure
        return dict(protocol='origin_pending1', board_incarnation=self.slot['board_incarnation'],
                    gateway_principal=self.slot['gateway_principal'], task=task, complete=True,
                    entries=self.entries)


def entry(origin='remote-origin', digest='a' * 64):
    return dict(origin=origin, hlc='1:0:remote', gateway='other-gateway', run=None,
                status='done', evidence_sha256=digest)


def seed(conn, task='task/a', origin='captured-origin'):
    wire = dict(table='tasks', op='upsert', uid=task, status_origin=origin,
                fields=dict(status='done'), hlc='1:0:local')
    with kbc.write_txn(conn):
        deferred.handoff(conn, wire, dict(deferred=dict(task=task, origin=origin)),
                         journal.snapshot(conn)['board_incarnation'])
    return wire


def poll(conn, receiver):
    local = journal.snapshot(conn)
    return observation.observe_one(conn, receiver, '/v2/boards/default/replay/', local,
                                   dict(deferred_observation=receiver.observation))


def test_actual_push_observes_after_ack_without_claiming_row_success(live):
    task = helpers.intent(live)
    receiver = Observing()
    receiver.slot.update(h='1', a='1', last_op_id=journal.snapshot(live)['last_op_id'],
                         last_payload_sha256=journal.snapshot(live)['last_payload_sha256'],
                         last_verdict=journal.snapshot(live)['last_verdict'])
    receiver.entries = [entry()]
    result = helpers.push(live, receiver)
    assert result.deferred == 1 and receiver.slot['a'] == '2'
    assert receiver.reads == [task]
    assert observation.observed(live) == [dict(board_incarnation=receiver.slot['board_incarnation'],
                                              task=task, **entry())]
    assert deferred.pending(live) == []  # old origin absent, NOT a successful status
    assert live.execute('SELECT sync_version FROM tasks WHERE id=?', (task,)).fetchone()[0] == 7
    assert journal.pending_wire(live) is None and len(receiver.sent) == 1


def test_unavailable_observer_does_not_block_origin_event_transport(live, caplog):
    helpers.intent(live)
    receiver = Observing()
    state = journal.snapshot(live)
    receiver.slot.update(h='1', a='1', last_op_id=state['last_op_id'],
                         last_payload_sha256=state['last_payload_sha256'], last_verdict=state['last_verdict'])
    receiver.failure = OSError('read offline')
    assert helpers.push(live, receiver).deferred == 1
    assert 'Deferred observation unavailable' in caplog.text
    assert len(deferred.pending(live)) == 1
    # Keep capability/read behavior, return an ordinary verdict for the origin event.
    original = receiver._call

    def event_reply(method, path, body=None):
        if path.endswith('/ops'):
            return helpers.helpers.Receiver._call(receiver, method, path, body)
        return original(method, path, body)
    receiver._call = event_reply
    assert helpers.push(live, receiver).applied == 1
    assert receiver.sent[-1]['table'] == 'task_events' and receiver.slot['a'] == '3'
    assert len(deferred.pending(live)) == 1


def test_observer_storage_failure_does_not_fail_already_acked_push(live, monkeypatch, caplog):
    import sqlite3
    helpers.intent(live)
    receiver = Observing()
    state = journal.snapshot(live)
    receiver.slot.update(h='1', a='1', last_op_id=state['last_op_id'],
                         last_payload_sha256=state['last_payload_sha256'], last_verdict=state['last_verdict'])

    def fail(*args):
        raise sqlite3.OperationalError('observation busy')
    monkeypatch.setattr(observation, '_store', fail)
    assert helpers.push(live, receiver).deferred == 1
    assert receiver.slot['a'] == '2' and journal.pending_wire(live) is None
    assert len(deferred.pending(live)) == 1 and observation.observed(live) == []
    assert 'observation busy' in caplog.text


def test_empty_membership_retires_diagnostic_not_transport_or_version(live):
    seed(live)
    before = journal.snapshot(live)
    assert poll(live, Observing())['status'] == 'observed'
    assert deferred.pending(live) == observation.observed(live) == []
    assert journal.snapshot(live) == before


@pytest.mark.parametrize('failure', [OSError('offline'), ValueError('partial'), None])
def test_unavailable_or_invalid_read_keeps_custody_and_previous_observation(live, failure):
    seed(live)
    receiver = Observing()
    receiver.entries = [entry('captured-origin')]
    assert poll(live, receiver)['status'] == 'observed'
    before = observation.observed(live)
    receiver.failure = failure
    receiver.entries = [dict(entry(), evidence_sha256='invalid')]
    result = poll(live, receiver)
    assert result['status'] == 'unavailable'
    assert observation.observed(live) == before and len(deferred.pending(live)) == 1


def test_capability_loss_does_not_read_or_retire(live):
    seed(live)
    receiver = Observing()
    receiver.observation = None
    assert poll(live, receiver)['status'] == 'unavailable'
    assert receiver.reads == [] and len(deferred.pending(live)) == 1


def test_rotation_persists_across_reopen_and_failed_tasks_do_not_starve(live):
    for task in ['a', 'b', 'c']:
        seed(live, task)
    receiver = Observing()
    receiver.failure = OSError('unavailable task')
    assert poll(live, receiver)['task'] == 'a'
    path = Path(live.execute('PRAGMA database_list').fetchone()[2])
    live.close()
    reopened = kbc.connect(path)
    try:
        assert [poll(reopened, receiver)['task'] for _ in range(3)] == ['b', 'c', 'a']
        assert receiver.reads == ['a', 'b', 'c', 'a']
    finally:
        reopened.close()


def test_new_same_origin_capture_during_io_survives_absence(live):
    wire = seed(live)
    receiver = Observing()

    def capture():
        with kbc.write_txn(live):
            deferred.handoff(live, dict(wire, hlc='2:0:local'),
                             dict(deferred=dict(task=wire['uid'], origin=wire['status_origin'])),
                             receiver.slot['board_incarnation'])
    receiver.on_read = capture
    assert poll(live, receiver)['status'] == 'observed'
    assert deferred.pending(live)[0]['hlc'] == '2:0:local'


def test_legacy_capture_migration_preserves_fields_and_fences_identical_recapture(live):
    local = journal.snapshot(live)
    with kbc.write_txn(live):
        live.execute(deferred._SCHEMA)
        live.execute('INSERT INTO kanban_replay_deferred1 VALUES (?,?,?,?,?)',
                     (local['board_incarnation'], 'task/a', 'captured-origin', 'done', '1:0:local'))
    before = deferred.pending(live)
    receiver = Observing()
    receiver.on_read = lambda: seed(live)
    assert poll(live, receiver)['status'] == 'observed'
    assert deferred.pending(live) == before
    assert live.execute('SELECT capture_token FROM kanban_replay_deferred1').fetchone()[0]


def test_identical_recapture_after_read_snapshot_is_not_retired(live):
    wire = seed(live)
    receiver = Observing()

    def recapture():
        with kbc.write_txn(live):
            live.execute('DELETE FROM kanban_replay_deferred1')
            deferred.handoff(live, wire, dict(deferred=dict(task=wire['uid'], origin=wire['status_origin'])),
                             receiver.slot['board_incarnation'])
    receiver.on_read = recapture
    poll(live, receiver)
    assert len(deferred.pending(live)) == 1


def test_overlapping_different_task_reads_both_commit(live):
    seed(live, 'a')
    seed(live, 'b')
    first = Observing()
    first.entries = [entry('first')]
    second = Observing()
    second.entries = [entry('second')]
    first.on_read = lambda: poll(live, second)
    assert poll(live, first)['status'] == 'observed'
    assert [(row['task'], row['origin']) for row in observation.observed(live)] == [('a', 'first'), ('b', 'second')]


def test_newer_unfinished_same_task_reservation_blocks_older_reply(live):
    seed(live)
    receiver = Observing()
    receiver.entries = [entry()]
    receiver.on_read = lambda: observation._reserve(live, journal.snapshot(live))
    assert poll(live, receiver)['status'] == 'superseded'
    assert observation.observed(live) == [] and len(deferred.pending(live)) == 1


def test_newer_poll_wins_over_late_old_response(live):
    seed(live)
    old = Observing()
    old.entries = [entry(digest='a' * 64)]
    newer = Observing()
    newer.entries = [entry(digest='b' * 64)]
    old.on_read = lambda: poll(live, newer)
    assert poll(live, old)['status'] == 'superseded'
    assert observation.observed(live)[0]['evidence_sha256'] == 'b' * 64


def test_binding_change_during_read_cannot_write_observation(live, monkeypatch):
    seed(live)
    receiver = Observing()
    original = journal.snapshot
    saved = original(live)
    receiver.on_read = lambda: monkeypatch.setattr(journal, 'snapshot', lambda conn: dict(saved, generation='2'))
    assert poll(live, receiver)['status'] == 'superseded'
    assert observation.observed(live) == [] and len(deferred.pending(live)) == 1


def test_observed_remote_only_membership_remains_polled_and_replaced(live):
    seed(live)
    receiver = Observing()
    receiver.entries = [entry()]
    poll(live, receiver)
    assert deferred.pending(live) == []
    receiver.entries = [entry(digest='b' * 64)]
    poll(live, receiver)
    assert len(receiver.reads) == 2 and observation.observed(live)[0]['evidence_sha256'] == 'b' * 64
    receiver.entries = []
    poll(live, receiver)
    assert observation.observed(live) == []
    assert poll(live, receiver)['status'] == 'idle'
    assert len(receiver.reads) == 3


def test_reservation_work_is_bounded_with_large_tracked_task_set(live):
    local = journal.snapshot(live)
    with kbc.write_txn(live):
        live.execute(deferred._SCHEMA)
        live.execute('INSERT INTO kanban_replay_deferred1 VALUES (?,?,?,?,?)',
                     (local['board_incarnation'], 'task-0000', 'origin', 'done', 'hlc'))
    # Warm lazy schema creation. Measure the entire reservation on the same
    # connection: shared transaction/schema overhead may grow with core's schema,
    # but adding tracked tasks must not add a table scan under the reservation.
    observation._reserve(live, local)

    def measure():
        steps = []
        live.set_progress_handler(lambda: steps.append(100) or 0, 100)
        try:
            task = observation._reserve(live, local)[0]
        finally:
            live.set_progress_handler(None, 0)
        return task, sum(steps)

    small_task, small_steps = measure()
    assert small_task == 'task-0000'
    with kbc.write_txn(live):
        live.executemany('INSERT INTO kanban_replay_deferred1 '
                         '(board_incarnation,task,origin,status,hlc) VALUES (?,?,?,?,?)',
                         [(local['board_incarnation'], f'task-{i:04d}', 'origin', 'done', 'hlc')
                          for i in range(1, 500)])
    large_task, large_steps = measure()
    assert large_task == 'task-0001'
    assert large_steps <= small_steps + 200


def test_observation_and_retirement_rollback_together(live, monkeypatch):
    seed(live)
    receiver = Observing()
    original = observation._store

    def fail(*args):
        original(*args)
        raise ValueError('after observation store')
    monkeypatch.setattr(observation, '_store', fail)
    with pytest.raises(ValueError, match='after observation store'):
        poll(live, receiver)
    assert observation.observed(live) == [] and len(deferred.pending(live)) == 1
