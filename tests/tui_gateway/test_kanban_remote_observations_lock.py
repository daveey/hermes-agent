"""[daveey] ``observe`` writes the observation ledger without holding ``_lock``, and only for card states it has not
seen in that ledger file yet; a crash or restart re-reads the ledger, so no acknowledged ``first_seen`` is lost.

Notice pollers queued at ``observe``'s ``with _lock`` while one poller re-read every terminal card's
``first_seen`` (and waited for the ledger's ``BEGIN IMMEDIATE``) inside it.
"""
import json
import sqlite3
import threading

from tests.tui_gateway.test_kanban_remote_arrival import CHAT, _card, _hub
from tests.waiting import wait_until
from tui_gateway import kanban_remote_notices as krn
from tui_gateway import kanban_remote_observations as obs

NOW = 1_800_000_000.0


def _world(tmp_path, n=20):
    cards = [_card(f"t_bulk_{i}", CHAT, status="done", at=NOW - i) for i in range(n)]
    replica = _hub(tmp_path, cards)
    krn._reset_for_tests()
    obs.stats["transactions"] = 0
    return replica, tmp_path / "ledger.db", cards


def _traced_ledger(monkeypatch):
    """Every observation-ledger statement, with whether ``_lock`` was held when it ran."""
    statements = []
    real = krn._ledger

    def traced(path=None):
        conn = real(path)
        conn.set_trace_callback(lambda sql: statements.append((sql, obs._lock.locked())))
        return conn

    monkeypatch.setattr(krn, "_ledger", traced)
    return statements


def _move(replica, card, status, at):
    moved = _card(card["key"].split("/")[-1], CHAT, status=status, at=at)
    with sqlite3.connect(replica) as conn:
        conn.execute("UPDATE replica SET data=?, seq=seq+1000 WHERE key=?", (json.dumps(moved["task"]), moved["key"]))


def test_the_ledger_write_runs_without_the_observe_lock(tmp_path, monkeypatch):
    replica, ledger, _cards = _world(tmp_path)
    statements = _traced_ledger(monkeypatch)
    first = obs.observe(replica, ledger, NOW)
    assert len(first) == 20
    writes = [held for sql, held in statements if sql.startswith(("BEGIN IMMEDIATE", "INSERT", "COMMIT"))]
    assert writes and not any(writes)
    assert not any(held for _sql, held in statements)


def test_a_moved_card_writes_only_its_new_state(tmp_path, monkeypatch):
    replica, ledger, cards = _world(tmp_path)
    first = obs.observe(replica, ledger, NOW)
    statements = _traced_ledger(monkeypatch)
    _move(replica, cards[3], "blocked", NOW + 5)
    second = obs.observe(replica, ledger, NOW + 10)
    assert sum(sql.startswith("SELECT first_seen") for sql, _ in statements) == 1
    assert sum(sql.startswith("INSERT INTO observations") for sql, _ in statements) == 1
    krn._reset_for_tests()  # a restarted backend answers the same from the ledger alone
    assert obs.observe(replica, ledger, NOW + 10) == second
    assert {k: v for k, v in second.items() if k[1] != cards[3]["key"]} == {
        k: v for k, v in first.items() if k[1] != cards[3]["key"]}


def test_a_snapshot_with_no_new_state_opens_no_ledger_transaction(tmp_path, monkeypatch):
    replica, ledger, _cards = _world(tmp_path)
    first = obs.observe(replica, ledger, NOW)
    statements = _traced_ledger(monkeypatch)
    with sqlite3.connect(replica) as conn:  # a running card moves: the signature changes, no terminal state does
        conn.execute("INSERT INTO replica VALUES ('hub','tasks','default/default/t_live',?,5000)",
                     (json.dumps({"id": "t_live", "status": "running"}),))
    assert obs.observe(replica, ledger, NOW + 10) == first
    assert statements == [] and obs.stats["transactions"] == 1


def test_a_replaced_ledger_is_read_again(tmp_path, monkeypatch):
    replica, ledger, cards = _world(tmp_path)
    obs.observe(replica, ledger, NOW)
    ledger.unlink()
    for suffix in ("-wal", "-shm"):
        ledger.with_name(ledger.name + suffix).unlink(missing_ok=True)
    _move(replica, cards[0], "blocked", NOW + 5)
    statements = _traced_ledger(monkeypatch)
    later = obs.observe(replica, ledger, NOW + 10)
    assert len(later) == 20
    assert sum(sql.startswith("INSERT INTO observations") for sql, _ in statements) == 20


def test_an_older_snapshot_never_overwrites_a_newer_memo(tmp_path, monkeypatch):
    replica, ledger, cards = _world(tmp_path)
    real = obs._persist
    started = []

    def racing(source, cards_, ledger_, now):
        started.append(now)
        if len(started) == 1:  # a newer poll starts (and publishes) while this one writes
            _move(replica, cards[5], "blocked", NOW + 5)
            obs._generation[next(iter(obs._generation))] += 1
        return real(source, cards_, ledger_, now)

    monkeypatch.setattr(obs, "_persist", racing)
    obs.observe(replica, ledger, NOW)
    assert obs._memo == {}  # superseded: the older answer is returned but never memoized
    obs.observe(replica, ledger, NOW + 10)
    assert len(obs._memo) == 1 and len(started) == 2


def test_concurrent_polls_agree_on_every_first_seen(tmp_path):
    replica, ledger, _cards = _world(tmp_path, n=200)
    results = []
    barrier = threading.Barrier(4)

    def poll():
        barrier.wait()
        results.append(obs.observe(replica, ledger, NOW))

    threads = [threading.Thread(target=poll) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 4 and all(r == results[0] for r in results) and len(results[0]) == 200


def test_a_busy_wal_switch_is_retried_not_raised(tmp_path, monkeypatch):
    """SQLite answers ``journal_mode=WAL`` with SQLITE_BUSY, skipping the busy handler, while a concurrent first
    opener holds the fresh ledger: one poller then raised ``database is locked`` (CI flake, 3 of 4 polls answered)."""
    real = sqlite3.connect
    busy = []

    class Racing(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql == "PRAGMA journal_mode=WAL" and len(busy) < 3:
                busy.append(sql)
                raise sqlite3.OperationalError("database is locked")
            return super().execute(sql, *args)

    monkeypatch.setattr(krn.sqlite3, "connect", lambda *a, **kw: real(*a, factory=Racing, **kw))
    replica, ledger, _cards = _world(tmp_path)
    assert len(obs.observe(replica, ledger, NOW)) == 20 and len(busy) == 3


def _waiters(monkeypatch):
    """Threads that reached the wait for another poller's in-flight pass (the ``_Pending`` event)."""
    waiting = []
    real_init = obs._Pending.__init__

    class Counted(threading.Event):
        def wait(self, timeout=None):
            waiting.append(threading.get_ident())  # list.append is atomic
            return super().wait(timeout)

    def init(self, token):
        real_init(self, token)
        self.done = Counted()

    monkeypatch.setattr(obs._Pending, "__init__", init)
    return waiting


def test_concurrent_polls_of_one_snapshot_persist_it_once(tmp_path, monkeypatch):
    """[daveey] Pollers that arrive while another persists the same snapshot wait for its answer.

    The memo is published only after ``_persist`` commits, outside the lane, so every notice poller that came in
    meanwhile re-ran the full pass over every terminal card and queued on the ledger's ``BEGIN IMMEDIATE``
    (hermes-hub 11-13: 7 pollers per process, 21 snapshot changes -> 79 ``_persist`` runs when a pass took 0.6 s).
    """
    replica, ledger, _cards = _world(tmp_path, n=50)
    real = obs._persist
    calls = []
    entered = threading.Event()
    release = threading.Event()

    def slow(source, cards_, ledger_, now):
        calls.append(now)
        entered.set()
        assert release.wait(10)
        return real(source, cards_, ledger_, now)

    monkeypatch.setattr(obs, "_persist", slow)
    waiting = _waiters(monkeypatch)
    results = []
    leader = threading.Thread(target=lambda: results.append(obs.observe(replica, ledger, NOW)))
    leader.start()
    assert entered.wait(10)
    followers = [threading.Thread(target=lambda: results.append(obs.observe(replica, ledger, NOW + 1)))
                 for _ in range(6)]
    for t in followers:
        t.start()
    wait_until(lambda: len(waiting) >= 6 or len(calls) > 1, timeout=10)  # all wait, or one re-persisted
    release.set()
    for t in (leader, *followers):
        t.join(10)
    assert len(calls) == 1
    assert len(results) == 7 and all(r == results[0] for r in results) and len(results[0]) == 50
    assert obs._pending == {}


def test_a_failed_persist_lets_a_waiting_poll_persist_itself(tmp_path, monkeypatch):
    replica, ledger, _cards = _world(tmp_path)
    real = obs._persist
    calls = []
    entered = threading.Event()
    release = threading.Event()

    def flaky(source, cards_, ledger_, now):
        calls.append(now)
        if len(calls) == 1:
            entered.set()
            assert release.wait(10)
            raise sqlite3.OperationalError("database is locked")
        return real(source, cards_, ledger_, now)

    monkeypatch.setattr(obs, "_persist", flaky)
    waiting = _waiters(monkeypatch)
    errors, results = [], []

    def first():
        try:
            obs.observe(replica, ledger, NOW)
        except sqlite3.OperationalError as exc:
            errors.append(exc)

    leader = threading.Thread(target=first)
    leader.start()
    assert entered.wait(10)
    follower = threading.Thread(target=lambda: results.append(obs.observe(replica, ledger, NOW + 1)))
    follower.start()
    wait_until(lambda: len(waiting) >= 1, timeout=10)
    release.set()
    for t in (leader, follower):
        t.join(10)
    assert len(errors) == 1 and len(calls) == 2
    assert len(results) == 1 and len(results[0]) == 20
    assert obs._pending == {}


def test_a_failed_persist_with_several_waiters_ends_and_answers_each(tmp_path, monkeypatch):
    replica, ledger, _cards = _world(tmp_path)
    real = obs._persist
    calls = []
    entered = threading.Event()
    release = threading.Event()

    def flaky(source, cards_, ledger_, now):
        calls.append(now)
        if len(calls) == 1:
            entered.set()
            assert release.wait(10)
            raise sqlite3.OperationalError("database is locked")
        return real(source, cards_, ledger_, now)

    monkeypatch.setattr(obs, "_persist", flaky)
    waiting = _waiters(monkeypatch)
    results = []

    def first():
        try:
            obs.observe(replica, ledger, NOW)
        except sqlite3.OperationalError:
            pass

    leader = threading.Thread(target=first)
    leader.start()
    assert entered.wait(10)
    followers = [threading.Thread(target=lambda: results.append(obs.observe(replica, ledger, NOW + 1)))
                 for _ in range(3)]
    for t in followers:
        t.start()
    wait_until(lambda: len(waiting) >= 3, timeout=10)
    release.set()
    for t in (leader, *followers):
        t.join(10)
        assert not t.is_alive()
    assert 2 <= len(calls) <= 4  # each waiter persists at most once itself; none loops
    assert len(results) == 3 and all(len(r) == 20 for r in results)
    assert obs._pending == {}


def test_a_newer_snapshot_does_not_wait_for_an_older_one(tmp_path, monkeypatch):
    replica, ledger, cards = _world(tmp_path)
    real = obs._persist
    calls = []
    entered = threading.Event()
    release = threading.Event()

    def slow(source, cards_, ledger_, now):
        calls.append(now)
        if len(calls) == 1:
            entered.set()
            assert release.wait(10)
        return real(source, cards_, ledger_, now)

    monkeypatch.setattr(obs, "_persist", slow)
    older = threading.Thread(target=lambda: obs.observe(replica, ledger, NOW))
    older.start()
    assert entered.wait(10)
    _move(replica, cards[2], "blocked", NOW + 5)
    newer = obs.observe(replica, ledger, NOW + 10)  # answers while the older pass is still blocked
    assert len(calls) == 2 and older.is_alive()
    assert any(k[1] == cards[2]["key"] and str(k[2]).startswith("blocked") for k in newer), sorted(newer)[:3]
    release.set()
    older.join(10)
    assert obs._pending == {}


def test_reset_clears_pending_and_never_strands_a_waiter(tmp_path, monkeypatch):
    replica, ledger, _cards = _world(tmp_path)
    real = obs._persist
    entered = threading.Event()
    release = threading.Event()

    def slow(source, cards_, ledger_, now):
        entered.set()
        assert release.wait(10)
        return real(source, cards_, ledger_, now)

    monkeypatch.setattr(obs, "_persist", slow)
    waiting = _waiters(monkeypatch)
    results = []
    leader = threading.Thread(target=lambda: results.append(obs.observe(replica, ledger, NOW)))
    leader.start()
    assert entered.wait(10)
    follower = threading.Thread(target=lambda: results.append(obs.observe(replica, ledger, NOW + 1)))
    follower.start()
    wait_until(lambda: len(waiting) >= 1, timeout=10)
    krn._reset_for_tests()
    assert obs._pending == {}
    release.set()
    for t in (leader, follower):
        t.join(10)
        assert not t.is_alive()
    assert len(results) == 2 and results[0] == results[1]
