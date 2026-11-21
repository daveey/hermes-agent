"""Reviewer pins at the actual dispatcher spawn boundary: author models and missing author evidence never deny."""
import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_review_model as review
from tests.hermes_cli import test_kanban_review_implementer_set as helpers

board = helpers.board
clock = helpers.clock


@pytest.mark.parametrize('pin,mandatory,expected', [
    (helpers.OPUS, True, helpers.OPUS),
    (helpers.ASTRA, True, helpers.ASTRA),
    (helpers.ASTRA, False, helpers.ASTRA),
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
        # A reviewer pin that is also an author is used as-is (mandatory pins never move; a non-mandatory pin leads).
        assert spawned == [(expected, helpers.PROVIDER[expected])]
        event = helpers.pin_tests._events(conn, tid, 'model_tier')[-1]
        assert event['reason'].startswith('review_')
        assert event['review_independence'] == 'separate_execution'
        assert 'implementer_models' not in event
        # Attribution is still collected from the source run, just not used for admission.
        assert review.implementer_models(conn, helpers.kb.get_task(conn, tid)) == {helpers.ASTRA}


@pytest.mark.parametrize('pin', [None, helpers.ASTRA])
@pytest.mark.parametrize('missing', ['empty', 'unreadable', 'evicted_source', 'partial_sources'])
def test_downstream_missing_evidence_does_not_deny_the_spawn(
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
        # Missing author attribution is no refusal: the pin (or, unpinned, the strong route) spawns.
        expected = pin or helpers.OPUS
        assert helpers.pin_tests._tick(conn) == [(expected, helpers.PROVIDER[expected])]


def test_downstream_review_with_every_pool_walled_still_never_spawns(
        board, clock, monkeypatch, all_assignees_spawnable):
    """The real refusal: an unavailable route (every provider pool walled), not missing author metadata."""
    helpers._routing(monkeypatch)
    monkeypatch.setattr(helpers.walls, 'active_walls', lambda **_k: {
        name: float(clock[0] + 99999) for name in ('anthropic', 'openai-codex', 'xai-oauth')})
    with kbc.connect() as conn:
        tid = helpers._card(conn, None, title='Independent exact-head review')
        conn.commit()
        assert helpers.pin_tests._tick(conn) == []
        assert helpers.kb.get_task(conn, tid).consecutive_failures == 0
