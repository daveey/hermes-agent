"""A finished remote card stays released when its owner goes silent after publishing it. [daveey]

Hub chat 20261110_185031_4f5f4b, 2026-11-18: workers 2, 5 and 7 were shut down on 2026-11-16. Every card they had
finished (2,376 records, each published before the worker's last scope heartbeat) read "done, release pending" in its
origin chat from then on, because release required a heartbeat inside the freshness window at read time.

Entry paths: the pure rule, ``owner_attests_terminal``, the explicit-``now`` full projection and the memoized live
(incremental) read, each against the same replica file; the reconciler's ledger link (``_ledger_links``) through the
projection it reads.
"""
import json
import sqlite3

import pytest

from hermes_cli import kanban_scope_heartbeat as scope_hb
from hermes_cli import kanban_session_activity as activity
from hermes_cli.kanban_session_activity_release import owner_attests_terminal
from tests.hermes_cli.test_kanban_remote_terminal_release import observed
from tests.hermes_cli import test_kanban_replica_read_memo as memo_helpers
from tests.hermes_cli.test_kanban_replica_read_memo import card
from tests.hermes_cli.test_kanban_replica_read_memo_heartbeat import ok, read, released, set_info, tick

hub = memo_helpers.hub
WINDOW = scope_hb.SCOPE_HEARTBEAT_FLOOR_S
SCOPE = ('owner', 'work', 'build')
LONG = 40 * 3600.0  # the workers had been down about 38 h


def put_done(path, tid, updated_s, seq, status='done'):
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO replica VALUES ('owner','tasks',?,1,?,?,?)",
                   (f'work/build/{tid}', seq, json.dumps(observed(card(tid, status=status))), int(updated_s * 1000)))


@pytest.mark.parametrize('last_beat, now, published_at, expected', [
    (1000.0, 5000.0, 900.0, True),      # published, then the owner read the scope cleanly, then silence
    (1000.0, 5000.0, 1000.0, True),     # the same instant counts: the beat follows the publication in one cycle
    (1000.0, 5000.0, 1000.001, False),  # published after the last clean read: no beat stands behind it
    (6000.0, 5000.0, 900.0, False),     # a future-dated beat proves nothing
    (1000.0, 5000.0, 0.0, False),       # no publication time
    (1000.0, 5000.0, None, False),
    (None, 5000.0, 900.0, False),       # the owner never beat
    (True, 5000.0, 900.0, False),       # bool is not a clock
])
def test_published_before_silence(last_beat, now, published_at, expected):
    assert scope_hb.published_before_silence(last_beat, now, published_at) is expected


def test_scope_verdicts_carry_quiet_beats_and_key_on_them():
    beats = {SCOPE: (1000.0, WINDOW), ('live', 'work', 'build'): (4990.0, WINDOW), ('ahead', 'work', 'build'): (9000.0, WINDOW)}
    v = scope_hb.fresh_scopes(beats, 5000.0)
    assert set(v) == {('live', 'work', 'build')} and v.quiet == {(SCOPE, 1000.0)}  # a future beat is in neither
    assert scope_hb.attests(v, ('live', 'work', 'build'), 4995.0)       # fresh: the publication time is not read
    assert scope_hb.attests(v, SCOPE, 900.0) and not scope_hb.attests(v, SCOPE, 1001.0)
    assert not scope_hb.attests(v, ('ahead', 'work', 'build'), 900.0)
    assert not scope_hb.attests(frozenset(), SCOPE, 900.0)             # a plain set knows no quiet beat
    same = scope_hb.fresh_scopes(beats, 5001.0)
    assert v == same and hash(v) == hash(same)                         # a silent owner's key does not move
    moved = scope_hb.fresh_scopes({**beats, SCOPE: (1001.0, WINDOW)}, 5000.0)
    assert v != moved and frozenset(v) == frozenset(moved)             # the quiet beat is part of the key
    assert v != frozenset(v) and scope_hb.ScopeVerdicts(v) == frozenset(v)


def test_owner_attests_terminal_for_a_silent_owner():
    task = observed({**card('t_done', status='done'), 'gateway': 'owner', 'profile': 'work', 'board': 'build'})
    from hermes_cli.kanban_origin_ledger import _owner_evidence
    evidence = _owner_evidence(task)
    args = (task, 'owner', 'work', 'build', {SCOPE: (1000.0, WINDOW)})
    assert owner_attests_terminal(*args, 1000.0 + LONG, False, evidence, published_at=900.0) is True
    assert owner_attests_terminal(*args, 1000.0 + LONG, False, evidence, published_at=1500.0) is False
    assert owner_attests_terminal(*args, 1000.0 + LONG, False, evidence) is False, 'no publication time: the old rule'
    assert owner_attests_terminal(*args, 1000.0 + LONG, True, evidence, published_at=900.0) is False, 'stale hub sync'
    assert owner_attests_terminal(*args, 1000.0 + LONG, False, {}, published_at=900.0) is False, 'no owner evidence'
    open_task = {**task, 'status': 'running'}
    assert owner_attests_terminal(open_task, *args[1:], 1000.0 + LONG, False, evidence, published_at=900.0) is False


@pytest.fixture
def world(hub):
    path, put, statements, clock = hub
    put('t_open')
    put_done(path, 't_before', 900.0, 50)
    put_done(path, 't_same', 1000.0, 51)
    put_done(path, 't_after', 1000.5, 52)
    put_done(path, 't_archived', 950.0, 53, status='archived')
    set_info(path, [ok(1000.0)])
    return path, put, statements, clock


def test_live_and_explicit_reads_keep_a_silent_owners_releases(world):
    path, _, _, clock = world
    live, _ = read(path, clock)  # owner alive: every finished card is released
    assert released(live) == {'t_open': False, 't_before': True, 't_same': True, 't_after': True, 't_archived': True}
    tick(path, clock, 1000.0 + WINDOW + 0.001)  # the first instant past the window
    live, outs = read(path, clock)              # ``read`` asserts the memoized live read equals the full one
    assert released(live) == {'t_open': False, 't_before': True, 't_same': True, 't_after': False, 't_archived': True}
    assert outs[2]['live_scopes'] == frozenset()
    tick(path, clock, 1000.0 + LONG)            # a day and a half later nothing has changed
    live, _ = read(path, clock)
    assert released(live) == {'t_open': False, 't_before': True, 't_same': True, 't_after': False, 't_archived': True}
    after = next(r for r in live if r['id'] == 't_after')
    assert (after['status'], after['reported_status'], after['unfinished']) == ('unknown', 'done', True)


def test_a_returning_owner_that_reopened_the_card_unreleases_it(world):
    path, _, _, clock = world
    tick(path, clock, 1000.0 + LONG)
    assert released(read(path, clock)[0])['t_before'] is True
    with sqlite3.connect(path) as db:  # the owner comes back and publishes the card as running again
        db.execute("UPDATE replica SET data = ?, seq = 60, version = 2, updated_at = ? WHERE key = 'work/build/t_before'",
                   (json.dumps(card('t_before', status='running')), int((1000.0 + LONG) * 1000)))
    set_info(path, [ok(1000.0 + LONG)], seq=901)
    tick(path, clock, 1000.0 + LONG + 1)
    row = next(r for r in read(path, clock)[0] if r['id'] == 't_before')
    assert (row['status'], row['released']) == ('running', False)


def test_a_stale_hub_sync_releases_nothing(world):
    path, _, _, clock = world
    clock['now'] = 1000.0 + LONG  # no hub pull since: the replica itself is stale
    rows = activity._read_remote_links(path, now=clock['now'])
    assert not any(r['released'] for r in rows)


def test_an_owner_with_no_heartbeat_releases_nothing(hub):
    path, _put, _, clock = hub
    put_done(path, 't_before', 900.0, 50)
    tick(path, clock, 1000.0 + LONG)
    assert released(read(path, clock)[0]) == {'t_before': False}
    set_info(path, [{**ok(1000.0), 'state': 'error'}])  # a scope the collector could not read proves nothing
    assert released(read(path, clock)[0]) == {'t_before': False}
