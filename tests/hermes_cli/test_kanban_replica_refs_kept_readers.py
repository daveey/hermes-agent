"""Phantom-ref re-resolution must not reopen every replica (or rescan the hub replica) on each board GET. [daveey]

Card E P1 (t_01a1d59e5543daa1): on hermes-hub one displayed card with an active ``suspected_hallucinated_references``
event made every ``GET /board`` open 21 SQLite connections (each Board replica twice plus hub.db) and scan ~20k
hub replica task keys for its short-form ref: ~79 ms of a ~130 ms GET. The answer depends only on rows that rarely
change, so lookups use the kept read-only readers and the hub scan is reused while the replica's task-key probe
(count, count(seq), max(seq), total(seq)) is unchanged. Every answer stays exactly what a fresh lookup returns.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from hermes_cli import kanban_board_replica_refs as refs
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_generation
from hermes_cli import kanban_prose_refs
from hermes_cli import kanban_session_activity as ksa
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli import kanban_sync_support as support

BOARD_CARD = "t_01a1cf4d49ca68bb"
HUB_ONLY = "t_01a1cf00000a0b0c"
NOWHERE_PREFIX = "t_203c2f81"
NOWHERE = "t_01a1cfdeadbeef00"


def _hub(state_dir, rows=(), *, synced_ago=2.0):
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(state_dir / "hub.db")
    conn.execute("CREATE TABLE replica (gw TEXT, coll TEXT, key TEXT, version INTEGER, seq INTEGER, data TEXT, "
                 "updated_at INTEGER, PRIMARY KEY (gw, coll, key))")
    conn.execute("CREATE INDEX replica_poll_metadata ON replica(coll, gw, seq)")
    conn.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO meta VALUES ('gateway', 'worker-1')")
    conn.execute("INSERT INTO meta VALUES ('last_ok', ?)", (str(time.time() - synced_ago),))
    for seq, (gw, key) in enumerate(rows, 1):
        _hub_put(conn, gw, key, seq)
    conn.commit()
    conn.close()
    return state_dir / "hub.db"


def _hub_put(conn, gw, key, seq):
    conn.execute("INSERT OR REPLACE INTO replica VALUES (?, 'tasks', ?, 1, ?, '{}', 0)", (gw, key, seq))


def _write(path, *sql):
    conn = sqlite3.connect(path)
    try:
        for stmt, args in sql:
            conn.execute(stmt, args)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def host(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hubstate"))
    support.write_sync_config(home, True)
    hub = _hub(tmp_path / "hubstate", [("gw-a", f"default/{HUB_ONLY}"), ("gw-b", "p/default/t_01a1cf0000000001")])
    for board in ("bb", "default"):
        replica = kbc.connect(ksc.shadow_path(board))
        try:
            with kbc.write_txn(replica):
                if board == "default":
                    replica.execute("INSERT INTO tasks (id, title, status, created_at, created_by, created_on) "
                                    "VALUES (?, 't', 'todo', 0, 'default', 'hub')", (BOARD_CARD,))
        finally:
            replica.close()
    _reset = getattr(refs, "reset", lambda: None)  # absent before the memo existed
    _reset()
    yield hub
    _reset()


@pytest.fixture
def opens(monkeypatch):
    """Every new SQLite connection the lookups open (one-shot opens and kept-reader (re)opens alike)."""
    seen = []
    real_gen, real_ksa = kanban_generation.open_ro, ksa._open_ro

    def spy(real):
        def open_ro(path, *a, **k):
            seen.append(str(path))
            return real(path, *a, **k)
        return open_ro

    monkeypatch.setattr(kanban_generation, "open_ro", spy(real_gen))
    monkeypatch.setattr(ksa, "_open_ro", spy(real_ksa))
    return seen


@pytest.fixture
def hub_scans(monkeypatch):
    calls = []
    real = kanban_prose_refs.hub_replica_lookup

    def spy(ref, *after):
        calls.append(ref if not after or after[0] is None else (ref, after[0]))  # (ref, seq): rows after seq only
        return real(ref, *after)

    monkeypatch.setattr(kanban_prose_refs, "hub_replica_lookup", spy)
    return calls


MISSING = [BOARD_CARD, BOARD_CARD[:10], HUB_ONLY, NOWHERE_PREFIX, NOWHERE]


def test_answers_match_the_replicas(host):
    assert refs.unresolved_after_replica(list(MISSING)) == [NOWHERE_PREFIX, NOWHERE]


def test_a_repeat_lookup_opens_no_connection(host, opens):
    """RED before: each call opened every Board replica twice plus hub.db."""
    first = refs.unresolved_after_replica(list(MISSING))
    opens.clear()
    for _ in range(3):
        assert refs.unresolved_after_replica(list(MISSING)) == first
    assert opens == []


def test_unchanged_hub_task_rows_are_not_rescanned(host, hub_scans):
    """RED before: every call scanned the hub replica once per unresolved ref."""
    refs.unresolved_after_replica(list(MISSING))
    assert hub_scans
    hub_scans.clear()
    _write(host, ("UPDATE meta SET v = ? WHERE k = 'last_ok'", (str(time.time()),)))  # a pull with no task rows
    assert refs.unresolved_after_replica(list(MISSING)) == [NOWHERE_PREFIX, NOWHERE]
    assert hub_scans == []


def test_a_pull_rechecks_absent_refs_against_new_rows_only(host, hub_scans):
    """Task rows moved: a known-absent ref can only have appeared in a row written after the old max(seq); a
    known-present one is looked up in full again (its row may have gone)."""
    assert refs.unresolved_after_replica([HUB_ONLY, NOWHERE_PREFIX, NOWHERE]) == [NOWHERE_PREFIX, NOWHERE]
    hub_scans.clear()
    _write(host, ("INSERT INTO replica VALUES ('gw-c', 'tasks', 'default/t_01a1cf0000000777', 1, 70, '{}', 0)", ()))
    assert refs.unresolved_after_replica([HUB_ONLY, NOWHERE_PREFIX, NOWHERE]) == [NOWHERE_PREFIX, NOWHERE]
    assert sorted(map(str, hub_scans)) == sorted(map(str, [(NOWHERE_PREFIX, 2), (NOWHERE, 2), HUB_ONLY]))
    hub_scans.clear()
    _write(host, ("INSERT INTO replica VALUES ('gw-c', 'tasks', ?, 1, 71, '{}', 0)", (f"p/b/{NOWHERE}",)))
    assert refs.unresolved_after_replica([HUB_ONLY, NOWHERE_PREFIX, NOWHERE]) == [NOWHERE_PREFIX]
    assert sorted(map(str, hub_scans)) == sorted(map(str, [(NOWHERE_PREFIX, 70), (NOWHERE, 70), HUB_ONLY]))


def test_a_lower_max_seq_rereads_in_full(host, hub_scans):
    """A feed that went back (a reset hub) proves nothing about rows above the old seq: full lookups."""
    _write(host, ("UPDATE replica SET seq = 40 WHERE gw = 'gw-b'", ()))
    assert refs.unresolved_after_replica([NOWHERE]) == [NOWHERE]
    hub_scans.clear()
    _write(host, ("UPDATE replica SET seq = 3 WHERE gw = 'gw-b'", ()),
           ("INSERT INTO replica VALUES ('gw-c', 'tasks', ?, 1, 2, '{}', 0)", (f"d/{NOWHERE}",)))
    assert refs.unresolved_after_replica([NOWHERE]) == []
    assert hub_scans == [NOWHERE]


def test_a_hub_row_landing_later_resolves_on_the_next_lookup(host):
    assert refs.unresolved_after_replica(list(MISSING)) == [NOWHERE_PREFIX, NOWHERE]
    _write(host, ("INSERT INTO replica VALUES ('gw-c', 'tasks', ?, 1, 99, '{}', 0)", (f"x/default/{NOWHERE}",)))
    assert refs.unresolved_after_replica(list(MISSING)) == [NOWHERE_PREFIX]
    _write(host, ("INSERT INTO replica VALUES ('gw-c', 'tasks', ?, 1, 100, '{}', 0)",
                  (f"default/{NOWHERE_PREFIX}00000000",)))
    assert refs.unresolved_after_replica(list(MISSING)) == []


def test_a_deleted_hub_row_is_unresolved_again(host):
    assert refs.unresolved_after_replica([HUB_ONLY, NOWHERE]) == [NOWHERE]
    _write(host, ("DELETE FROM replica WHERE key = ?", (f"default/{HUB_ONLY}",)))
    assert refs.unresolved_after_replica([HUB_ONLY, NOWHERE]) == [HUB_ONLY, NOWHERE]


def test_a_rewritten_key_at_a_new_seq_is_seen(host):
    """An in-place key rewrite keeps the row count; the hub stamps it with a new seq."""
    assert refs.unresolved_after_replica([HUB_ONLY]) == []
    _write(host, ("UPDATE replica SET key = 'default/t_01a1cf0000ffffff', seq = 50 WHERE key = ?",
                  (f"default/{HUB_ONLY}",)))
    assert refs.unresolved_after_replica([HUB_ONLY]) == [HUB_ONLY]


def test_rows_without_seq_are_never_memoized(host, hub_scans):
    _write(host, ("INSERT INTO replica VALUES ('gw-z', 'tasks', 'default/t_01a1cf000000abcd', 1, NULL, '{}', 0)", ()))
    refs.unresolved_after_replica([NOWHERE])
    hub_scans.clear()
    refs.unresolved_after_replica([NOWHERE])
    assert hub_scans == [NOWHERE]
    _write(host, ("UPDATE replica SET key = ? WHERE gw = 'gw-z'", (f"default/{NOWHERE}",)))  # seq still NULL
    assert refs.unresolved_after_replica([NOWHERE]) == []


def test_a_stale_hub_is_unknown_even_when_its_rows_are_memoized(host):
    assert refs.unresolved_after_replica([NOWHERE]) == [NOWHERE]
    _write(host, ("UPDATE meta SET v = ? WHERE k = 'last_ok'", (str(time.time() - 10 ** 6),)))
    assert refs.unresolved_after_replica([NOWHERE]) == []  # unknown, never flagged
    found, fresh = ksa.replica_task_resolution([HUB_ONLY, NOWHERE])
    assert found == {HUB_ONLY} and fresh is False


def test_a_replaced_hub_file_is_reread(host, tmp_path):
    assert refs.unresolved_after_replica([HUB_ONLY]) == []
    host.unlink()
    for side in ("-wal", "-shm"):
        host.with_name(host.name + side).unlink(missing_ok=True)
    _hub(tmp_path / "hubstate", [("gw-a", "default/t_01a1cf0000000002")])  # same probe shape, other keys
    assert refs.unresolved_after_replica([HUB_ONLY]) == [HUB_ONLY]


@pytest.mark.parametrize("seam", ["metadata", "lookup"])
def test_replacement_during_read_does_not_poison_next_generation(host, tmp_path, monkeypatch, seam):
    replacement = _hub(tmp_path / "replacement", [
        ("gw-a", f"default/{NOWHERE}"), ("gw-b", "p/default/t_01a1cf0000000001")])
    original_ident = refs._file_ident(host)
    replaced = False

    def replace_once():
        nonlocal replaced
        if not replaced:
            replacement.replace(host)
            replaced = True
            assert refs._file_ident(host) != original_ident

    if seam == "metadata":
        original = ksa._meta_float

        def meta(conn, key):
            result = original(conn, key)
            if key == "last_digest":
                replace_once()
            return result

        monkeypatch.setattr(ksa, "_meta_float", meta)
    else:
        original = kanban_prose_refs.hub_replica_lookup

        def lookup(*args):
            replace_once()
            return original(*args)

        monkeypatch.setattr(kanban_prose_refs, "hub_replica_lookup", lookup)

    # The in-flight snapshot may answer from the old file, but cannot poison
    # the next borrowed generation with either a false positive or negative.
    assert ksa.replica_task_resolution([HUB_ONLY, NOWHERE]) == ({HUB_ONLY}, True)
    assert replaced
    assert ksa.replica_task_resolution([HUB_ONLY, NOWHERE]) == ({NOWHERE}, True)


def test_a_board_replica_row_landing_later_is_seen_through_the_kept_reader(host):
    assert refs.unresolved_after_replica([NOWHERE]) == [NOWHERE]
    replica = kbc.connect(ksc.shadow_path("bb"))
    try:
        with kbc.write_txn(replica):
            replica.execute("INSERT INTO tasks (id, title, status, created_at, created_by, created_on) "
                            "VALUES (?, 't', 'todo', 0, 'default', 'hub')", (NOWHERE,))
    finally:
        replica.close()
    assert refs.unresolved_after_replica([NOWHERE]) == []
    assert refs.replica_tasks([NOWHERE])[0] == {NOWHERE: "default"}


def test_an_unreadable_board_replica_still_skips_and_others_answer(host):
    ksc.shadow_path("aaa-broken").write_bytes(b"not a sqlite file" * 64)
    assert refs.unresolved_after_replica([BOARD_CARD, NOWHERE]) == [NOWHERE]
    assert refs.unresolved_after_replica([BOARD_CARD, NOWHERE]) == [NOWHERE]
