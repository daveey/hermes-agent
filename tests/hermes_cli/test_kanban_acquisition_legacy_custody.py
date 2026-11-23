"""Legacy fallback and borrowed-reader controls for the owned acquisition seam."""
import sqlite3
from pathlib import Path

from hermes_cli import kanban_contained_wait as contained
from hermes_cli import kanban_session_acquisition as acquisition
from hermes_cli import kanban_session_activity as activity
from hermes_cli import kanban_session_activity_card_details as details
from tests.hermes_cli import test_kanban_contained_strand as containment_fixtures
from tests.hermes_cli import test_kanban_session_outcome_tasks as selection_fixtures

board = containment_fixtures.board


def test_legacy_containment_reads_missing_authority_from_discovered_replica(board, monkeypatch):
    relay, authority, _ = containment_fixtures._contained(board)
    board.execute('UPDATE tasks SET id=? WHERE id=?', ('moved-authority', authority))
    calls = []

    def replica_status(ids):
        calls.append(ids)
        return {authority: ('done', 99)}

    monkeypatch.setattr(contained, '_replica_status', replica_status)
    result = contained.read(board, [relay])
    assert result[relay]['authority'] == {'id': authority, 'status': 'done', 'at': 99}
    assert any(authority in ids for ids in calls)


def test_legacy_enrichment_keeps_optional_helper_call_contract(tmp_path, monkeypatch):
    with selection_fixtures._connect(tmp_path / 'board.db') as conn:
        conn.row_factory = sqlite3.Row
        selection_fixtures._insert_task(conn, 'card', 'ready', 'scope')
        found = {'card': {'status': 'ready'}}
        calls = []

        def card_details(reader, rows):
            assert reader is conn and rows is found
            calls.append('details')

        def wait_info(reader, rows):
            assert reader is conn and rows is found
            calls.append('wait')
            return {'card': {'kind': 'legacy-control'}}

        monkeypatch.setattr(activity, '_add_card_details', card_details)
        monkeypatch.setattr(activity, '_local_wait_info', wait_info)
        acquisition.enrich_session_tasks(conn, Path('board.db'), found,
                                         observed_at=100, scope=('fixture', 'default'), replica=...)
        assert calls == ['details', 'wait']
        assert found['card']['wait'] == {'kind': 'legacy-control'}


def test_borrowed_detail_replica_remains_open_across_all_chunks(tmp_path):
    with selection_fixtures._connect(tmp_path / 'board.db') as conn:
        conn.row_factory = sqlite3.Row
        with sqlite3.connect(':memory:') as replica:
            found = {f'card-{n}': {'status': 'done'} for n in range(1100)}
            details._add_card_details(conn, found, replica=replica)
            assert replica.execute('SELECT 1').fetchone() == (1,)


def test_legacy_details_without_replica_is_a_valid_local_read(tmp_path, monkeypatch):
    with selection_fixtures._connect(tmp_path / 'board.db') as conn:
        conn.row_factory = sqlite3.Row
        monkeypatch.setattr(details, '_replica_for', lambda _conn: None)
        found = {'card': {'status': 'done'}}
        details._add_card_details(conn, found)
        assert found['card']['status'] == 'done'
        details._add_blocked_upstream(conn, found)


def test_explicit_none_never_attempts_status_lookup_on_absent_reader(tmp_path, monkeypatch):
    with selection_fixtures._connect(tmp_path / 'board.db') as conn:
        conn.execute('ALTER TABLE tasks ADD COLUMN completed_at REAL')
        readers = []
        original = contained._local_status

        def local_status(reader, ids):
            readers.append(reader)
            return original(reader, ids)

        monkeypatch.setattr(contained, '_local_status', local_status)
        assert contained.statuses(conn, ['missing-authority'], replica=None) == {}
        assert readers == [conn]


def test_legacy_wait_age_uses_wall_clock_not_owned_observation(board, monkeypatch):
    from hermes_cli import kanban_wait_info as waits

    relay, authority, _ = containment_fixtures._contained(board)
    now = containment_fixtures.NOW
    containment_fixtures._finish(board, authority, at=now - 3600)
    monkeypatch.setattr(waits.time, 'time', lambda: now)
    monkeypatch.setattr(details, '_replica_for', lambda _conn: None)
    legacy = {relay: {'status': 'triage'}}
    owned = {relay: {'status': 'triage'}}
    for rows, replica in [(legacy, ...), (owned, None)]:
        acquisition.enrich_session_tasks(board, Path('board.db'), rows, observed_at=now - 3600,
                                         scope=('fixture', 'default'), replica=replica)
    assert legacy[relay]['wait']['wait_state'] == 'stalled'
    assert owned[relay]['wait']['wait_state'] == 'waiting'
