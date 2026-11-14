"""Idle heartbeat/loop gates reuse a proven-EMPTY answer only while the store's committed revision holds.

The gates read ``state_meta`` by prefix (a JSON parse of every retained row) on every poll tick. These contracts pin the reuse: an unchanged store is scanned once across K checks; any
commit (this process's writer or another connection) is seen on the very next check; active work,
unparseable rows and read errors are never remembered, so they are re-examined on every check.
"""
import asyncio
import sqlite3

import pytest

from gateway import run_idle_gates as gates
from hermes_cli import goals, heartbeat, loops
from hermes_cli.heartbeat import HeartbeatState
from hermes_cli.loops import LoopState

K = 6

_KINDS = {
    # kind: (gate, module holding the scan, scan name, meta prefix, row factory)
    "heartbeat": (gates.profile_has_active_heartbeat, heartbeat, "store_has_active_heartbeat", "heartbeat:",
                  lambda status: HeartbeatState(prompt="p", interval_seconds=60, status=status).to_json()),
    "loop": (gates.profile_has_active_loop, loops, "store_has_active_loop", "loop:",
             lambda status: LoopState(prompt="p", status=status, interval_seconds=60.0).to_json()),
}


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root"))
    (tmp_path / "root").mkdir()
    home = tmp_path / "profiles" / "work"
    home.mkdir(parents=True)
    goals._DB_CACHE.clear()
    db = gates._profile_session_db_probe(home)  # the registry-owned handle the gates themselves read
    assert db is not None
    getattr(gates, "_EMPTY_PROOFS", {}).clear()
    yield home, db
    goals._DB_CACHE.clear()
    getattr(gates, "_EMPTY_PROOFS", {}).clear()
    db.close()


def _count_scans(monkeypatch, module, name, *, fail=None):
    real = getattr(module, name)
    calls = []

    def counted(store):
        calls.append(1)
        if fail and fail.pop():
            raise sqlite3.OperationalError("database is locked")
        return real(store)

    monkeypatch.setattr(module, name, counted)
    return calls


@pytest.mark.parametrize("kind", sorted(_KINDS))
def test_unchanged_store_is_scanned_once_across_k_checks(work, monkeypatch, kind):
    home, db = work
    gate, module, name, prefix, row = _KINDS[kind]
    db.set_meta(prefix + "old", row("cleared"))  # retained cleared rows are what every tick re-parsed
    scans = _count_scans(monkeypatch, module, name)

    assert [gate(home) for _ in range(K)] == [False] * K
    assert len(scans) == 1, f"unchanged store must be scanned once across {K} checks, got {len(scans)}"


@pytest.mark.parametrize("kind", sorted(_KINDS))
def test_commit_is_seen_on_the_next_check(work, monkeypatch, kind):
    home, db = work
    gate, module, name, prefix, row = _KINDS[kind]
    scans = _count_scans(monkeypatch, module, name)
    assert gate(home) is False and gate(home) is False
    assert len(scans) == 1

    # This process's writer connection commits an active row.
    db.set_meta(prefix + "s1", row("active"))
    assert gate(home) is True
    # Active work is never remembered: every check rescans it.
    assert gate(home) is True
    assert len(scans) == 3

    db.set_meta(prefix + "s1", row("cleared"))
    assert gate(home) is False and gate(home) is False
    assert len(scans) == 4

    # Another connection (another process's writer) commits: seen on the next check too.
    other = sqlite3.connect(str(home / "state.db"))
    try:
        with other:
            other.execute("INSERT INTO state_meta (key, value) VALUES (?, ?)", (prefix + "s2", row("active")))
    finally:
        other.close()
    assert gate(home) is True


@pytest.mark.parametrize("kind", sorted(_KINDS))
def test_read_errors_and_unparseable_rows_are_rechecked(work, monkeypatch, kind):
    home, db = work
    gate, module, name, prefix, _row = _KINDS[kind]
    scans = _count_scans(monkeypatch, module, name, fail=[False, True])  # pop(): first scan raises

    assert gate(home) is True, "a failing read fails open"
    assert gate(home) is False, "the failure was not remembered: the next check scans again"
    assert gate(home) is False
    assert len(scans) == 2

    db.set_meta(prefix + "bad", "{not json")
    assert [gate(home) for _ in range(3)] == [True] * 3, "an unparseable row is unknown, never idle"
    assert len(scans) == 5


def test_commit_landing_during_the_proving_scan_is_seen_next_check(work, monkeypatch):
    home, db = work
    real = heartbeat.store_has_active_heartbeat
    landed = []

    def scan_then_commit(store):
        answer = real(store)
        if not landed:  # another connection commits after the scan read, before the proof is stored
            landed.append(1)
            db.set_meta("heartbeat:late", _KINDS["heartbeat"][4]("active"))
        return answer

    monkeypatch.setattr(heartbeat, "store_has_active_heartbeat", scan_then_commit)
    assert gates.profile_has_active_heartbeat(home) is False
    assert gates.profile_has_active_heartbeat(home) is True


def test_writer_fallback_sees_its_own_commits(work, monkeypatch):
    """With no pooled reader the read runs on the writer connection, whose data_version does not move
    for its own commits: its write counter must invalidate the proof."""
    home, db = work
    monkeypatch.setattr(db, "_checkout_read_conn", lambda: None)
    assert gates.profile_has_active_heartbeat(home) is False
    assert gates.profile_has_active_heartbeat(home) is False
    db.set_meta("heartbeat:s1", _KINDS["heartbeat"][4]("active"))
    assert gates.profile_has_active_heartbeat(home) is True


def test_uncommitted_state_never_becomes_a_proof(work, monkeypatch):
    home, db = work
    monkeypatch.setattr(db, "_checkout_read_conn", lambda: None)
    db.set_meta("heartbeat:s1", _KINDS["heartbeat"][4]("active"))
    conn = db._conn
    conn.execute("BEGIN")
    conn.execute("DELETE FROM state_meta WHERE key = 'heartbeat:s1'")
    assert gates.profile_has_active_heartbeat(home) is False  # the open transaction's own view
    conn.execute("ROLLBACK")  # total_changes keeps the rolled-back delete: the revision cannot tell
    assert gates.profile_has_active_heartbeat(home) is True


def test_heartbeat_and_loop_proofs_are_independent(work):
    home, db = work
    db.set_meta("loop:s1", _KINDS["loop"][4]("active"))
    assert gates.profile_has_active_heartbeat(home) is False
    assert gates.profile_has_active_heartbeat(home) is False
    assert gates.profile_has_active_loop(home) is True, "a heartbeat proof must not answer the loop gate"


@pytest.mark.asyncio
async def test_root_loop_scan_skips_list_active_loops_while_proven_empty(work, monkeypatch):
    """The watcher's unscoped root scan (profile_home None) had no gate: it called list_active_loops
    every tick. It now consults the same proven-empty gate, and still lists every tick while a loop
    is active."""
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner

    root_db = await asyncio.to_thread(goals._get_session_db)  # HERMES_HOME=root: the store the unscoped root scan reads
    root_db.set_meta("loop:old", _KINDS["loop"][4]("cleared"))
    monkeypatch.setattr("gateway.run._handoff_watch_scopes", lambda _runner: [(None, None)])

    listed = []
    real_list = loops.list_active_loops

    def counted_list():
        listed.append(1)
        return real_list()

    monkeypatch.setattr(loops, "list_active_loops", counted_list)
    fired = []

    async def _fire_one(self, sid, state, now, warned, profile_name):
        fired.append(sid)

    monkeypatch.setattr(GatewayRunner, "_loop_wakeup_fire_one", _fire_one)

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={})
    runner.adapters = {}
    orig_sleep = asyncio.sleep

    async def _ticks(n):
        runner._running = True
        calls = {"n": 0}

        async def _sleep(delay):
            calls["n"] += 1
            if calls["n"] > n:  # 5s connect grace, then n scan passes
                runner._running = False
            return await orig_sleep(0)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(asyncio, "sleep", _sleep)
            await asyncio.wait_for(GatewayRunner._loop_wakeup_watcher(runner, interval=0), timeout=10)

    try:
        await _ticks(K)
        assert listed == [], f"a proven-empty root store must not be listed every tick, got {len(listed)}"

        root_db.set_meta("loop:live", _KINDS["loop"][4]("active"))
        await _ticks(3)
        assert len(listed) == 3, "an active loop keeps the per-tick listing"
        assert fired == ["live"] * 3
    finally:
        root_db.close()


def test_root_loop_gate_does_not_warn_on_a_corrupt_row_every_tick(work, caplog):
    """Before the gate, the root scan's list_active_loops skipped an unparseable loop row silently.
    The gate must keep failing open on it (never cached) without adding a warning per tick."""
    import logging

    root_db = goals._get_session_db()  # HERMES_HOME=root
    assert root_db is not None
    try:
        root_db.set_meta("loop:bad", "{not json")
        with caplog.at_level(logging.DEBUG, logger="hermes_cli.loops"):
            assert [gates.current_store_may_have_active_loop() for _ in range(3)] == [True] * 3
            assert loops.list_active_loops() == []  # the unchanged silent root listing
        warned = [r for r in caplog.records if r.name == "hermes_cli.loops" and r.levelno >= logging.WARNING]
        assert warned == []
    finally:
        root_db.close()


def test_conn_meta_prefix_read_is_a_literal_index_range_not_a_table_scan():
    """The gate's own prefix read reaches rows through the primary key (never every stored value) and
    still matches the prefix literally: ``_`` and ``%`` are not wildcards, neighbours stay out."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany("INSERT INTO state_meta VALUES (?, ?)", [
        ("loop:a", "1"), ("loop:b", "2"), ("loop", "x"), ("loop;z", "x"), ("loopy:a", "x"), ("heartbeat:a", "x"),
        ("a_b:1", "u"), ("axb:1", "x"), ("a%b:1", "p"), ("aqqb:1", "x")])
    meta = gates._ConnMeta(conn)
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    assert meta.list_meta_prefix("loop:") == [("loop:a", "1"), ("loop:b", "2")]
    assert meta.list_meta_prefix("a_b:") == [("a_b:1", "u")]
    assert meta.list_meta_prefix("a%b:") == [("a%b:1", "p")]
    assert meta.list_meta_prefix("nope:") == []
    conn.set_trace_callback(None)
    assert len(seen) == 4
    for sql in seen:
        plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql)]
        assert plan and all(step.startswith("SEARCH") for step in plan), plan
    assert meta.list_meta_prefix("") == []  # as SessionDB.list_meta_prefix: no prefix names no rows
