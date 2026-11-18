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
from hermes_cli.kanban_session_activity_records import _release_removed_owners
from tests.hermes_cli import test_kanban_replica_read_memo as memo_helpers
from tests.hermes_cli.test_kanban_replica_read_memo import card
from tests.hermes_cli.test_kanban_replica_read_memo_heartbeat import ok, set_info, tick

hub = memo_helpers.hub
FRESH = {'published': {}, 'live_scopes': frozenset({('alive', 'work', 'build')}),
         'known_gateways': frozenset({'alive', 'other'}), 'publishing': frozenset({'publishes'})}


def row(tid, gateway, reported, **extra):
    return {'id': tid, 'session': 's1', 'gateway': gateway, 'key': f'{gateway}/work/build/{tid}', 'board': 'build',
            'status': 'unknown', 'reported_status': reported, 'unfinished': True, 'released': False,
            'ownership_state': 'qualified', 'stale': True, **extra}


def test_only_finished_rows_of_a_wholly_absent_owner_are_released():
    rows = [row('t_done', 'gone', 'done'), row('t_arch', 'gone', 'archived'), row('t_open', 'gone', 'running'),
            row('t_none', 'gone', None), row('t_unq', 'gone', 'done', ownership_state='unknown'),
            row('t_pub', 'publishes', 'done'), row('t_beat', 'alive', 'done')]
    out = _release_removed_owners(rows, FRESH)
    got = {r['id']: (r['status'], r['released'], r['unfinished'], r.get('owner_removed', False)) for r in out}
    assert got == {'t_done': ('done', True, False, True), 't_arch': ('archived', True, False, True),
                   't_open': ('unknown', False, True, False), 't_none': ('unknown', False, True, False),
                   't_unq': ('unknown', False, True, False),
                   't_pub': ('unknown', False, True, False),   # its owner still publishes other cards: one record's
                   't_beat': ('unknown', False, True, False)}  # absence is never closure; nor with a fresh heartbeat
    assert rows[0]['status'] == 'unknown', 'the remembered rows are not edited in place'


def test_a_stale_replica_releases_nothing():
    rows = [row('t_done', 'gone', 'done')]
    assert _release_removed_owners(rows, {}) == rows, 'stale: no moved evidence'


def put(path, gateway, tid, seq, **extra):
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO replica VALUES (?,'tasks',?,1,?,?,1000000)",
                   (gateway, f'work/build/{tid}', seq, json.dumps(card(tid, gateway=gateway, **{'created_at': 100 + seq, **extra}))))


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


def _removed_then_released(path, clock):
    activity._legacy_remote_links()
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM replica WHERE gw = 'gone'")
    tick(path, clock, 1010.0)
    assert by_id(activity._legacy_remote_links())['t_done'] == ('done', True, True)


@pytest.mark.parametrize('cold', [False, True])
def test_an_owner_that_publishes_again_takes_the_release_back(world, cold):
    """Review 5952178127: the release is an inference re-made on every fresh read, not a receipt."""
    path, clock = world
    _removed_then_released(path, clock)
    put(path, 'gone', 't_new', 60, status='running')  # the machine is back and publishes another card
    tick(path, clock, 1020.0)
    if cold:
        activity._reset_cache_for_tests()  # a new process: only the durable snapshot remembers the release
    rows = activity._legacy_remote_links()
    assert by_id(rows)['t_done'] == ('unknown', False, True)
    done = next(r for r in rows if r['id'] == 't_done')
    assert 'owner_removed' not in done and done['unfinished'] is True and done['reported_status'] == 'done'
    with sqlite3.connect(path) as db:  # removed once more: released once more
        db.execute("DELETE FROM replica WHERE gw = 'gone'")
    tick(path, clock, 1030.0)
    assert by_id(activity._legacy_remote_links())['t_done'] == ('done', True, True)


@pytest.mark.parametrize('cold', [False, True])
def test_a_reprovisioned_owner_republishing_the_card_running_without_links_takes_it_back(world, cold):
    path, clock = world
    _removed_then_released(path, clock)
    with sqlite3.connect(path) as db:  # the exact card, running again, its session links dropped
        db.execute("INSERT INTO replica VALUES ('gone','tasks','work/build/t_done',2,61,?,1015000)",
                   (json.dumps(card('t_done', gateway='gone', status='running', sessions=[])),))
    tick(path, clock, 1020.0)
    if cold:
        activity._reset_cache_for_tests()
    assert by_id(activity._legacy_remote_links())['t_done'] == ('unknown', False, True)


def test_an_owner_back_with_a_clean_scope_and_no_such_card_keeps_it_closed_as_aged_out(world):
    """The machine beats again and reads the card's exact scope cleanly without the card: a collector publishes
    every card of a scope except those archived past its window, so the card is closed. It is no longer
    ``owner_removed`` (the owner is back); it is ``aged_out``."""
    path, clock = world
    _removed_then_released(path, clock)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO replica VALUES ('gone','gateway','info',1,62,?,0)",
                   (json.dumps({'gateway': 'gone', 'task_scopes': [ok(1015.0)]}),))
    tick(path, clock, 1020.0)
    rows = activity._legacy_remote_links()
    done = next(r for r in rows if r['id'] == 't_done')
    assert by_id(rows)['t_done'] == ('done', True, True) and done.get('aged_out') is True and 'owner_removed' not in done
    assert by_id(rows)['t_open'] == ('running', False, True), 'its open card is still a remembered open row'


@pytest.mark.parametrize('scope', [{**ok(1015.0), 'state': 'error'}, ok(1015.0, board='other'), ok(1.0)])
def test_an_owner_back_without_a_clean_fresh_read_of_that_scope_takes_the_release_back(world, scope):
    path, clock = world
    _removed_then_released(path, clock)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO replica VALUES ('gone','gateway','info',1,62,?,0)",
                   (json.dumps({'gateway': 'gone', 'task_scopes': [scope]}),))
        db.execute("INSERT INTO replica VALUES ('gone','tasks','work/other/t_else',1,63,?,1015000)",
                   (json.dumps(card('t_else', gateway='gone', board='other', status='running', sessions=[])),))
    tick(path, clock, 1020.0)
    assert by_id(activity._legacy_remote_links())['t_done'] == ('unknown', False, True)


def test_a_stale_read_keeps_an_earlier_release(world):
    path, clock = world
    _removed_then_released(path, clock)
    clock['now'] = 1010.0 + 40 * 3600
    assert by_id(activity._legacy_remote_links())['t_done'] == ('done', True, True)


def test_released_rows_are_restored_by_the_pure_rule_when_the_owner_is_listed_again():
    released = _release_removed_owners([row('t_done', 'gone', 'done')], FRESH)
    assert released[0]['owner_removed'] is True
    assert _release_removed_owners(released, FRESH) == released, 'stable while it is gone'
    back = _release_removed_owners(released, {**FRESH, 'known_gateways': FRESH['known_gateways'] | {'gone'}})
    assert back == [row('t_done', 'gone', 'done')]
    assert _release_removed_owners(released, {}) == released, 'a stale read neither makes nor takes back'


def test_without_the_list_of_known_gateways_nothing_counts_as_removed():
    rows = [row('t_done', 'gone', 'done')]
    evidence = {k: v for k, v in FRESH.items() if k != 'known_gateways'}
    assert _release_removed_owners(rows, evidence) == rows
    listed = {**FRESH, 'known_gateways': FRESH['known_gateways'] | {'gone'}}
    assert _release_removed_owners(rows, listed) == rows, 'offline, not removed'


def test_a_malformed_created_at_proves_nothing_and_does_not_raise():
    rows = [row('t_bad', 'gone', 'ready', created_at=[1]), row('t_live', 'alive', 'done', created_at=[1], owner_profile='work')]
    assert _release_removed_owners(rows, FRESH) == rows


def test_a_live_owners_closed_card_that_left_its_scope_is_released_as_aged_out(world):
    """Hub, 2026-11-18: 18 MacBook cards last reported archived, their records aged out of publication, stayed
    remembered unknown rows although the MacBook was alive and publishing that scope."""
    path, clock = world
    put(path, 'alive', 't_arch', 70, status='archived')
    put(path, 'alive', 't_busy', 71, status='running')
    with sqlite3.connect(path) as db:  # never attested: no observation, so the rows are remembered unknown/open
        pass
    first = by_id(activity._legacy_remote_links())
    assert first['t_arch'] == ('unknown', False, False)
    with sqlite3.connect(path) as db:  # the archived card ages out of publication; so does the running card's record
        db.execute("DELETE FROM replica WHERE gw = 'alive' AND key IN ('work/build/t_arch', 'work/build/t_busy')")
    set_info(path, [ok(1010.0)], gw='alive', seq=901)
    tick(path, clock, 1011.0)
    rows = activity._legacy_remote_links()
    got = by_id(rows)
    assert got['t_arch'] == ('archived', True, True)
    assert next(r for r in rows if r['id'] == 't_arch')['aged_out'] is True
    assert got['t_busy'] == ('running', False, True), 'a card last reported open is never closed by absence'
    # The owner's heartbeat goes stale: the inference is not made (and an earlier one is taken back).
    tick(path, clock, 1010.0 + 40 * 3600)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE meta SET v = ? WHERE k = 'last_ok'", (str(clock['now']),))
    assert by_id(activity._legacy_remote_links())['t_arch'] == ('unknown', False, True)


def test_a_removed_owners_copy_of_a_card_another_gateway_publishes_is_retired(world):
    """A worker node published its copy of a hub card as its own. With the worker removed, the row named an owner
    that no longer exists while the real owner published the card all along."""
    path, clock = world
    put(path, 'gone', 't_copy', 80, status='ready', created_at=555)
    put(path, 'gone', 't_copy_done', 81, status='done', created_at=556)
    put(path, 'gone', 't_other_time', 82, status='ready', created_at=557)
    with sqlite3.connect(path) as db:  # the real owner's records link no session of this origin
        for seq, (tid, created) in enumerate([('t_copy', 555), ('t_copy_done', 556), ('t_other_time', 999)]):
            db.execute("INSERT INTO replica VALUES ('alive','tasks',?,1,?,?,1000000)",
                       (f'work/build/{tid}', 90 + seq, json.dumps(card(tid, gateway='alive', status='running',
                                                                    created_at=created, sessions=[]))))
    first = {r['id'] for r in activity._legacy_remote_links() if r['gateway'] == 'gone'}
    assert {'t_copy', 't_copy_done', 't_other_time'} <= first
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM replica WHERE gw = 'gone'")
    tick(path, clock, 1010.0)
    rows = [r for r in activity._legacy_remote_links() if r['gateway'] == 'gone']
    ids = {r['id'] for r in rows}
    assert 't_copy' not in ids and 't_copy_done' not in ids, 'the real owner publishes them: the copies are retired'
    assert 't_other_time' in ids, 'a different created_at is a different card'
    assert 't_open' in ids, 'no other gateway publishes it: it stays a remembered open row'
