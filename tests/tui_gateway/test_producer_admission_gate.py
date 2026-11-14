"""Change gate of the producer-admission cold-chat sweep (design t_01a1d563324d7e8e §3, card J). [daveey]

``sweep_once`` opened every profile's SessionDB on every 15 s pass (13 of 17 profile samples; ~0.7 s CPU a pass
on the hub). It now keeps one read-only observer per store and skips a store whose (file identity,
``data_version``) is unchanged since a pass that found nothing pending. A pending row is due on every pass, so a
refused resume retries with no commit in between.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import time

import pytest

import hermes_state_producer_admission as pa
from tests.tui_gateway.test_producer_admission import (
    PRODUCER,
    SID,
    _admit,
    _db,
    _install_resume,
    _notices,
    _root,
    sdb as sdb,  # noqa: PLC0414 -- the self-alias is load-bearing: it suppresses F811 for the pytest fixture parameter shadowing this import
    turns as turns,  # noqa: PLC0414 -- the self-alias is load-bearing: it suppresses F811 for the pytest fixture parameter shadowing this import
)
from tui_gateway import producer_admission as delivery
from tui_gateway import server


@pytest.fixture(autouse=True)
def _fresh_observers():
    reset = getattr(delivery, "reset_observers", lambda: None)  # tolerant: the RED run predates the gate
    reset()
    yield
    reset()


@pytest.fixture
def opens(monkeypatch):
    """Count full-path store opens (the cost the gate removes)."""
    calls = []
    real = pa.open_profile_db

    def _counting(home):
        calls.append(str(home))
        return real(home)

    monkeypatch.setattr(pa, "open_profile_db", _counting)
    return calls


def test_unchanged_store_with_nothing_pending_is_not_reopened(sdb, opens):
    assert delivery.sweep_once() == []
    first = len(opens)
    for _ in range(3):
        assert delivery.sweep_once() == []
    assert len(opens) == first, "a quiet store must be skipped, not reopened every pass"


def test_unrelated_commit_reads_the_observer_but_opens_nothing(sdb, opens):
    delivery.sweep_once()
    before = len(opens)
    sdb.append_message(SID, "user", "an unrelated turn moves data_version")
    assert delivery.sweep_once() == []
    assert len(opens) == before, "no pending owner: the observer answers without a SessionDB open"


def test_admission_after_a_skipped_pass_is_picked_up_next_pass(sdb, turns, monkeypatch, opens):
    calls, _ = _install_resume(monkeypatch)
    assert delivery.sweep_once() == [] and delivery.sweep_once() == []
    _admit()  # the input change: one commit to the store
    [rec] = delivery.sweep_once()
    assert rec["verdict"] == "started" and rec["owner"] == SID
    assert len(_notices(sdb)) == 1 and len(calls) == 1


def test_pending_row_is_retried_every_pass_with_no_other_change(sdb, turns, monkeypatch):
    calls, _ = _install_resume(monkeypatch, refuse=True)
    _admit()
    for n in range(1, 4):  # a refused resume writes nothing: the row is due again on the next pass anyway
        [rec] = delivery.sweep_once()
        assert rec["verdict"] == "resume_refused"
        assert len(calls) == n
    assert pa.read_producer_admission(producer=PRODUCER, profile_home=_root())["status"] == "queued"


def test_delivered_row_settles_then_the_store_goes_quiet(sdb, turns, monkeypatch, opens):
    _install_resume(monkeypatch)
    _admit()
    [rec] = delivery.sweep_once()
    assert rec["verdict"] == "started"
    assert delivery.sweep_once() == []  # the pass after a pending one re-reads, finds nothing
    settled = len(opens)
    assert delivery.sweep_once() == [] and len(opens) == settled


def test_replaced_store_file_is_reobserved(tmp_path):
    import sqlite3
    path = tmp_path / "state.db"

    def _make(target):
        with contextlib.closing(sqlite3.connect(target)) as conn:
            conn.execute("CREATE TABLE t (x)")
            conn.commit()

    _make(path)
    with delivery._observers_lock:
        key1, conn1 = delivery._observe(path)
        assert delivery._observe(path) == (key1, conn1)  # unchanged: same key, same kept connection
    _make(tmp_path / "restored.db")
    os.replace(tmp_path / "restored.db", path)  # a restore swaps the inode
    with delivery._observers_lock:
        key2, conn2 = delivery._observe(path)
    assert key2[:2] != key1[:2] and conn2 is not conn1


def test_profile_store_added_later_is_swept(turns, monkeypatch):
    calls, _ = _install_resume(monkeypatch)
    assert delivery.sweep_once() == []
    work = _db("work")
    work.create_session(SID, "desktop")
    work.append_message(SID, "user", "hi")

    @contextlib.contextmanager
    def _opener(session):
        yield work

    monkeypatch.setattr(server, "_session_db", _opener)
    _admit(home=_root() / "profiles" / "work")
    recs = delivery.sweep_once()
    assert [(r["profile"], r["verdict"]) for r in recs] == [("work", "started")]
    assert len(calls) == 1
    work.close()


def test_removed_profile_store_drops_its_observer(monkeypatch):
    work = _db("work")
    work.create_session(SID, "desktop")
    work.close()
    delivery.sweep_once()
    assert any("work" in p for p in delivery._observers)
    shutil.rmtree(_root() / "profiles" / "work")
    delivery.sweep_once()
    assert not any("work" in p for p in delivery._observers)


def test_profile_delete_release_closes_the_observer_under_that_home(sdb, monkeypatch):
    """A profile delete (and a snapshot restore) calls ``hermes_state_registry.close_all_under`` before rmtree; on
    Windows any open handle fails it with WinError 32, and a still-listed store would keep its observer forever."""
    import hermes_state_registry
    work = _db("work")
    work.create_session(SID, "desktop")
    work.close()
    delivery.sweep_once()
    work_home = _root() / "profiles" / "work"
    assert any("work" in p for p in delivery._observers)
    hermes_state_registry.close_all_under(work_home)
    assert not any("work" in p for p in delivery._observers), "the delete path must release the observer fd"
    assert any(p == str(_root() / "state.db") for p in delivery._observers), "other homes keep theirs"
    hermes_state_registry.close_all()
    assert delivery._observers == {} and delivery._last_pass == {}


def test_released_observer_is_reopened_and_the_next_admission_still_lands(sdb, turns, monkeypatch):
    import hermes_state_registry
    calls, _ = _install_resume(monkeypatch)
    delivery.sweep_once()
    hermes_state_registry.close_all_under(_root())
    _admit()
    [rec] = delivery.sweep_once()
    assert rec["verdict"] == "started" and len(calls) == 1


def test_key_from_a_dropped_observer_is_not_remembered(sdb, turns, monkeypatch):
    """A fresh connection's data_version restarts low, so a key read by an observer that was dropped mid-pass
    could equal a later key and skip commits: it must not be recorded."""
    _install_resume(monkeypatch, refuse=True)
    _admit()
    real = delivery._sweep_profile

    def _reset_mid_pass(*args, **kwargs):
        delivery.reset_observers()
        return real(*args, **kwargs)

    monkeypatch.setattr(delivery, "_sweep_profile", _reset_mid_pass)
    delivery.sweep_once()
    assert str(_root() / "state.db") not in delivery._last_pass


def test_lease_loss_closes_the_observers(sdb):
    class _Lease:
        def __init__(self, held):
            self._held = held

        def held(self):
            return self._held

        def beat(self):
            pass

    assert delivery.run_pass(_Lease(True)) is True
    assert delivery._observers
    assert delivery.run_pass(_Lease(False)) is False
    assert delivery._observers == {}, "another process sweeps now: this one must hold no store fds"


@pytest.mark.parametrize("exit_mode", ["stop", "wait_error"])
def test_loop_exit_closes_observer_connections_and_releases_lease(sdb, monkeypatch, exit_mode):
    import sqlite3
    import threading

    from hermes_cli import host_loop_lease

    connections = []
    released = []
    beats = []

    class _Lease:
        def held(self):
            return True

        def beat(self):
            beats.append(True)

        def release(self):
            released.append(True)

    class _Stop(threading.Event):
        waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                return False  # no wall-clock delay before the real first pass
            assert self.waits == 2, "shutdown must not run another sweep"
            assert delivery._observers and delivery._last_pass
            connections.extend(entry[1] for entry in delivery._observers.values())
            for conn in connections:
                assert conn.execute("SELECT 1").fetchone()[0] == 1
            if exit_mode == "wait_error":
                raise RuntimeError("injected wait failure")
            self.set()
            return True

    monkeypatch.setattr(host_loop_lease, "HostLoopLease", lambda *a, **kw: _Lease())
    monkeypatch.setattr(host_loop_lease, "process_role", lambda: "test")
    stop = _Stop()
    if exit_mode == "wait_error":
        with pytest.raises(RuntimeError, match="injected wait failure"):
            delivery._loop(stop)
    else:
        delivery._loop(stop)
        assert stop.is_set()
    assert beats == [True] and released == [True]
    assert connections, "exercise actual observers opened by the real sweep"
    assert delivery._observers == {} and delivery._last_pass == {}
    for conn in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")


def test_unreadable_store_takes_the_full_path(sdb, opens, monkeypatch):
    def _broken(path):
        raise OSError("observer unavailable")

    monkeypatch.setattr(delivery, "_observe", _broken)
    delivery.sweep_once()
    delivery.sweep_once()
    assert len(opens) == 2, "no observer: every pass keeps today's full path"


# -- replay benchmark: budget 0.2% of one core (design §3: producer_admission 1.3 -> 0.2) -------------------------

PROFILES = 11  # the hub's profile count at measurement time
INTERVAL_S = delivery.INTERVAL_S
BUDGET_CORE = 0.002


def test_replay_budget_every_store_committing_every_pass():
    """Worst case of the measured change stream (state.db data_version moved in 140/300 2 s windows, so every
    15 s pass sees a commit): every store commits before every pass. CPU per pass / interval must stay in budget."""
    stores = [_db(None)] + [_db(f"p{n}") for n in range(PROFILES - 1)]
    try:
        for n, db in enumerate(stores):
            db.create_session(f"s{n}", "desktop")
        delivery.sweep_once()  # cold: opens observers
        best = float("inf")
        for _ in range(3):
            spent = 0.0
            for i in range(10):
                for db in stores:
                    _commit(db, i)
                t0 = time.thread_time()
                assert delivery.sweep_once() == []
                spent += time.thread_time() - t0
            best = min(best, spent / 10)
        assert best / INTERVAL_S <= BUDGET_CORE, f"{best * 1000:.2f} ms per pass = {best / INTERVAL_S:.4%} of a core"
    finally:
        for db in stores:
            db.close()


def _commit(db, i: int) -> None:
    with db._read_ctx() as conn:
        sid = conn.execute("SELECT id FROM sessions LIMIT 1").fetchone()[0]
    db.append_message(sid, "user", f"tick {i}")
