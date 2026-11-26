"""Counts-only metadata equivalence: raw Cards provenance must remain literal."""
import json
import sqlite3
import time

import pytest

from hermes_cli import kanban_annotation_host as host
from hermes_cli import kanban_annotation_hub_key as hk
from hermes_cli import kanban_session_activity as ksa
from tests.hermes_cli import test_annotation_heartbeat_fence as fence
from tests.hermes_cli import test_kanban_annotation_parts as holders
from tests.hermes_cli import test_kanban_replica_read_memo as legacy

world = fence.world
replica = fence.replica
hub = legacy.hub


@pytest.mark.parametrize('heartbeat', [False, True])
def test_compound_session_metadata_suppresses_counts_with_real_watch_shape(world, replica, monkeypatch, record_property, heartbeat):
    path, conn, beat = replica
    # Compare snapshot values at one instant; fresh reads stamp local observation time.
    # The existing holder renewal bounds are unchanged by this experiment.
    legacy.fake_clock(monkeypatch, lambda: 1000.0)
    import types
    monkeypatch.setattr(hk, 'time', types.SimpleNamespace(time=lambda: 1000.0))
    from hermes_cli import kanban_annotation_parts as parts
    monkeypatch.setattr(parts, 'time', types.SimpleNamespace(time=lambda: 1000.0))
    monkeypatch.setattr(ksa, '_hub_db_path', lambda: path)
    paths = host._watch_paths
    monkeypatch.setattr(host, '_watch_paths', lambda scope: ([*paths(scope)[0], str(path)], paths(scope)[1]))
    conn.execute("INSERT INTO meta VALUES('last_ok',?)", (str(ksa.time.time() - 1),))
    for key in ['cursor', 'head', 'staged_at:sessions', 'watch_last_event']:
        conn.execute('INSERT INTO meta VALUES(?,?)', (key, '0'))
    beat(fence.info(999.0))
    holders._refresh()
    expected = holders._published()
    before = world.counts
    start = time.thread_time()
    for index in range(12):
        conn.execute('BEGIN')
        conn.execute('INSERT INTO sessions VALUES(?)', (index,))
        for key in ['cursor', 'head', 'staged_at:sessions', 'watch_last_event']:
            conn.execute('UPDATE meta SET v=? WHERE k=?', (str(index + 1), key))
        conn.execute("UPDATE meta SET v=? WHERE k='last_ok'", (str(ksa.time.time()),))
        conn.commit()
        if heartbeat:
            beat(fence.info(999.0 + (index + 1) / 100))
        holders._refresh()
        assert holders._published() == expected
    result = {'writes': 12, 'actual_count_reads': world.counts-before, 'cpu_s': time.thread_time()-start}
    record_property('metadata_work', json.dumps(result))
    assert world.counts == before, result
    world.conn.execute("UPDATE tasks SET status='done' WHERE id='t1'")
    holders._refresh()
    assert world.counts == before + 1 and holders._published()[0] == {}


@pytest.mark.parametrize('value', ['1001', '819', 'nan', 'inf', 'oops', '', None])
def test_last_ok_nonfresh_literal_boundary(replica, value):
    path, conn, _ = replica
    conn.execute("INSERT INTO meta VALUES('last_ok','999')")
    first, _ = hk.capture(path, 1000)
    conn.execute("UPDATE meta SET v=? WHERE k='last_ok'", (value,))
    assert hk.capture(path, 1000)[0] != first


def test_fresh_last_ok_is_counts_equivalent_but_other_meta_is_literal(replica):
    path, conn, _ = replica
    conn.execute("INSERT INTO meta VALUES('last_ok','999')")
    first, receipt = hk.capture(path, 1000)
    conn.execute("UPDATE meta SET v='1000' WHERE k='last_ok'")
    assert hk.capture(path, 1000)[0] == first
    assert not receipt.valid()
    conn.execute("INSERT INTO meta VALUES('unknown_future_dependency','one')")
    assert hk.capture(path, 1000)[0] != first
    conn.execute("DELETE FROM meta WHERE k='unknown_future_dependency'")
    conn.execute("INSERT INTO meta VALUES('offline_since','0')")
    assert hk.capture(path, 1000)[0] != first


def test_actual_legacy_cards_provenance_changes_but_full_count_snapshot_does_not(hub, monkeypatch):
    path, put, _, clock = hub
    put('t_one')
    with sqlite3.connect(path) as conn:
        conn.execute('UPDATE replica SET updated_at=0')
    # Real legacy projection and all actual count derivation; no fake Cards output.
    monkeypatch.setattr(ksa, '_read_local_pairs_into', lambda *args: set())
    monkeypatch.setattr(ksa, '_remote_cards', lambda *args: ksa._read_remote_links(path))
    scope = ('counts-metadata-contract', 'default')
    fingerprint = ('fixture-inputs',)
    first_cards = ksa._read_remote_links(path)
    first = ksa._recompute_all_counts(scope, time.monotonic(), fingerprint,
                                    include_waiting=True, include_snapshot=True, wall=clock['now'])
    assert first_cards and first_cards[0]['synced_at'] == 1000.0
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE meta SET v='1002' WHERE k='last_ok'")
    clock['now'] = 1002.5
    second_cards = ksa._read_remote_links(path, now=clock['now'])
    second = ksa._recompute_all_counts(scope, time.monotonic(), fingerprint,
                                     include_waiting=True, include_snapshot=True, wall=clock['now'])
    assert second_cards[0]['synced_at'] == 1002.0
    assert first_cards != second_cards
    assert first == second  # counters, IDs and timer/external markers
    assert first.execution == second.execution  # attribute, not part of tuple equality
    assert first.execution, 'execution oracle must not be vacuous'
