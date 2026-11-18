"""Count snapshot work under overlapping notice polls, not machine-dependent time."""
from concurrent.futures import ThreadPoolExecutor
import math
import threading

import sqlite3
import json
import pytest

from tests.waiting import wait_until
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
    # No memo can be published until every other caller has its snapshot and waits for the one persisting it.
    waiting, persisted = [], []

    class CountedEvent(threading.Event):
        def wait(self, timeout=None):
            waiting.append(threading.get_ident())  # list.append is atomic
            return super().wait(timeout)

    real_pending_init = obs._Pending.__init__

    def pending_init(self, token):
        real_pending_init(self, token)
        self.done = CountedEvent()

    def snapshot(*args):
        snapshots.append(lock.owned())
        return real_snapshot(*args)

    def ro(path):
        conn = real_ro(path)
        conn.set_trace_callback(lambda sql: statements.append((sql, lock.owned())))
        return conn

    def save(*args):
        persisted.append(threading.get_ident())
        wait_until(lambda: len(waiting) >= CALLERS - 1, timeout=20, what="snapshot followers")
        return real_persist(*args)

    def poll(_):
        start.wait(timeout=20)
        return obs.observe(replica, ledger, NOW)

    monkeypatch.setattr(obs._Pending, "__init__", pending_init)
    monkeypatch.setattr(obs, "_snapshot", snapshot)
    monkeypatch.setattr(krn, "_ro", ro)
    monkeypatch.setattr(obs, "_persist", save)
    with ThreadPoolExecutor(max_workers=CALLERS) as pool:
        results = list(pool.map(poll, range(CALLERS)))
    assert len(results[0]) == 20 and all(result == results[0] for result in results)
    assert statements
    assert sum(held for _, held in statements) == 0
    assert snapshots == [False]
    assert len(persisted) == 1 and len(waiting) == CALLERS - 1  # one ledger pass; the rest took its answer


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


class _LaneLock:
    """A replica lane lock that reports its owner and which threads are blocked on it."""

    def __init__(self, cond):
        self.lock, self.cond, self.waiting, self.owner = threading.Lock(), cond, set(), None

    def __enter__(self):
        me = threading.get_ident()
        if not self.lock.acquire(blocking=False):
            with self.cond:
                self.waiting.add(me)
                self.cond.notify_all()
            self.lock.acquire()
        with self.cond:
            self.waiting.discard(me)
            self.owner = me
            self.cond.notify_all()
        return self

    def __exit__(self, *args):
        with self.cond:
            self.owner = None
            self.lock.release()
            self.cond.notify_all()


class _GatedLock:
    """``_lock`` stand-in. After its snapshot, the gated poller enters only while it owns the replica lane and the
    other poller is blocked on that lane, or once the other poller has finished. A lane released before this
    poller's generation therefore lets the other poller run a whole observe first, without timing."""

    def __init__(self, cond, lane, other_done):
        self.lock, self.cond, self.lane, self.other_done = threading.Lock(), cond, lane, other_done
        self.gated: int | None = None

    def _ready(self):
        me = threading.get_ident()
        return self.other_done.is_set() or (self.lane.owner == me and bool(self.lane.waiting - {me}))

    def __enter__(self):
        if self.gated == threading.get_ident():
            with self.cond:
                assert self.cond.wait_for(self._ready, timeout=20)
        self.lock.acquire()
        return self

    def __exit__(self, *args):
        self.lock.release()

    def locked(self):
        return self.lock.locked()


class _Generations(dict):
    def __init__(self, order):
        super().__init__()
        self.order = order

    def __setitem__(self, key, value):
        self.order.append((threading.current_thread().name, value))
        super().__setitem__(key, value)


def test_lane_is_held_through_generation_so_older_snapshot_never_outranks_newer(tmp_path, monkeypatch):
    replica, ledger, cards = _world(tmp_path)
    cond, b_done = threading.Condition(), threading.Event()
    lane = _LaneLock(cond)
    gate = _GatedLock(cond, lane, b_done)
    flight = obs._replica_flight(str(replica.resolve()))  # Strong ref: the lane is weakly indexed.
    monkeypatch.setattr(flight, "lock", lane)
    order = []
    monkeypatch.setattr(obs, "_lock", gate)
    monkeypatch.setattr(obs, "_generation", _Generations(order))
    real_snapshot, a_read = obs._snapshot, threading.Event()

    def snapshot(*args):
        got = real_snapshot(*args)
        if threading.current_thread().name == "old":
            _move(replica, cards[0], "blocked", NOW + 1)  # A newer replica state lands after the old read.
            gate.gated = threading.get_ident()
            a_read.set()
        return got

    monkeypatch.setattr(obs, "_snapshot", snapshot)
    results = {}

    def poll(name, now, done=None):
        try:
            results[name] = obs.observe(replica, ledger, now)
        finally:
            if done is not None:
                with cond:
                    done.set()
                    cond.notify_all()

    old = threading.Thread(target=poll, args=("old", NOW), name="old")
    old.start()
    assert a_read.wait(20)
    new = threading.Thread(target=poll, args=("new", NOW + 2, b_done), name="new")
    new.start()
    old.join(20)
    new.join(20)
    assert not old.is_alive() and not new.is_alive()
    assert any(state.startswith("blocked@") for _, _, state in results["new"])
    assert not any(state.startswith("blocked@") for _, _, state in results["old"])
    # Generations follow lane (snapshot) order, so the newer snapshot's memo is the one published.
    assert order == [("old", 1), ("new", 2)]
    assert [entry[1] for entry in obs._memo.values()] == [results["new"]]


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
    assert not hasattr(obs, "_decoded")  # No module-level decode map outlives a replica lane.


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


def test_unknown_fingerprints_never_reuse_a_shared_snapshot(tmp_path, monkeypatch):
    from tui_gateway import kanban_replica_observer as observer

    replica, ledger, cards = _world(tmp_path)
    monkeypatch.setattr(observer, "fingerprint", lambda path: None)
    before = obs.observe(replica, ledger, NOW)
    _move(replica, cards[0], "blocked", NOW + 1)
    after = obs.observe(replica, ledger, NOW + 2)
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
