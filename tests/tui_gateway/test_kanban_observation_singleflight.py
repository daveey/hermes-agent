"""Count snapshot work under overlapping notice polls, not machine-dependent time."""
from concurrent.futures import ThreadPoolExecutor
import math
import threading

import sqlite3
import json
import pytest

from tests.tui_gateway.test_kanban_remote_arrival import CHAT, _card
from tests.tui_gateway.test_kanban_remote_observations_lock import NOW, _move, _world
from tui_gateway import kanban_remote_notices as krn
from tui_gateway import kanban_remote_observations as obs

# Folded capture has no thread IDs. 992.15 poller thread-seconds / 83 sampled
# seconds gives 12 concurrent-equivalent callers, not an exact thread census.
CALLERS = math.ceil(992.15 / 83.0)


class OwnedLock:
    def __init__(self):
        self.lock = threading.Lock()
        self.owner = None

    def __enter__(self):
        self.lock.acquire()
        self.owner = threading.get_ident()
        return self

    def __exit__(self, *args):
        self.owner = None
        self.lock.release()

    def locked(self):
        return self.lock.locked()

    def owned(self):
        return self.owner == threading.get_ident()


def test_concurrent_snapshot_is_singleflight_outside_publication_lock(tmp_path, monkeypatch):
    replica, ledger, _ = _world(tmp_path)
    lock = OwnedLock()
    monkeypatch.setattr(obs, "_lock", lock)
    snapshots, statements = [], []
    real_snapshot, real_ro, real_persist = obs._snapshot, krn._ro, obs._persist
    start = threading.Barrier(CALLERS)
    # No memo can be published until every caller has obtained its snapshot.
    persist = threading.Barrier(CALLERS)

    def snapshot(*args):
        snapshots.append(lock.owned())
        return real_snapshot(*args)

    def ro(path):
        conn = real_ro(path)
        conn.set_trace_callback(lambda sql: statements.append((sql, lock.owned())))
        return conn

    def save(*args):
        persist.wait(timeout=20)
        return real_persist(*args)

    def poll(_):
        start.wait(timeout=20)
        return obs.observe(replica, ledger, NOW)

    monkeypatch.setattr(obs, "_snapshot", snapshot)
    monkeypatch.setattr(krn, "_ro", ro)
    monkeypatch.setattr(obs, "_persist", save)
    with ThreadPoolExecutor(max_workers=CALLERS) as pool:
        results = list(pool.map(poll, range(CALLERS)))
    assert len(results[0]) == 20 and all(result == results[0] for result in results)
    assert statements
    assert sum(held for _, held in statements) == 0
    assert snapshots == [False]


def test_shared_snapshot_matches_uncached_path_across_changes(tmp_path, monkeypatch):
    replica, ledger, cards = _world(tmp_path)
    oracle_ledger = tmp_path / "oracle.db"
    second_ledger = tmp_path / "second.db"
    snapshots = []
    original = obs._snapshot

    def counted(*args):
        snapshots.append(args[0])
        return original(*args)

    monkeypatch.setattr(obs, "_snapshot", counted)
    for step in range(4):
        if step == 1:
            card = _card("t_insert", CHAT, status="done", at=NOW + 1)
            with sqlite3.connect(replica) as conn:
                conn.execute("INSERT INTO replica VALUES ('hub','tasks',?,?,100)",
                             (card["key"], json.dumps(card["task"])))
        if step == 2:
            _move(replica, cards[0], "blocked", NOW + 2)
        if step == 3:
            with sqlite3.connect(replica) as conn:
                conn.execute("UPDATE replica SET data='broken json', seq=seq+1000 WHERE key=?",
                             (cards[1]["key"],))
        now = NOW + step
        ready, raw, _ = original(replica, None, {})
        assert ready
        expected = obs._persist(str(replica.resolve()), raw, oracle_ledger, now)
        assert obs.observe(replica, ledger, now) == expected
        assert obs.observe(replica, second_ledger, now) == expected
        assert len(snapshots) == step + 1


def test_slow_replica_does_not_hold_publication_or_other_replica(tmp_path, monkeypatch):
    (tmp_path / "slow").mkdir()
    (tmp_path / "other").mkdir()
    replica, ledger, _ = _world(tmp_path / "slow")
    other, other_ledger, _ = _world(tmp_path / "other")
    entered, release = threading.Event(), threading.Event()
    original = obs._snapshot

    def held(path, *args):
        if path == replica:
            entered.set()
            assert release.wait(20)
        return original(path, *args)

    monkeypatch.setattr(obs, "_snapshot", held)
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(obs.observe, replica, ledger, NOW)
        try:
            assert entered.wait(20)
            assert obs._lock.acquire(timeout=5)
            obs._lock.release()
            assert len(pool.submit(obs.observe, other, other_ledger, NOW).result(20)) == 20
        finally:
            release.set()
        assert len(slow.result(20)) == 20


def test_real_newer_snapshot_wins_over_delayed_ledger_write(tmp_path, monkeypatch):
    replica, ledger, cards = _world(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = obs._persist

    def delayed(source, rows, ledger_, now):
        if now == NOW:
            entered.set()
            assert release.wait(20)
        return original(source, rows, ledger_, now)

    monkeypatch.setattr(obs, "_persist", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(obs.observe, replica, ledger, NOW)
        try:
            assert entered.wait(20)
            _move(replica, cards[0], "blocked", NOW + 1)
            newer = obs.observe(replica, ledger, NOW + 2)
        finally:
            release.set()
        assert old.result(20) != newer
    assert obs.observe(replica, ledger, NOW + 3) == newer
    assert next(iter(obs._memo.values()))[1] == newer


def test_failed_snapshot_releases_lane_and_retries(tmp_path, monkeypatch):
    replica, ledger, _ = _world(tmp_path)
    original = obs._snapshot

    def failed(*args):
        raise sqlite3.OperationalError("controlled failure")

    monkeypatch.setattr(obs, "_snapshot", failed)
    with pytest.raises(sqlite3.OperationalError, match="controlled failure"):
        obs.observe(replica, ledger, NOW)
    monkeypatch.setattr(obs, "_snapshot", original)
    assert len(obs.observe(replica, ledger, NOW)) == 20


def test_idle_lane_bound_retains_active_lane_identity():
    krn._reset_for_tests()
    active = obs._replica_flight("active")
    for i in range(20):
        obs._replica_flight(str(i))
    assert len(obs._retained) == 16
    assert "active" not in obs._retained
    assert obs._replica_flight("active") is active
    assert len(obs._flights) == 16


def test_signature_reuse_still_supplies_cards_to_a_new_ledger(tmp_path):
    from tui_gateway import kanban_replica_observer as observer

    replica, ledger, _ = _world(tmp_path)
    expected = obs.observe(replica, ledger, NOW)
    observer.reset()  # New observer generation, same task signature.
    assert obs.observe(replica, tmp_path / "new-ledger.db", NOW) == expected
    flight = obs._replica_flight(str(replica.resolve()))
    assert len(flight.decoded) == 20
    assert obs._decoded == {}  # Observe never uses the legacy direct-helper cache.


def test_same_signature_refresh_does_not_repeat_persistence(tmp_path, monkeypatch):
    from tui_gateway import kanban_replica_observer as observer

    replica, ledger, _ = _world(tmp_path)
    expected = obs.observe(replica, ledger, NOW)
    observer.reset()

    def forbidden(*args):
        pytest.fail("unchanged signature repeated persistence")

    monkeypatch.setattr(obs, "_persist", forbidden)
    assert obs.observe(replica, ledger, NOW) == expected


def test_missing_sequence_signature_never_reuses_an_old_memo(tmp_path):
    replica, ledger, cards = _world(tmp_path)
    with sqlite3.connect(replica) as conn:
        conn.execute("ALTER TABLE replica DROP COLUMN seq")
    before = obs.observe(replica, ledger, NOW)
    moved = _card("t_bulk_0", CHAT, status="blocked", at=NOW + 2)
    with sqlite3.connect(replica) as conn:
        conn.execute("UPDATE replica SET data=? WHERE key=?", (json.dumps(moved["task"]), cards[0]["key"]))
    after = obs.observe(replica, ledger, NOW + 3)
    assert after != before
    assert any(state.startswith("blocked@") for _, _, state in after)


def test_unknown_after_fingerprint_forces_a_fresh_snapshot(tmp_path, monkeypatch):
    from tui_gateway import kanban_replica_observer as observer

    replica, ledger, _ = _world(tmp_path)
    original, read = observer.fingerprint, obs._snapshot
    calls, snapshots = [], []

    def fingerprint(path):
        calls.append(path)
        return None if len(calls) == 2 else original(path)

    def counted(*args):
        snapshots.append(args[0])
        return read(*args)

    monkeypatch.setattr(observer, "fingerprint", fingerprint)
    monkeypatch.setattr(obs, "_snapshot", counted)
    expected = obs.observe(replica, ledger, NOW)
    assert obs._memo == {}
    assert obs.observe(replica, ledger, NOW) == expected
    assert len(snapshots) == 2


def test_replacement_during_snapshot_forgets_decode_identity(tmp_path, monkeypatch):
    from tui_gateway import kanban_replica_observer as observer

    replica, _, _ = _world(tmp_path)
    stamp = observer.fingerprint(replica)
    ident = observer.file_identity(stamp)
    flight = obs._replica_flight(str(replica.resolve()))
    after = stamp._replace(ident=(stamp.ident[0], stamp.ident[1] + 1))
    monkeypatch.setattr(observer, "fingerprint", lambda path: after)
    ready, cards, _, retained_stamp, retained_identity = flight.read(replica, stamp, ident)
    assert ready and len(cards) == 20
    assert retained_stamp is None
    assert retained_identity is None
