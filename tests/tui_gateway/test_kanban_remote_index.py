"""The host's published remote notice index (``kanban_remote_index``): readers of the replica's current state read
their own rows instead of rebuilding the index and re-running ``observe``; any miss computes locally. [daveey]"""
import json
import os
import shutil
import sqlite3
import threading
import time

import pytest

from tests.tui_gateway.test_kanban_remote_arrival import _hub
from tests.tui_gateway.test_kanban_remote_notices import CHAT, OTHER, _card
from tests.waiting import wait_until
from tui_gateway import kanban_remote_index as kri
from tui_gateway import kanban_remote_notices as krn
from tui_gateway import kanban_remote_observations as obs

NOW = 1_800_000_000.0
PARENT = "20261020_101010_aaaaaa"


@pytest.fixture(autouse=True)
def _fresh_memos():
    krn._reset_for_tests()
    yield
    krn._reset_for_tests()


def _linked(tid, *sessions, status="done", at=NOW - 60):
    card = _card(tid, sessions[0], status=status, at=at)
    card["task"]["sessions"] = [{"gw": "macbook", "profile": "default", "session": sid, "rel": "shepherd"}
                                for sid in sessions]
    return card


def _cards():
    return [_linked("t_done", CHAT), _linked("t_blocked", CHAT, status="blocked"),
            _linked("t_old", CHAT, at=NOW - 10 * 3600), _linked("t_shared", CHAT, OTHER),
            _linked("t_parent", PARENT), _linked("t_other", OTHER)]


def _home(tmp_path, cards):
    tmp_path.mkdir(parents=True, exist_ok=True)
    replica = _hub(tmp_path, cards)
    with sqlite3.connect(replica) as conn:
        conn.execute("PRAGMA journal_mode=wal")
    return replica, tmp_path / "kanban" / "remote-notices.db"


def _add(replica, card, seq):
    with sqlite3.connect(replica) as conn:
        conn.execute("INSERT INTO replica VALUES ('hub','tasks',?,?,?)", (card["key"], json.dumps(card["task"]), seq))


def _collect(replica, ledger, session=CHAT, lineage=(PARENT,), now=NOW):
    return krn.collect(session, "default", hub_db=replica, ledger=ledger, now=now, lineage=lineage)


def _norm(items):
    return json.dumps(items, sort_keys=True, default=str)


def _forbid_local_build(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("a reader of the published state rebuilt the index or re-ran observe")
    monkeypatch.setattr(krn, "_index", forbidden)
    monkeypatch.setattr(obs, "observe", forbidden)


def _count_local_builds(monkeypatch):
    calls, real = [], krn._index
    monkeypatch.setattr(krn, "_index", lambda path: calls.append(path) or real(path))
    return calls


def test_reader_hit_returns_the_same_notices_as_local_compute(tmp_path, monkeypatch):
    replica, ledger = _home(tmp_path / "home", _cards())
    assert _collect(replica, ledger, session=OTHER, lineage=())  # this process builds and publishes
    assert kri.published_key(ledger) == kri.replica_key(replica)
    twin = tmp_path / "twin"
    shutil.copytree(tmp_path / "home", twin)  # same replica and ledger state, computed locally below
    krn._reset_for_tests()  # another backend process on this host

    with monkeypatch.context() as local:
        local.setattr(kri, "lookup", lambda *args, **kwargs: None)
        local.setattr(kri, "publish", lambda *args, **kwargs: False)
        twin_ledger = twin / "kanban" / "remote-notices.db"
        expected_index = krn._chat_index(twin / "hub.db", twin_ledger, NOW, "default", (CHAT, PARENT))
        expected = _collect(twin / "hub.db", twin_ledger)
    krn._reset_for_tests()

    _forbid_local_build(monkeypatch)
    me, by_sid, observed = krn._chat_index(replica, ledger, NOW, "default", (CHAT, PARENT))
    assert (me, by_sid) == expected_index[:2]
    from tui_gateway.kanban_remote_notice_state import state
    mine = {(card["gw"], card["key"], state(card["task"])) for cards in by_sid.values() for card in cards}
    assert len(mine) == 5 and set(observed) == mine
    assert dict(observed) == {ident: expected_index[2][ident] for ident in mine}
    assert observed.first_seen == {ident: at for ident, at in expected_index[2].first_seen.items() if ident in mine}
    got = _collect(replica, ledger)
    assert len(got) == 4 and _norm(got) == _norm(expected)
    assert kri.stats["hits"] >= 1


def test_stale_key_computes_locally_and_publishes(tmp_path, monkeypatch):
    replica, ledger = _home(tmp_path, _cards())
    _collect(replica, ledger)
    old = kri.published_key(ledger)
    krn._reset_for_tests()
    _add(replica, _linked("t_new", CHAT), 50)
    builds = _count_local_builds(monkeypatch)
    got = _collect(replica, ledger)
    assert [item["ref"] for item in got] == ["hub/default/default/t_new"]
    assert builds, "a stale published copy must not be read"
    assert kri.published_key(ledger) == kri.replica_key(replica) != old


def test_publish_writes_only_the_rows_that_changed(tmp_path):
    replica, ledger = _home(tmp_path, _cards())
    _collect(replica, ledger)
    first = kri.stats["rows_written"]
    assert first >= len(_cards())
    _add(replica, _linked("t_new", OTHER), 50)
    _collect(replica, ledger)
    assert kri.stats["rows_written"] - first == 1


@pytest.mark.parametrize("damage", ["missing", "corrupt", "wrong_ledger"])
def test_missing_or_corrupt_copy_computes_locally(tmp_path, monkeypatch, damage):
    replica, ledger = _home(tmp_path, _cards())
    _collect(replica, ledger, session=OTHER, lineage=())
    krn._reset_for_tests()
    db = kri.index_path(ledger)
    if damage == "missing":
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(f"{db}{suffix}"):
                os.unlink(f"{db}{suffix}")
    elif damage == "corrupt":
        for suffix in ("-wal", "-shm"):
            if os.path.exists(f"{db}{suffix}"):
                os.unlink(f"{db}{suffix}")
        db.write_bytes(b"not a database" * 512)
    else:
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE meta SET v = '[\"elsewhere\", null]' WHERE k = 'ledger'")
    builds = _count_local_builds(monkeypatch)
    got = _collect(replica, ledger)
    assert len(got) == 4 and builds
    assert kri.published_key(ledger) == kri.replica_key(replica)  # the reader repaired the copy


def test_no_index_io_or_wait_under_the_collect_lock(tmp_path, monkeypatch):
    replica, ledger = _home(tmp_path, _cards())
    _collect(replica, ledger, session=OTHER, lineage=())
    krn._reset_for_tests()
    _add(replica, _linked("t_new", CHAT), 50)
    seen = []
    for name in ("_read", "_build_lock", "_open_rw", "replica_key", "published_key"):
        real = getattr(kri, name)

        def checked(*args, _real=real, _name=name, **kwargs):
            seen.append(_name)
            assert not krn._lock.locked(), f"{_name} ran under kanban_remote_notices._lock"
            return _real(*args, **kwargs)
        monkeypatch.setattr(kri, name, checked)
    db = kri.index_path(ledger)
    holder = os.open(f"{db}.lock", os.O_RDWR | os.O_CREAT)
    import fcntl
    fcntl.flock(holder, fcntl.LOCK_EX)  # another process is publishing right now
    writer = sqlite3.connect(db, timeout=0)
    writer.execute("BEGIN EXCLUSIVE")
    try:
        started, result = time.monotonic(), []
        poll = threading.Thread(target=lambda: result.append(_collect(replica, ledger)), daemon=True)
        poll.start()
        poll.join(5.0)
        assert result, "collect waited on the index's build lock or write transaction"
        assert time.monotonic() - started < 2.0
        got = result[0]
        assert {item["ref"] for item in got} >= {"hub/default/default/t_new"}
        assert {"replica_key", "_build_lock"} <= set(seen)
        assert kri.stats["busy"] == 1
    finally:
        writer.rollback()
        writer.close()
        os.close(holder)


def test_notice_reaches_the_chat_on_its_next_poll_with_the_holder_dead(tmp_path):
    replica, ledger = _home(tmp_path, _cards())
    assert kri.refresh(replica, ledger, now=NOW)  # the holder published, then died: nothing refreshes now
    krn._reset_for_tests()
    _add(replica, _linked("t_late", CHAT, at=NOW - 5), 50)
    started = time.monotonic()
    got = _collect(replica, ledger, now=NOW)
    assert "hub/default/default/t_late" in {item["ref"] for item in got}
    assert time.monotonic() - started < 5.0  # inside the poller's 5 s interval, without any holder


class _Lease:
    def __init__(self, held):
        self.value, self.asked, self.beats, self.released = held, 0, 0, False

    def held(self):
        self.asked += 1
        return self.value

    def beat(self):
        self.beats += 1

    def release(self):
        self.released = True


def test_holder_republishes_on_replica_change_only_while_leased(tmp_path):
    replica, ledger = _home(tmp_path, _cards())
    lease, stop = _Lease(False), threading.Event()
    thread = threading.Thread(target=kri._hold, args=(lease, replica, ledger, stop, 0.01), daemon=True)
    thread.start()
    try:
        wait_until(lambda: lease.asked > 3, what="standby passes")
        assert kri.published_key(ledger) is None  # a standby never publishes
        lease.value = True
        wait_until(lambda: kri.published_key(ledger) == kri.replica_key(replica), what="holder publish")
        _add(replica, _linked("t_new", CHAT), 50)
        wait_until(lambda: kri.published_key(ledger) == kri.replica_key(replica), what="change-driven publish")
        # The key is on disk before ``publish`` counts it: let the publishing pass end (it beats after
        # ``refresh`` returns) before reading the counter, or a starved runner reads it one short.
        beats = lease.beats
        wait_until(lambda: lease.beats > beats, what="publishing pass done")
        publishes = kri.stats["publishes"]
        beats = lease.beats
        wait_until(lambda: lease.beats > beats + 5, what="idle passes")
        assert kri.stats["publishes"] == publishes  # an unchanged replica is not rebuilt
    finally:
        stop.set()
        thread.join(5)
    assert lease.released
    krn._reset_for_tests()
    with pytest.MonkeyPatch.context() as mp:
        _forbid_local_build(mp)
        assert "hub/default/default/t_new" in {item["ref"] for item in _collect(replica, ledger)}


def test_reader_parses_only_the_payloads_that_changed(tmp_path, monkeypatch):
    cards = [_linked(f"t_{i}", CHAT) for i in range(12)]
    replica, ledger = _home(tmp_path, cards)
    assert kri.refresh(replica, ledger, now=NOW)
    from tui_gateway.kanban_replica_observer import fingerprint
    assert len(kri.lookup(replica, ledger, fingerprint(replica), "default", (CHAT,))[1][CHAT]) == 12
    cards[3]["task"]["title"] = "renamed"
    with sqlite3.connect(replica) as conn:
        conn.execute("UPDATE replica SET data=?, seq=50 WHERE key=?", (json.dumps(cards[3]["task"]), cards[3]["key"]))
    assert kri.refresh(replica, ledger, now=NOW)
    parsed = []
    real = json.loads
    monkeypatch.setattr(kri, "json", type("J", (), {"loads": staticmethod(lambda s: parsed.append(s) or real(s)),
                                                     "dumps": staticmethod(json.dumps)}))
    hit = kri.lookup(replica, ledger, fingerprint(replica), "default", (CHAT,))
    titles = {card["task"]["title"] for card in hit[1][CHAT]}
    assert "renamed" in titles and len(titles) == 12
    assert len([s for s in parsed if '"title"' in s]) == 1  # one task payload, not twelve


def test_replica_without_seq_has_no_key_and_never_publishes(tmp_path):
    replica, ledger = _home(tmp_path, _cards())
    with sqlite3.connect(replica) as conn:
        conn.execute("ALTER TABLE replica DROP COLUMN seq")
    assert kri.replica_key(replica) is None
    assert len(_collect(replica, ledger)) == 4
    assert not kri.index_path(ledger).exists()


def test_busy_index_is_skipped_not_deleted_as_corrupt(tmp_path):
    """A mode=ro reader's read transaction on a not-yet-WAL copy makes ``journal_mode=WAL`` raise 'database is
    locked': that is busy (skip, the reader computes locally), not corruption, so the healthy copy survives."""
    replica, ledger = _home(tmp_path, _cards())
    db = kri.index_path(ledger)
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:  # rollback-journal copy, e.g. written by an older build
        conn.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO meta VALUES ('marker', 'kept')")
    ident = os.stat(db).st_ino
    key = kri.replica_key(replica)
    assert key is not None
    reader = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.0)
    try:
        reader.execute("BEGIN")
        assert reader.execute("SELECT v FROM meta WHERE k = 'marker'").fetchone() == ("kept",)
        assert kri.publish(replica, ledger, key, "macbook", {}, {}, {}) is False
        assert kri.stats["busy"] == 1
    finally:
        reader.close()
    assert os.stat(db).st_ino == ident
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT v FROM meta WHERE k = 'marker'").fetchone() == ("kept",)
    assert kri.publish(replica, ledger, key, "macbook", {}, {}, {})  # the reader gone, the same file is upgraded
    assert os.stat(db).st_ino == ident and kri.published_key(ledger) == key


def test_holder_backs_off_while_publishing_fails(tmp_path, monkeypatch):
    """A refresh that never succeeds (an unwritable index, a replica with no key) is not rerun every poll: the
    holder retries after a doubling backoff, and once current it does not rebuild the same stamp again."""
    replica, ledger = _home(tmp_path, _cards())
    clock, calls, result = [1000.0], [], [False]
    monkeypatch.setattr(kri, "_clock", lambda: clock[0])
    monkeypatch.setattr(kri, "refresh", lambda hub, led: calls.append(1) or result[0])
    lease, stop = _Lease(True), threading.Event()
    thread = threading.Thread(target=kri._hold, args=(lease, replica, ledger, stop, 0.01), daemon=True)
    thread.start()
    try:
        wait_until(lambda: lease.beats > 30, what="failing passes")
        assert len(calls) == 1  # frozen clock: still backing off
        clock[0] += 0.015
        wait_until(lambda: len(calls) == 2, what="retry after the first backoff")
        beats = lease.beats
        clock[0] += 0.015  # the backoff doubled: not due yet
        wait_until(lambda: lease.beats > beats + 10, what="passes inside the doubled backoff")
        assert len(calls) == 2
        result[0] = True
        clock[0] += 0.03
        wait_until(lambda: len(calls) == 3, what="retry after the doubled backoff")
        beats = lease.beats
        wait_until(lambda: lease.beats > beats + 10, what="passes on a current copy")
        assert len(calls) == 3  # the stamp it published is not refreshed again
    finally:
        stop.set()
        thread.join(5)


def _spawns(monkeypatch):
    import agent.memory_provider as mp
    spawned = []

    class _Thread:
        def __init__(self, args):
            self.args = args

        def start(self):
            spawned.append(self.args)

    monkeypatch.setattr(mp, "spawn_context_thread", lambda target, name, args: _Thread(args))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    return spawned


def test_no_holder_without_flock(monkeypatch):
    spawned = _spawns(monkeypatch)
    monkeypatch.setattr(kri, "fcntl", None)
    kri.ensure_holder("serve")
    assert spawned == [] and not kri._holders


def test_gateway_claim_upgrades_a_serve_holder(monkeypatch):
    """The role is not whichever call came first: a gateway's later claim replaces a 'serve' refresher."""
    spawned = _spawns(monkeypatch)
    kri.ensure_holder("serve")
    kri.ensure_holder("serve")
    assert [args[0].role for args in spawned] == ["serve"]
    kri.ensure_holder("gateway")
    assert [args[0].role for args in spawned] == ["serve", "gateway"]
    assert spawned[0][3].is_set() and not spawned[1][3].is_set()  # the serve refresher stops
    kri.ensure_holder("serve")
    kri.ensure_holder("gateway")
    assert len(spawned) == 2


def _failing_holder(monkeypatch, replica, ledger):
    """A held ``_hold`` whose every refresh fails, on a frozen clock (the backoff never expires by itself)."""
    clock, calls = [1000.0], []
    monkeypatch.setattr(kri, "_clock", lambda: clock[0])
    monkeypatch.setattr(kri, "refresh", lambda hub, led: calls.append(1) and False)
    lease, stop = _Lease(True), threading.Event()
    thread = threading.Thread(target=kri._hold, args=(lease, replica, ledger, stop, 0.01), daemon=True)
    thread.start()
    wait_until(lambda: lease.beats > 20, what="failing passes")
    assert len(calls) == 1  # backing off
    return lease, stop, thread, calls


def test_holder_backoff_restarts_when_the_lease_is_re_acquired(tmp_path, monkeypatch):
    """A lost lease ends the holding's failing streak: the next holding rebuilds at once, not after it."""
    replica, ledger = _home(tmp_path, _cards())
    lease, stop, thread, calls = _failing_holder(monkeypatch, replica, ledger)
    try:
        asked = lease.asked
        lease.value = False
        wait_until(lambda: lease.asked > asked + 3, what="standby passes")
        lease.value = True
        wait_until(lambda: len(calls) == 2, what="rebuild on re-acquire, clock still frozen")
    finally:
        stop.set()
        thread.join(5)


def test_holder_backoff_restarts_on_a_new_replica_stamp(tmp_path, monkeypatch):
    """The backoff is per replica state: a new stamp is tried at once, whatever the last state's streak."""
    replica, ledger = _home(tmp_path, _cards())
    lease, stop, thread, calls = _failing_holder(monkeypatch, replica, ledger)
    try:
        _add(replica, _linked("t_new", CHAT), 50)
        wait_until(lambda: len(calls) == 2, what="rebuild of the new stamp, clock still frozen")
        beats = lease.beats
        wait_until(lambda: lease.beats > beats + 20, what="passes on the same failing stamp")
        assert len(calls) == 2  # that stamp backs off again
    finally:
        stop.set()
        thread.join(5)


@pytest.mark.parametrize("delay_metadata", [False, True], ids=["ordinary", "held-before-metadata"])
def test_serve_holder_hands_the_lease_to_a_gateway_claim_in_the_same_process(tmp_path, monkeypatch, delay_metadata):
    """Two real ``HostLoopLease`` objects in one process: the serve refresher holds, a gateway claim stops it, its
    ``release()`` unlocks, and the gateway lease's blocked ``LOCK_EX`` waiter becomes the holder and publishes."""
    import agent.memory_provider as mp
    from hermes_cli import host_loop_lease as hll

    metadata_observed = threading.Event()
    real_write = hll.HostLoopLease._write_meta

    def write_meta(lease, *, held):
        if delay_metadata and lease.role == "gateway" and held:
            # Hold the actual acquisition-to-publication gap until the reader
            # observes it, rather than hoping thread scheduling exposes it.
            assert metadata_observed.wait(15)
        real_write(lease, held=held)

    monkeypatch.setattr(hll.HostLoopLease, "_write_meta", write_meta)
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path))
    replica, _ = _home(tmp_path, _cards())
    ledger = krn.ledger_path()
    assert krn.hub_db_path() == replica
    real, spawned = mp.spawn_context_thread, []

    def spawn(target, name, args):
        thread = real(target, name=name, args=args)
        spawned.append((thread, args[0]))
        return thread
    monkeypatch.setattr(mp, "spawn_context_thread", spawn)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    try:
        kri.ensure_holder("serve")
        (serve_thread, serve), = spawned
        assert isinstance(serve, hll.HostLoopLease) and serve.role == "serve"
        wait_until(lambda: serve._held, timeout=15, what="the serve refresher holds the lease")
        wait_until(lambda: kri.published_key(ledger) == kri.replica_key(replica), timeout=15, what="serve publish")
        kri.ensure_holder("gateway")
        gateway_thread, gateway = spawned[1]
        assert gateway is not serve and gateway.role == "gateway" and gateway.path == serve.path
        serve_thread.join(15)
        assert not serve_thread.is_alive() and not serve._held  # the serve refresher stopped and released
        wait_until(lambda: gateway._held, timeout=15, what="the gateway lease takes over the released lock")
        # _held describes the acquired flock, not completion of _become_holder's
        # subsequent metadata write. Observe publication before asserting its fields.
        meta = {}

        def gateway_metadata_published():
            nonlocal meta
            try:
                meta = json.loads(gateway.path.read_text())
            except json.JSONDecodeError:  # lock-free reader may overlap an in-place write
                return False
            if delay_metadata and not metadata_observed.is_set():
                assert meta["held"] is False and meta["role"] == "serve"
            metadata_observed.set()
            return meta.get("held") is True and meta.get("role") == "gateway"

        wait_until(gateway_metadata_published, timeout=15, what="gateway lease metadata publication")
        assert meta["held"] is True and meta["role"] == "gateway" and meta["pid"] == os.getpid()
        assert not gateway._waiting
        _add(replica, _linked("t_new", CHAT), 50)  # the gateway refresher publishes the next state
        wait_until(lambda: kri.published_key(ledger) == kri.replica_key(replica), timeout=15,
                   what="gateway publish")
    finally:
        metadata_observed.set()
        kri.reset()
        for thread, _lease in spawned:
            thread.join(15)
    assert not gateway_thread.is_alive() and not any(lease._held for _thread, lease in spawned)
