"""The live replica memo keys on each scope heartbeat's freshness verdict, not the raw ``gateway/info`` rows.

The owner's collector rewrites ``gateway/info`` with a new ``observed_at`` every cycle while its task rows rarely
change; the projection reads that row only as per-scope freshness verdicts (release, moved evidence). Every case
compares the memoized live read with an explicit-``now`` read at the same wall time (it always reads every row).
"""
import json
import sqlite3
import threading

import pytest

from hermes_cli import kanban_scope_heartbeat as scope_hb
from hermes_cli import kanban_session_activity as activity
from tests.hermes_cli.test_kanban_remote_terminal_release import observed
from tests.hermes_cli import test_kanban_replica_read_memo as memo_helpers
from tests.hermes_cli.test_kanban_replica_read_memo import card, row_reads

hub = memo_helpers.hub
WINDOW = scope_hb.SCOPE_HEARTBEAT_FLOOR_S


def put_done(path, tid='t_done', seq=50):
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO replica VALUES ('owner','tasks',?,1,?,?,1000000)",
                   (f'work/build/{tid}', seq, json.dumps(observed(card(tid, status='done')))))


def set_info(path, scopes=None, *, raw=None, gw='owner', seq=900):
    data = raw if raw is not None else json.dumps({'gateway': gw, 'task_scopes': scopes})
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO replica VALUES (?,'gateway','info',1,?,?,0) ON CONFLICT(gw, coll, key) DO UPDATE"
                   " SET data=excluded.data", (gw, seq, data))


def tick(path, clock, now):
    """Move wall time with a hub pull landing just before it, so hub staleness never decides a case."""
    with sqlite3.connect(path) as db:
        db.execute("UPDATE meta SET v = ? WHERE k = 'last_ok'", (str(now),))
    clock['now'] = now


def ok(beat, board='build'):
    return {'profile': 'work', 'board': board, 'state': 'ok', 'observed_at': beat}


def read(path, clock):
    """(live, explicit, live outs, explicit outs) at the same instant."""
    outs = [(set(), set(), {}), (set(), set(), {})]
    live = activity._read_remote_links(path, qualified_out=outs[0][0], owners_out=outs[0][1], moved_out=outs[0][2])
    fresh = activity._read_remote_links(path, now=clock['now'], qualified_out=outs[1][0], owners_out=outs[1][1],
                                        moved_out=outs[1][2])
    assert live == fresh and outs[0] == outs[1]
    return live, outs[0]


def released(rows):
    return {r['id']: r['released'] for r in rows}


@pytest.fixture
def world(hub):
    path, put, statements, clock = hub
    put('t_open')
    put_done(path)
    set_info(path, [ok(1000.0)])
    return path, put, statements, clock


def test_heartbeat_timestamp_only_refresh_serves_the_memo(world):
    path, _, statements, clock = world
    first, _ = read(path, clock)
    assert released(first) == {'t_open': False, 't_done': True}
    for beat in (1030.0, 1060.0, 1090.0):  # the collector's ~30 s cycle: same verdicts, new timestamp
        set_info(path, [ok(beat)])
        tick(path, clock, beat + 1)
        statements.clear()
        assert activity._read_remote_links(path) == first
        assert row_reads(statements) == 0
        live, outs = read(path, clock)
        assert live == first and outs[2]['live_scopes'] == frozenset({('owner', 'work', 'build')})


def test_a_refreshed_heartbeat_extends_reuse_past_the_first_cutoff(world):
    path, _, statements, clock = world
    first, _ = read(path, clock)
    set_info(path, [ok(1400.0)])
    tick(path, clock, 1000.0 + WINDOW + 50)  # past the cut-off of the heartbeat the memo was built on
    statements.clear()
    assert activity._read_remote_links(path) == first
    assert row_reads(statements) == 0
    read(path, clock)


def test_expiry_flips_release_exactly_after_the_window(world):
    path, _, statements, clock = world
    read(path, clock)
    tick(path, clock, 1000.0 + WINDOW)  # fresh uses <=: still fresh at the boundary
    statements.clear()
    live, _ = read(path, clock)
    assert row_reads(statements) == 1 and released(live)['t_done'] is True
    tick(path, clock, 1000.0 + WINDOW + 0.001)
    statements.clear()
    live, outs = read(path, clock)
    assert row_reads(statements) == 2 and released(live)['t_done'] is False
    assert outs[2]['live_scopes'] == frozenset()


def test_future_dated_heartbeat_proves_nothing_until_it_is_reached(world):
    path, _, statements, clock = world
    set_info(path, [ok(1100.0)])
    live, _ = read(path, clock)
    assert released(live)['t_done'] is False
    tick(path, clock, 1050.0)
    statements.clear()
    assert released(read(path, clock)[0])['t_done'] is False and row_reads(statements) == 1
    tick(path, clock, 1100.0)
    statements.clear()
    assert released(read(path, clock)[0])['t_done'] is True and row_reads(statements) == 2


def test_clock_rollback_rereads(world):
    path, _, statements, clock = world
    tick(path, clock, 1200.0)
    read(path, clock)
    set_info(path, [ok(1200.0)])
    tick(path, clock, 1100.0)  # wall clock stepped back: the 1200 beat is future-dated now
    statements.clear()
    live, _ = read(path, clock)
    assert row_reads(statements) == 2 and released(live)['t_done'] is False


@pytest.mark.parametrize('scopes,raw', [
    ([ok(1010.0), ok(1010.0)], None),  # duplicate scope: ambiguous, proves nothing
    ([{**ok(1010.0), 'state': 'error'}], None),
    ([{**ok(1010.0), 'observed_at': True}], None),
    (None, '{not json'),
    (None, json.dumps({'gateway': 'someone-else', 'task_scopes': [ok(1010.0)]})),  # identity mismatch
])
def test_invalid_or_duplicate_scopes_reread_and_lose_release(world, scopes, raw):
    path, _, statements, clock = world
    read(path, clock)
    set_info(path, scopes, raw=raw)
    tick(path, clock, 1010.0)
    statements.clear()
    live, outs = read(path, clock)
    assert row_reads(statements) == 2 and released(live)['t_done'] is False
    assert outs[2]['live_scopes'] == frozenset()


def test_an_unrelated_scope_or_gateway_row_changes_nothing(world):
    path, _, statements, clock = world
    first, _ = read(path, clock)
    set_info(path, [ok(1000.0), {**ok(1005.0, board='other'), 'state': 'error'}])
    set_info(path, [], gw='bystander', seq=901)
    tick(path, clock, 1005.0)
    statements.clear()
    assert read(path, clock)[0] == first and row_reads(statements) == 1


def test_a_new_fresh_scope_rereads(world):
    """``live_scopes`` (moved evidence) gains a scope: a different answer, never served from the memo."""
    path, _, statements, clock = world
    read(path, clock)
    set_info(path, [ok(1000.0), ok(1005.0, board='other')])
    tick(path, clock, 1005.0)
    statements.clear()
    _, outs = read(path, clock)
    assert row_reads(statements) == 2 and ('owner', 'work', 'other') in outs[2]['live_scopes']


def test_task_mutation_alongside_a_heartbeat_refresh_rereads(world):
    path, put, statements, clock = world
    read(path, clock)
    set_info(path, [ok(1020.0)])
    put('t_open', status='blocked')
    tick(path, clock, 1020.0)
    statements.clear()
    live, _ = read(path, clock)
    assert row_reads(statements) == 2 and {r['id']: r['status'] for r in live}['t_open'] == 'blocked'


def test_gateway_identity_change_rereads(world):
    path, _, statements, clock = world
    read(path, clock)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE meta SET v = 'elsewhere' WHERE k = 'gateway'")
    statements.clear()
    live, _ = read(path, clock)
    assert row_reads(statements) == 2 and live == []


def test_reader_answers_exactly_while_heartbeat_writer_is_uncommitted(world):
    """A production WAL writer cannot exclude readers, even if descheduled before commit."""
    path, _, _, clock = world
    tick(path, clock, 1100.0)
    fresh_answer, _ = read(path, clock)
    set_info(path, [ok(2000.0)])
    future_answer, _ = read(path, clock)
    assert fresh_answer != future_answer
    writer = sqlite3.connect(path)
    try:
        writer.execute('BEGIN EXCLUSIVE')
        writer.execute("UPDATE replica SET data=? WHERE coll='gateway' AND key='info'",
                       (json.dumps({'gateway': 'owner', 'task_scopes': [ok(1000.0)]}),))
        # The last committed heartbeat remains visible while the writer holds its transaction.
        assert read(path, clock)[0] == future_answer
        writer.commit()
        assert read(path, clock)[0] == fresh_answer
    finally:
        writer.close()


def test_concurrent_reads_during_heartbeat_churn_answer_exactly(world):
    path, _, _, clock = world
    tick(path, clock, 1100.0)
    fresh_answer, _ = read(path, clock)
    set_info(path, [ok(2000.0)])  # future-dated: release lost
    future_answer, _ = read(path, clock)
    assert fresh_answer != future_answer
    answers, errors = [], []

    def reader():
        try:
            for _ in range(30):
                answers.append(activity._read_remote_links(path))
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def writer():
        for i in range(30):
            set_info(path, [ok(1000.0 + i)] if i % 2 else [ok(2000.0)])

    threads = [threading.Thread(target=reader) for _ in range(4)] + [threading.Thread(target=writer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert all(a in (fresh_answer, future_answer) for a in answers) and len(answers) == 120
