"""Reviewer pins must pass independence at the actual dispatcher spawn boundary."""
import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_review_model as review
from tests.hermes_cli import test_kanban_review_implementer_set as helpers

board = helpers.board
clock = helpers.clock


@pytest.mark.parametrize('pin,mandatory,expected', [
    (helpers.OPUS, True, helpers.OPUS),
    (helpers.ASTRA, True, None),
    (helpers.ASTRA, False, helpers.OPUS),
])
def test_downstream_pin_dispatch(board, clock, monkeypatch, all_assignees_spawnable, pin, mandatory, expected):
    helpers._routing(monkeypatch)
    with kbc.connect() as conn:
        tid = helpers._mandatory_downstream(conn, pin=pin)
        conn.execute('UPDATE tasks SET model_pin_mandatory=? WHERE id=?', (mandatory, tid))
        # The implementation is evidence, not another runnable candidate.
        conn.execute("UPDATE tasks SET status='done' WHERE id<>?", (tid,))
        conn.commit()
        spawned = helpers.pin_tests._tick(conn)
        assert spawned == ([] if expected is None else [(expected, helpers.PROVIDER[expected])])
        if expected:
            event = helpers.pin_tests._events(conn, tid, 'model_tier')[-1]
            assert event['reason'].startswith('review_')
            assert event['implementer_models'] == [helpers.ASTRA]


@pytest.mark.parametrize('pin', [None, helpers.ASTRA])
@pytest.mark.parametrize('missing', ['empty', 'unreadable', 'evicted_source', 'partial_sources'])
def test_downstream_missing_evidence_never_spawns_pin(
        board, clock, monkeypatch, all_assignees_spawnable, missing, pin):
    helpers._routing(monkeypatch)
    with kbc.connect() as conn:
        creator = helpers._card(conn, helpers.ASTRA) if missing in {'evicted_source', 'partial_sources'} else None
        tid = helpers._card(conn, pin, title='Independent exact-head review', creator=creator)
        if creator:
            if missing == 'partial_sources':
                other = helpers._card(conn, helpers.OPUS)
                helpers.kb.link_tasks(conn, tid, other)
            original_get = helpers.kb.get_task
            monkeypatch.setattr(helpers.kb, 'get_task',
                                lambda c, key: None if key == creator else original_get(c, key))
            conn.execute("UPDATE tasks SET status='done' WHERE id<>?", (tid,))
        conn.execute('UPDATE tasks SET model_pin_mandatory=1 WHERE id=?', (tid,))
        conn.commit()
        if missing == 'unreadable':
            import sqlite3

            def unavailable(*args):
                raise sqlite3.OperationalError('author evidence unavailable')

            monkeypatch.setattr(review, 'implementer_models', unavailable)
        assert helpers.pin_tests._tick(conn) == []
