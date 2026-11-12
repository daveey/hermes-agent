"""A live remote-links miss reads and re-projects only the replica rows whose ``seq`` moved. [daveey]

hermes-hub's replica holds ~13k task rows and changes every ~30 s, so the remote-links memo misses all the time.
Every case here drives a 12k-row hub replica through inserts, updates, deletes, a malformed row and owner-scope
freshness changes, and after each step compares the live (incremental) read with an explicit-``now`` read, which
always reads and projects every row through ``_project_replica``. The work counters pin that a step touches the
changed rows only; nothing here measures wall time.
"""
import json
import sqlite3

import pytest

from hermes_cli import kanban_replica_projection as projection
from hermes_cli import kanban_replica_rows as replica_rows
from hermes_cli import kanban_session_activity as activity
from tests.hermes_cli.test_kanban_remote_terminal_release import observed
from tests.hermes_cli import test_kanban_replica_read_memo as _memo_tests
from tests.hermes_cli.test_kanban_replica_read_memo import card

hub = _memo_tests.hub  # the 12k-row world builds on that file's hub replica fixture
N = 12000


def _scopes(path, scopes, seq):
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO replica VALUES ('owner','gateway','info',1,?,?,0) ON CONFLICT(gw, coll, key)"
                   " DO UPDATE SET data=excluded.data, seq=excluded.seq",
                   (seq, json.dumps({'gateway': 'owner', 'task_scopes': scopes})))


def _ok(beat, board='build'):
    return {'profile': 'work', 'board': board, 'state': 'ok', 'observed_at': beat}


class World:
    def __init__(self, path, clock):
        self.path, self.clock, self.seq = path, clock, 100

    def write(self, sql, args=()):
        with sqlite3.connect(self.path) as db:
            db.execute(sql, args)

    def put(self, key, data, gw='owner', updated=1_000_000):
        self.seq += 1
        self.write("INSERT INTO replica VALUES (?,'tasks',?,1,?,?,?) ON CONFLICT(gw, coll, key) DO UPDATE"
                   " SET version=version+1, seq=excluded.seq, data=excluded.data, updated_at=excluded.updated_at",
                   (gw, key, self.seq, data, updated))

    def delete(self, key, gw='owner'):
        self.write("DELETE FROM replica WHERE gw = ? AND coll = 'tasks' AND key = ?", (gw, key))

    def tick(self, now):
        self.write("UPDATE meta SET v = ? WHERE k = 'last_ok'", (str(now),))
        self.clock['now'] = now

    def both(self):
        """(live, explicit, live outs, explicit outs) at one instant; the explicit read is the full path."""
        outs = [(set(), set(), {}), (set(), set(), {})]
        live = activity._read_remote_links(self.path, qualified_out=outs[0][0], owners_out=outs[0][1],
                                           moved_out=outs[0][2])
        full = activity._read_remote_links(self.path, now=self.clock['now'], qualified_out=outs[1][0],
                                           owners_out=outs[1][1], moved_out=outs[1][2])
        return live, full, outs[0], outs[1]

    def same(self):
        replica_rows.stats.update(dict.fromkeys(replica_rows.stats, 0))
        projection.stats.update(dict.fromkeys(projection.stats, 0))
        live, full, live_outs, full_outs = self.both()
        assert live == full and live_outs == full_outs
        return live


def _task(i, **extra):
    t = card(f't_{i}', **extra)
    if i % 4:  # most rows link another origin's session: projected, never cards here
        t['sessions'] = [{'gw': 'elsewhere', 'profile': 'default', 'session': 's9', 'rel': 'shepherd'}]
    return t


@pytest.fixture
def world(hub):
    path, _, statements, clock = hub
    w = World(path, clock)
    with sqlite3.connect(path) as db:
        db.executemany("INSERT INTO replica VALUES (?,'tasks',?,1,?,?,1000000)",
                       [('owner', f'work/build/t_{i}', i + 1, json.dumps(_task(i))) for i in range(N)])
    _scopes(path, [_ok(1000.0)], seq=N + 50)
    w.seq = N + 100
    return w, statements


def test_steps_through_inserts_updates_deletes_and_scope_changes_equal_the_full_read(world):
    w, _ = world
    first = w.same()
    assert replica_rows.stats['full'] == 1 and projection.stats['projected'] == N  # the cold read projects all
    assert len(first) == N // 4

    w.put('work/build/t_8', json.dumps(_task(8, status='blocked')))  # update, linked
    w.put('work/build/t_9', json.dumps(_task(9, title='renamed')))  # update, unlinked
    w.put('work/build/t_new', json.dumps(card('t_new')))  # insert
    w.tick(1005.0)
    rows = w.same()
    assert {r['id']: r['status'] for r in rows}['t_8'] == 'blocked' and 't_new' in {r['id'] for r in rows}
    assert replica_rows.stats == {'full': 0, 'delta': 1, 'delta_rows': 3, 'identity': 0}
    assert projection.stats['projected'] == 3
    assert projection.stats['historical'] == 2  # the two linked rows; the unlinked t_9 makes no card
    assert projection.stats['considered'] == len(rows)  # only rows linking this origin are looked at

    w.delete('work/build/t_12')  # delete a linked row
    w.delete('work/build/t_13')
    w.tick(1010.0)
    rows = w.same()
    assert not {'t_12', 't_13'} & {r['id'] for r in rows}
    assert replica_rows.stats['full'] == 0 and replica_rows.stats['identity'] == 1
    assert projection.stats['projected'] == 0

    w.put('work/build/t_16', json.dumps(observed(card('t_16', status='done'))))  # owner attests it closed
    w.tick(1015.0)
    rows = w.same()
    assert {r['id']: r['released'] for r in rows}['t_16'] is True and projection.stats['projected'] == 1

    _scopes(w.path, [], seq=w.seq + 1)  # the owner scope goes away: the release no longer holds
    w.seq += 1
    w.tick(1020.0)
    rows = w.same()
    assert {r['id']: r['released'] for r in rows}['t_16'] is False
    assert (projection.stats['projected'], projection.stats['rebuilt'], projection.stats['historical']) == (0, 1, 0)
    assert replica_rows.stats['delta_rows'] == 0


def test_a_malformed_row_fails_like_the_full_read_and_recovers(world):
    w, _ = world
    w.same()
    w.put('work/build/t_20', '{bad')
    w.tick(1005.0)
    with pytest.raises(ValueError):
        activity._read_remote_links(w.path)
    with pytest.raises(ValueError):
        activity._read_remote_links(w.path, now=w.clock['now'])
    w.put('work/build/t_20', json.dumps(_task(20, status='blocked')))
    w.tick(1010.0)
    assert {r['id']: r['status'] for r in w.same()}['t_20'] == 'blocked'
    assert replica_rows.stats['full'] == 0 and projection.stats['projected'] == 1


def test_legacy_and_qualified_twins_fold_like_the_full_read(world):
    """A frozen ``board/id`` record folds its shepherd link into its finished qualified twin (an unlinked row)."""
    w, _ = world
    w.same()
    same_card = {'created_at': 500, 'created_by': 'u'}
    w.put('work/build/t_5', json.dumps(observed({**_task(5, status='done'), **same_card})))  # released
    w.put('build/t_5', json.dumps({**card('t_5'), 'profile': None, **same_card}))
    w.put('build/t_legacy', json.dumps({**card('t_legacy'), 'profile': None}))
    w.tick(1005.0)
    rows = w.same()
    assert [r['legacy_key'] for r in rows if r['id'] == 't_5'] == ['owner/build/t_5']
    assert any(r['id'] == 't_legacy' and r['owner_profile'] is None for r in rows)
    w.delete('build/t_5')  # the legacy record goes: t_5 links no session of this origin any more
    w.tick(1010.0)
    assert 't_5' not in {r['id'] for r in w.same()}


def test_a_gateways_newest_row_leaving_moves_its_synced_at_back(world):
    w, _ = world
    w.tick(3000.0)
    w.same()
    w.put('work/build/t_late', json.dumps(card('t_late')), updated=2_500_000)
    w.tick(3005.0)
    assert {r['synced_at'] for r in w.same()} == {2500.0}
    w.delete('work/build/t_late')
    w.tick(3010.0)
    assert {r['synced_at'] for r in w.same()} == {1000.0}


def test_this_origins_own_cards_are_not_remote(world):
    w, _ = world
    w.same()
    own = {**card('t_own'), 'gateway': 'origin', 'profile': 'default'}
    w.put('default/build/t_own', json.dumps(own), gw='origin')
    w.put('other/build/t_peer', json.dumps({**own, 'id': 't_peer', 'profile': 'other'}), gw='origin')
    w.tick(1005.0)
    ids = {r['id'] for r in w.same()}
    assert 't_own' not in ids and 't_peer' in ids


def test_a_row_rewritten_at_an_older_seq_or_a_replaced_file_reads_in_full(world):
    w, _ = world
    w.same()
    w.write("UPDATE replica SET seq = 5, data = ? WHERE key = 'work/build/t_40'",
            (json.dumps(_task(40, status='blocked')),))
    w.tick(1005.0)
    rows = w.same()
    assert {r['id']: r['status'] for r in rows}['t_40'] == 'blocked'
    assert replica_rows.stats['full'] == 1  # the delta could not explain it

    w.put('work/build/t_44', json.dumps(_task(44, status='blocked')))
    w.write("UPDATE replica SET data = ?, seq = 7 WHERE key = 'work/build/t_48'",
            (json.dumps(_task(48, status='blocked')),))
    real = replica_rows.file_ident
    # Same path, another file (st_dev, st_ino): nothing kept from the old file's rows is trusted.
    replica_rows.file_ident = lambda path: (real(path), 'replaced')
    try:
        w.tick(1010.0)
        rows = w.same()
    finally:
        replica_rows.file_ident = real
    status = {r['id']: r['status'] for r in rows}
    assert status['t_48'] == 'blocked' and status['t_44'] == 'blocked'
    assert replica_rows.stats['full'] == 1 and replica_rows.stats['delta'] == 0


def test_rows_rewritten_in_place_are_never_trusted(world):
    """Writes that bypass the feed's seq (same seq, or seqs swapped between rows) read in full."""
    w, _ = world
    w.same()
    w.write("UPDATE replica SET data = ? WHERE key = 'work/build/t_60'", (json.dumps(_task(60, title='renamed')),))
    w.tick(1005.0)
    assert {r['id']: r['title'] for r in w.same()}['t_60'] == 'renamed'
    assert replica_rows.stats['full'] == 1 and replica_rows.stats['identity'] == 1
    assert projection.stats['projected'] == 1  # a full read still re-projects only the row whose text moved

    w.put('work/build/t_64', json.dumps(_task(64, title='x' * 10)))
    w.put('work/build/t_68', json.dumps(_task(68, title='y' * 10)))
    w.tick(1005.0)
    w.same()
    s64, s68 = w.seq - 1, w.seq  # swap the titles and the seqs: every total stays put
    w.write("UPDATE replica SET data = ?, seq = ? WHERE key = 'work/build/t_64'",
            (json.dumps(_task(64, title='y' * 10)), s68))
    w.write("UPDATE replica SET data = ?, seq = ? WHERE key = 'work/build/t_68'",
            (json.dumps(_task(68, title='x' * 10)), s64))
    w.delete('work/build/t_72')  # so the totals differ and the identity aggregate runs
    w.tick(1010.0)
    titles = {r['id']: r['title'] for r in w.same()}
    assert titles['t_64'] == 'y' * 10 and titles['t_68'] == 'x' * 10
    assert replica_rows.stats['full'] == 1


def test_a_replica_without_a_file_identity_is_read_in_full_every_time(world, monkeypatch):
    w, _ = world
    monkeypatch.setattr(replica_rows, 'file_ident', lambda path: None)
    w.same()
    w.put('work/build/t_76', json.dumps(_task(76, status='blocked')))
    w.tick(1005.0)
    assert {r['id']: r['status'] for r in w.same()}['t_76'] == 'blocked'
    assert replica_rows.stats == {'full': 1, 'delta': 0, 'delta_rows': 0, 'identity': 0}


def test_a_stale_hub_answers_like_the_full_read(world):
    w, _ = world
    w.put('work/build/t_80', json.dumps(observed(card('t_80', status='done'))))
    w.tick(1000.0)
    assert {r['id']: r['released'] for r in w.same()}['t_80'] is True
    w.clock['now'] = 1000.0 + activity.HUB_STALE_AFTER_S + 5  # no hub pull since: stale, nothing is released
    rows = w.same()
    assert {r['id']: r['released'] for r in rows}['t_80'] is False and all(r['stale'] for r in rows)
    assert projection.stats['projected'] == 0


def test_a_row_without_seq_is_never_read_incrementally(world):
    w, _ = world
    w.same()
    w.write("UPDATE replica SET seq = NULL WHERE key = 'work/build/t_52'")
    w.tick(1005.0)
    w.same()
    assert not any(replica_rows.stats.values())  # the plain full read, not the incremental reader
