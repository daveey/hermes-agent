"""[daveey] ``observe`` writes the observation ledger without holding ``_lock``, and only for card states it has not
seen in that ledger file yet; a crash or restart re-reads the ledger, so no acknowledged ``first_seen`` is lost.

Notice pollers queued at ``observe``'s ``with _lock`` while one poller re-read every terminal card's
``first_seen`` (and waited for the ledger's ``BEGIN IMMEDIATE``) inside it.
"""
import json
import sqlite3
import threading

from tests.tui_gateway.test_kanban_remote_arrival import CHAT, _card, _hub
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
