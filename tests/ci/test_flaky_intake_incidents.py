"""Closed daily intake generations never reopen or delay fresh evidence."""
import pytest

from scripts.ci import flaky_intake as intake
from tests.ci.test_flaky_intake import Board, record


@pytest.mark.parametrize('status', ['done', 'archived'])
def test_closed_daily_incident_gets_new_generation(tmp_path, status):
    board = Board()
    journal = intake.Journal(tmp_path / 'queue.sqlite3')
    try:
        record(journal, run=1, count=1)
        journal.deliver('o/r', board)
        board.cards['card0']['status'] = status
        record(journal, run=2, count=1)
        journal.deliver('o/r', board)
        assert len(board.cards) == 2
        assert board.cards['card0']['status'] == status
        assert len(board.notes['card1']) == 1
        record(journal, run=3, count=1)
        journal.deliver('o/r', board)
        assert len(board.cards) == 2
        assert len(board.notes['card1']) == 2
        board.cards['card1']['status'] = status
        record(journal, run=4, count=1)
        journal.deliver('o/r', board)
        assert len(board.cards) == 3
        assert journal.db.execute('SELECT sum(delivered) FROM occurrences').fetchone()[0] == 4
    finally:
        journal.close()


def test_destination_refuses_unknown_status_and_foreign_cache():
    board = Board()
    key = 'ci-flaky-intake:o/r:2026-11-15'
    card = board.create('intake', '', key)
    board.cards[card]['status'] = 'unknown'
    with pytest.raises(RuntimeError, match='status unavailable'):
        intake._incident_destination(board, key, None, 'payload')
    foreign = board.create('foreign', '', key + ':other')
    with pytest.raises(RuntimeError, match='cached identity'):
        intake._incident_destination(board, key, foreign, 'payload')


def test_destination_matches_exact_subject_and_generation():
    board = Board()
    key = 'ci-flaky-intake:o/r:2026-11-15'
    board.create('other', '', key + '#other')
    board.create('other repo', '', key.replace('o/r', 'o/r2'))
    assert intake._incident_destination(board, key, None, 'payload') == (None, key, False)
    old = board.create('closed', '', key)
    board.cards[old]['status'] = 'done'
    live = board.create('open generation', '', key + '#1')
    assert intake._incident_destination(board, key, None, 'payload') == (live, key + '#1', False)


def test_closed_cached_page_reconciles_lost_ack_before_replacement(tmp_path):
    board = Board()
    journal = intake.Journal(tmp_path / 'queue.sqlite3')
    try:
        record(journal, count=1)
        journal.deliver('o/r', board)
        with journal.db:
            journal.db.execute('UPDATE pages SET delivered=0')
            journal.db.execute('UPDATE occurrences SET delivered=0')
        board.cards['card0']['status'] = 'done'
        journal.deliver('o/r', board)
        assert len(board.cards) == 1
        assert journal.db.execute('SELECT sum(delivered) FROM occurrences').fetchone()[0] == 1
    finally:
        journal.close()
