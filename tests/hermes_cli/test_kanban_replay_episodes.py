"""Controlled future reads: reachable episodes are not delivered owner notices."""
import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_replay_episodes as episode_api
from hermes_cli import kanban_replay_observation as observation
from hermes_cli import kanban_replay_journal as journal
from tests.hermes_cli import test_kanban_replay_observation as helpers

live = helpers.live


@pytest.fixture
def clock(monkeypatch):
    state = ['process-one', 1000]
    monkeypatch.setattr(observation, '_sample', lambda: tuple(state), raising=False)
    return state


def episodes(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_replay_episode1'").fetchone():
        return []
    return [dict(row) for row in conn.execute('SELECT * FROM kanban_replay_episode1 ORDER BY task,origin')]


def setup(conn):
    helpers.seed(conn)
    receiver = helpers.Observing()
    receiver.entries = [helpers.entry()]
    return receiver


def test_actual_observer_tracks_unchanged_evidence_until_due_without_success(live, clock):
    receiver = setup(live)
    before = journal.snapshot(live)
    helpers.poll(live, receiver)
    assert len(episodes(live)) == 1
    assert episodes(live)[0]['reachable_ms'] == 0
    assert episode_api.diagnostics(live) == []
    for _ in range(5):
        clock[1] += 60000
        helpers.poll(live, receiver)
    row = episodes(live)[0]
    assert row['reachable_ms'] == 300000 and row['evidence_sha256'] == 'a' * 64
    assert episode_api.diagnostics(live) == [dict(board_incarnation=receiver.slot['board_incarnation'],
                                                 task='task/a', **helpers.entry(), reachable_ms=300000)]
    # No repeated due record, unbounded age or operation ledger.
    clock[1] += 60000
    helpers.poll(live, receiver)
    assert episodes(live)[0]['reachable_ms'] == 300000
    assert len(episodes(live)) == 1 and journal.snapshot(live) == before


def test_due_episode_survives_database_reopen_without_duplicate_records(live, clock):
    from pathlib import Path
    receiver = setup(live)
    helpers.poll(live, receiver)
    for _ in range(5):
        clock[1] += 60000
        helpers.poll(live, receiver)
    before = episode_api.diagnostics(live)
    path = Path(live.execute('PRAGMA database_list').fetchone()[2])
    live.close()
    reopened = kbc.connect(path)
    try:
        clock[0] = 'replacement-process'
        clock[1] += 10000
        helpers.poll(reopened, receiver)
        assert episode_api.diagnostics(reopened) == before and len(episodes(reopened)) == 1
    finally:
        reopened.close()


@pytest.mark.parametrize('interrupt', ['offline', 'invalid', 'capability', 'restart', 'long-gap', 'regression'])
def test_interrupted_sampling_never_counts_gap_or_erases_accumulated_time(live, clock, interrupt):
    receiver = setup(live)
    helpers.poll(live, receiver)
    clock[1] += 30000
    helpers.poll(live, receiver)
    assert episodes(live)[0]['reachable_ms'] == 30000
    clock[1] += 10000
    receiver.failure = {'offline': OSError('offline'), 'invalid': ValueError('bad read')}.get(interrupt)
    receiver.observation = None if interrupt == 'capability' else 'origin_pending1'
    if interrupt in ('offline', 'invalid', 'capability'):
        helpers.poll(live, receiver)
    receiver.failure = None
    receiver.observation = 'origin_pending1'
    clock[0] = 'new-process' if interrupt == 'restart' else clock[0]
    clock[1] += {'long-gap': 60001, 'regression': -50000}.get(interrupt, 0)
    helpers.poll(live, receiver)
    assert episodes(live)[0]['reachable_ms'] == 30000
    clock[1] += 10000
    helpers.poll(live, receiver)
    assert episodes(live)[0]['reachable_ms'] == 40000


def test_evidence_change_and_absence_reset_only_active_diagnostic_episode(live, clock):
    receiver = setup(live)
    helpers.poll(live, receiver)
    clock[1] += 60000
    helpers.poll(live, receiver)
    before = journal.snapshot(live)
    receiver.entries = [helpers.entry(digest='b' * 64)]
    clock[1] += 60000
    helpers.poll(live, receiver)
    assert episodes(live)[0]['evidence_sha256'] == 'b' * 64
    assert episodes(live)[0]['reachable_ms'] == 0
    receiver.entries = []
    helpers.poll(live, receiver)
    assert episodes(live) == [] and journal.snapshot(live) == before
    assert episode_api.diagnostics(live) == []


def test_read_latency_is_not_reachable_observation_time(live, clock):
    receiver = setup(live)
    receiver.on_read = lambda: clock.__setitem__(1, clock[1] + 20000)
    helpers.poll(live, receiver)  # finishes at 21000
    clock[1] += 10000
    helpers.poll(live, receiver)  # starts 31000, finishes 51000
    assert episodes(live)[0]['reachable_ms'] == 10000


@pytest.mark.parametrize('defect', ['epoch', 'clock'])
def test_inflight_sampling_interrupt_cannot_add_elapsed_time(live, clock, defect):
    receiver = setup(live)
    helpers.poll(live, receiver)
    clock[1] += 30000
    helpers.poll(live, receiver)
    clock[1] += 10000

    def interrupt():
        if defect == 'epoch':
            episode_api.interrupt(live)
        else:
            clock[1] -= 1
    receiver.on_read = interrupt
    helpers.poll(live, receiver)
    assert episodes(live)[0]['reachable_ms'] == 30000


def test_late_same_task_read_cannot_replace_episode_or_add_time(live, clock):
    receiver = setup(live)
    helpers.poll(live, receiver)
    clock[1] += 30000
    newer = helpers.Observing()
    newer.entries = [helpers.entry(digest='b' * 64)]
    receiver.on_read = lambda: helpers.poll(live, newer)
    assert helpers.poll(live, receiver)['status'] == 'superseded'
    assert episodes(live)[0]['evidence_sha256'] == 'b' * 64
    assert episodes(live)[0]['reachable_ms'] == 0


def test_episode_and_membership_storage_rollback_together(live, clock, monkeypatch):
    receiver = setup(live)
    helpers.poll(live, receiver)
    before = episodes(live)
    original = observation._store

    def fail(*args):
        original(*args)
        raise ValueError('after episode storage')
    monkeypatch.setattr(observation, '_store', fail)
    receiver.entries = [helpers.entry(digest='b' * 64)]
    clock[1] += 30000
    with pytest.raises(ValueError, match='after episode storage'):
        helpers.poll(live, receiver)
    # Reservation consumes only the sampling baseline before I/O; semantic state
    # still rolls back and a failed storage attempt cannot age the next sample.
    assert episodes(live) == [dict(row, sample_token='') for row in before]
    assert observation.observed(live)[0]['evidence_sha256'] == 'a' * 64


def test_post_ack_sqlite_failure_breaks_sampling_without_losing_age(live, clock, monkeypatch, caplog):
    import sqlite3
    receiver = setup(live)
    helpers.poll(live, receiver)
    clock[1] += 30000
    helpers.poll(live, receiver)
    original = observation._store

    def fail(*args):
        original(*args)
        raise sqlite3.OperationalError('episode storage unavailable')
    monkeypatch.setattr(observation, '_store', fail)
    clock[1] += 10000
    assert helpers.helpers.push(live, receiver).sent == 0
    assert 'episode storage unavailable' in caplog.text
    monkeypatch.setattr(observation, '_store', original)
    clock[1] += 10000
    helpers.poll(live, receiver)
    assert episodes(live)[0]['reachable_ms'] == 30000


def test_each_origin_has_independent_fingerprint_and_timing(live, clock):
    receiver = setup(live)
    receiver.entries = [helpers.entry(f'origin-{i}') for i in range(16)]
    helpers.poll(live, receiver)
    clock[1] += 30000
    receiver.entries[0] = helpers.entry('origin-0', digest='b' * 64)
    helpers.poll(live, receiver)
    assert len(episodes(live)) == 16
    assert {row['origin']: row['reachable_ms'] for row in episodes(live)} == {
        f'origin-{i}': 0 if i == 0 else 30000 for i in range(16)}


def test_failed_push_capability_request_interrupts_reachable_sampling(live, clock):
    receiver = setup(live)
    helpers.poll(live, receiver)
    clock[1] += 30000
    helpers.poll(live, receiver)
    original = receiver._call

    def offline(method, path, body=None):
        if path.endswith('/capability'):
            raise OSError('capability offline')
        return original(method, path, body)
    receiver._call = offline
    clock[1] += 10000
    with pytest.raises(OSError, match='capability offline'):
        helpers.helpers.push(live, receiver)
    receiver._call = original
    clock[1] += 10000
    helpers.poll(live, receiver)
    assert episodes(live)[0]['reachable_ms'] == 30000


def _other_process_failure(path):
    from pathlib import Path
    conn = kbc.connect(Path(path))
    try:
        receiver = helpers.Observing()
        receiver.failure = OSError('other process offline')
        assert helpers.poll(conn, receiver)['status'] == 'unavailable'
    finally:
        conn.close()


def test_other_process_failed_read_breaks_shared_sampling_baseline(live, clock):
    import multiprocessing
    receiver = setup(live)
    helpers.poll(live, receiver)
    clock[1] += 30000
    helpers.poll(live, receiver)
    path = live.execute('PRAGMA database_list').fetchone()[2]
    child = multiprocessing.get_context('spawn').Process(target=_other_process_failure, args=(path,))
    child.start()
    try:
        child.join(30)
        assert child.exitcode == 0
    finally:
        if child.is_alive():
            child.kill()
            child.join()
    clock[1] += 10000
    helpers.poll(live, receiver)
    assert episodes(live)[0]['reachable_ms'] == 30000
