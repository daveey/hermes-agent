"""Behavioural regression pins for the local-key survivors from #3125."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_annotation_local as own
from hermes_cli import kanban_annotation_narrow as narrow
from tests.hermes_cli import test_kanban_annotation_narrow_local as helpers

hub = helpers.hub
world = helpers.world


def test_archived_cards_are_absent_from_board_rows(world):
    world["conn"].execute("UPDATE tasks SET status='archived' WHERE id='t1'")
    rows = own._board_rows(world["conn"])
    assert 't1' not in rows['tasks']
    assert 't2' in rows['tasks']


def test_patch_removes_both_ends_without_mutating_old_snapshot():
    rows = {'tasks': {}, 'subs': {}, 'edges': {('parent', 'changed'), ('changed', 'child'), ('a', 'b')}}
    fresh = {'tasks': {}, 'subs': {}, 'edges': {('new', 'changed')}}
    result = own._patched(rows, frozenset({'changed'}), fresh)
    assert result['edges'] == {('a', 'b'), ('new', 'changed')}
    assert rows['edges'] == {('parent', 'changed'), ('changed', 'child'), ('a', 'b')}


def test_session_negative_expires_on_same_database(world, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(own.time, 'monotonic', lambda: clock[0])
    assert own.profile_sessions({'later'}) == set()
    helpers._sessions(['later'])
    clock[0] += 299
    assert own.profile_sessions({'later'}) == set()
    clock[0] += 2
    assert own.profile_sessions({'later'}) == {'later'}


def test_narrow_reset_invalidates_session_negative(world):
    assert own.profile_sessions({'later'}) == set()
    helpers._sessions(['later'])
    assert own.profile_sessions({'later'}) == set()
    narrow.reset()
    assert own.profile_sessions({'later'}) == {'later'}


def test_board_part_contains_transitive_ancestors(world):
    world['conn'].execute("INSERT INTO task_links (parent_id, child_id) VALUES ('tx', 'tp')")
    rows = own._board_rows(world['conn'])
    part = own._board_part(rows, frozenset({'mine1'}))
    assert {row[0] for row in part[2]} == {'tp', 'tx'}


def test_shared_rows_keep_distinct_origin_parts(world):
    rows = own._board_rows(world['conn'])
    first = own._board_part(rows, frozenset({'mine1'}))
    second = own._board_part(rows, frozenset({'other'}))
    assert {row[0] for row in first[0]} == {'t1', 't2'}
    assert {row[0] for row in second[0]} == {'tp', 'tx'}
    assert own._board_part(rows, frozenset({'mine1'})) == first


@pytest.mark.parametrize('clock', [float('nan'), float('inf'), -float('inf')])
def test_nonfinite_observed_clock_is_rejected(clock):
    rows = (('gateway', json.dumps({'observed_at': clock})),)
    with pytest.raises(ValueError, match='nonfinite clock'):
        narrow._infos(rows, 1000.0)
    assert narrow._infos((('gateway', '{"observed_at": 999}'),), 1000.0)


def test_stat_only_replica_key_never_reuses_old_rows(world):
    from hermes_cli import kanban_chat_links as links

    conn = world['sconn']
    conn.row_factory = sqlite3.Row
    content = ('stat', (1, 2, 3, 4), 0, 0)
    try:
        before = links.replica_link_rows(conn, world['shadow'], helpers.ME, helpers.PROFILE, content)
        assert [(r['id'], r['status']) for r in before] == [('r1', 'running')]
        conn.execute("UPDATE tasks SET status='blocked' WHERE id='r1'")
        after = links.replica_link_rows(conn, world['shadow'], helpers.ME, helpers.PROFILE, content)
        assert [(r['id'], r['status']) for r in after] == [('r1', 'blocked')]
        assert before[0]['status'] == 'running'
    finally:
        conn.row_factory = None
        links._reset_for_tests()
