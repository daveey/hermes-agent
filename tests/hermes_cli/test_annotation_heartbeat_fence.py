"""Outer-only heartbeat keys retain a same-connection publication fence."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_annotation_hub_key as hk
from hermes_cli import kanban_annotation_parts as parts
from hermes_cli import kanban_session_activity as ksa
from tests.hermes_cli import test_kanban_annotation_parts as holders

world = holders.world
NOW = 1000.0


def info(beat=999.0):
    return {'gateway': 'owner', 'task_scopes': [
        {'profile': 'default', 'board': 'default', 'state': 'ok', 'observed_at': beat}],
        'scope_heartbeat_period_s': 200.0}


@pytest.fixture
def replica(tmp_path):
    path = tmp_path / 'hub.db'
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript("""PRAGMA journal_mode=WAL;
        CREATE TABLE replica(gw TEXT,coll TEXT,key TEXT,seq INTEGER,data TEXT);
        CREATE TABLE meta(k TEXT PRIMARY KEY,v TEXT);
        INSERT INTO meta VALUES('gateway','self');
        INSERT INTO replica VALUES('owner','tasks','default/default/t1',1,'{}');
        INSERT INTO replica VALUES('owner','gateway','info',2,'{}');
        CREATE TABLE sessions(n INTEGER);
    """)
    def beat(value):
        conn.execute("UPDATE replica SET data=?,seq=seq+1 WHERE coll='gateway'", (json.dumps(value),))
    beat(info())
    yield path, conn, beat
    conn.close()
    ksa._reset_cache_for_tests()


def test_fresh_heartbeat_only_is_stable_but_raw_inner_signature_moves(replica):
    path, _, beat = replica
    a, receipt = hk.capture(path, NOW)
    assert receipt.valid()
    raw = ksa._hub_content_signature(path)
    beat(info(1000.0))
    b, _ = hk.capture(path, NOW)
    assert a == b
    assert raw != ksa._hub_content_signature(path)
    assert not receipt.valid()


@pytest.mark.parametrize('change', ['future', 'stale', 'period', 'unknown', 'duplicate', 'state', 'board'])
def test_non_equivalent_gateway_evidence_moves_key(replica, change):
    path, _, beat = replica
    a, _ = hk.capture(path, NOW)
    changed = info()
    scope = changed['task_scopes'][0]
    actions = {
        'future': lambda: scope.update(observed_at=NOW + 1),
        'stale': lambda: scope.update(observed_at=NOW - 501),
        'period': lambda: changed.update(scope_heartbeat_period_s=201.0),
        'unknown': lambda: scope.update(new_field=True),
        'duplicate': lambda: changed['task_scopes'].append(dict(scope)),
        'state': lambda: scope.update(state='error'),
        'board': lambda: scope.update(board='other'),
    }
    actions[change]()
    beat(changed)
    assert hk.capture(path, NOW)[0] != a


def test_freshness_is_rechecked_without_database_commit(replica):
    path, _, _ = replica
    fresh, _ = hk.capture(path, NOW)
    assert hk.capture(path, NOW + 499)[0] == fresh
    assert hk.capture(path, NOW + 500)[0] != fresh
    assert hk.capture(path, 998.0)[0] != fresh


def test_raw_receipt_rejects_session_only_commit_and_gateway_aba(replica):
    path, conn, beat = replica
    a, original = hk.capture(path, NOW)
    conn.execute('INSERT INTO sessions VALUES(1)')
    assert not original.valid()
    _, start = hk.capture(path, NOW)
    beat(info(NOW + 5))
    middle, _ = hk.capture(path, NOW)
    beat(info())
    assert hk.capture(path, NOW)[0] == a and middle != a
    assert not start.valid()


@pytest.mark.parametrize('middle', [998.0, 1005.0])
def test_real_holder_rejects_consumed_aba_and_forces_next_read(world, replica, monkeypatch, middle):
    path, _, beat = replica
    monkeypatch.setattr(ksa, '_hub_db_path', lambda: path)
    # Keep all holder deadlines and renewal rules real; only the wall clock is frozen.
    monkeypatch.setattr(hk.time, 'time', lambda: NOW)
    read = ksa._recompute_all_counts
    calls = []
    def crossing(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            beat(info(middle))
            value = read(*args, **kwargs)
            beat(info())
            return value
        return read(*args, **kwargs)
    monkeypatch.setattr(ksa, '_recompute_all_counts', crossing)
    holders._refresh()
    assert holders.SCOPE not in parts._seen, 'racing result received a normalized certificate'
    holders._refresh()
    assert len(calls) == 2, 'a matching late probe certified the rejected result'
    holders._refresh()
    assert len(calls) == 2
    beat(info(1000.0))
    holders._refresh()
    assert len(calls) == 2, 'fresh heartbeat caused a full read'
    world.conn.execute("UPDATE tasks SET status='done' WHERE id='t1'")
    holders._refresh()
    assert len(calls) == 3 and holders._published()[0] == {}
