"""Remembered rows of an owner the Board no longer carries at all are released as the owner last reported. [daveey]

2026-11-18: workers 2-8 were removed from the Board, which drops their records. The cards they had finished stayed
remembered ``unknown`` rows with nothing left to attest them: 1,231 in 68 hub chats and 1,409 in 28 MacBook chats.

Entry paths: the pure rule (``_release_removed_owners``) and ``_legacy_remote_links`` over a real replica file and a
real durable snapshot, through the memoized live read.
"""
import json
import sqlite3

import pytest

from hermes_cli import kanban_db
from hermes_cli import kanban_session_activity as activity
from hermes_cli.kanban_session_activity_records import OwnerSink, _release_removed_owners
from tests.hermes_cli import test_kanban_replica_read_memo as memo_helpers
from tests.hermes_cli.test_kanban_replica_read_memo import card
from tests.hermes_cli.test_kanban_replica_read_memo_heartbeat import ok, set_info, tick

hub = memo_helpers.hub
FRESH = {'published': {}, 'live_scopes': frozenset({('alive', 'work', 'build')})}


def row(tid, gateway, reported, **extra):
    return {'id': tid, 'session': 's1', 'gateway': gateway, 'key': f'{gateway}/work/build/{tid}', 'board': 'build',
            'status': 'unknown', 'reported_status': reported, 'unfinished': True, 'released': False,
            'ownership_state': 'qualified', 'stale': True, **extra}


def test_only_finished_rows_of_a_wholly_absent_owner_are_released():
    rows = [row('t_done', 'gone', 'done'), row('t_arch', 'gone', 'archived'), row('t_open', 'gone', 'running'),
            row('t_none', 'gone', None), row('t_unq', 'gone', 'done', ownership_state='unknown'),
            row('t_pub', 'publishes', 'done'), row('t_beat', 'alive', 'done')]
    out = _release_removed_owners(rows, {('publishes', 'build', 't_other')}, FRESH)
    got = {r['id']: (r['status'], r['released'], r['unfinished'], r.get('owner_removed', False)) for r in out}
    assert got == {'t_done': ('done', True, False, True), 't_arch': ('archived', True, False, True),
                   't_open': ('unknown', False, True, False), 't_none': ('unknown', False, True, False),
                   't_unq': ('unknown', False, True, False),
                   't_pub': ('unknown', False, True, False),   # its owner still publishes other cards: one record's
                   't_beat': ('unknown', False, True, False)}  # absence is never closure; nor with a fresh heartbeat
    assert rows[0]['status'] == 'unknown', 'the remembered rows are not edited in place'


def test_a_stale_or_empty_replica_releases_nothing():
    rows = [row('t_done', 'gone', 'done')]
    assert _release_removed_owners(rows, (), FRESH) == rows, 'no owner set and no heartbeat: the read proves nothing'
    assert _release_removed_owners(rows, {('publishes', 'build', 't_x')}, {}) == rows, 'stale: no moved evidence'
    assert _release_removed_owners(rows, (), {}) == rows


def test_owner_sink_takes_a_generator_once_and_borrows_a_set():
    sink = OwnerSink()
    assert tuple(sink.value) == ()
    owners = frozenset({('a', 'b', 't')})
    sink.update(owners)
    assert sink.value is owners
    sink.update(o for o in [('a', 'b', 't')])
    assert sink.value == {('a', 'b', 't')} and list(sink.value) == list(sink.value)


def put(path, gateway, tid, seq, **extra):
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO replica VALUES (?,'tasks',?,1,?,?,1000000)",
                   (gateway, f'work/build/{tid}', seq, json.dumps(card(tid, gateway=gateway, **extra))))


@pytest.fixture
def world(hub, tmp_path, monkeypatch):
    path, _put, _statements, clock = hub
    monkeypatch.setattr(kanban_db, 'kanban_home', lambda: tmp_path)
    monkeypatch.setattr(activity, '_hub_db_path', lambda: path)
    monkeypatch.setattr(activity, '_board_db_paths', list)
    activity._reset_cache_for_tests()
    # Neither "gone" nor "partial" ever beat, so their finished cards are remembered unreleased.
    put(path, 'gone', 't_done', 50, status='done')
    put(path, 'gone', 't_open', 51, status='running')
    put(path, 'partial', 't_dropped', 52, status='done')
    put(path, 'partial', 't_kept', 53, status='running')
    put(path, 'alive', 't_live', 54, status='running')
    set_info(path, [ok(1000.0)], gw='alive')
    return path, clock


def by_id(rows):
    return {r['id']: (r['status'], bool(r.get('released')), bool(r.get('stale'))) for r in rows}


def test_legacy_remote_links_release_a_removed_owners_finished_cards(world):
    path, clock = world
    first = by_id(activity._legacy_remote_links())
    assert first['t_done'] == ('unknown', False, False) and first['t_dropped'] == ('unknown', False, False)
    with sqlite3.connect(path) as db:  # the Board removes "gone" entirely, and one record of "partial"
        db.execute("DELETE FROM replica WHERE gw = 'gone'")
        db.execute("DELETE FROM replica WHERE gw = 'partial' AND key = 'work/build/t_dropped'")
    tick(path, clock, 1010.0)
    rows = activity._legacy_remote_links()
    got = by_id(rows)
    assert got['t_done'] == ('done', True, True), 'the removed owner last reported it done'
    assert got['t_open'] == ('running', False, True), 'its open card stays a remembered open row'
    assert got['t_dropped'] == ('unknown', False, True), 'an owner that still publishes keeps its unknown row'
    assert got['t_kept'] == ('running', False, False) and got['t_live'] == ('running', False, False)
    done = next(r for r in rows if r['id'] == 't_done')
    assert done['owner_removed'] is True and done['unfinished'] is False
    # The release is remembered: a later read and a cold process (the durable snapshot) both keep it.
    tick(path, clock, 1020.0)
    assert by_id(activity._legacy_remote_links())['t_done'] == ('done', True, True)
    saved = {r['id']: r for r in activity._remote_snapshot()}
    assert saved['t_done']['released'] is True and saved['t_dropped']['released'] is False


def test_a_stale_replica_keeps_the_rows_unknown(world):
    path, clock = world
    activity._legacy_remote_links()
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM replica WHERE gw = 'gone'")
    clock['now'] = 1000.0 + 40 * 3600  # no hub pull since: the replica is stale and proves nothing
    assert by_id(activity._legacy_remote_links())['t_done'] == ('unknown', False, True)
