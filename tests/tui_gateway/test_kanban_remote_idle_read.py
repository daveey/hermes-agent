"""An unchanged hub replica costs the notice readers one ``PRAGMA data_version`` on a retained observer connection,
not a replica read; every commit is read on the next poll, including one published after its WAL frames."""
import json
import os
import selectors
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.tui_gateway.test_kanban_remote_arrival import CHAT, OTHER, _card, _hub as _ready_hub
from tui_gateway import kanban_remote_notices as krn
from tui_gateway import kanban_remote_observations as obs

NOW = 1_800_000_000.0


def _replica(directory, cards, mode="delete"):
    directory.mkdir(parents=True, exist_ok=True)
    path = _ready_hub(directory, cards)
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA journal_mode={mode}")
    return path


def _index(path):
    with krn._lock:
        return krn._index(path)


def _chat_keys(path, session=CHAT):
    return sorted(card["key"] for card in _index(path)[1].get(("default", session), ()))


def _states(fresh):
    return sorted((key, state) for (_gw, key, state) in fresh)


def _insert(path, card, seq):
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO replica VALUES ('hub','tasks',?,?,?)", (card["key"], json.dumps(card["task"]), seq))


def _observer():
    from tui_gateway import kanban_replica_observer
    return kanban_replica_observer


def _held(replica):
    return _observer()._observers[os.path.realpath(replica)][0]


def _key(replica):
    key = _observer().fingerprint(replica)
    assert key is not None
    return key


def _closed(conn):
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


def _decodes(monkeypatch):
    """Ids of the task rows ``observe`` decodes from here on."""
    seen, real = [], obs._summary
    monkeypatch.setattr(obs, "_summary", lambda task: seen.append(task.get("id")) or real(task))
    return seen


def _forbid_sqlite(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("an unchanged replica was opened, decoded or written to the observation ledger")

    monkeypatch.setattr(krn, "_ro", forbidden)
    monkeypatch.setattr(krn, "_task_rows", forbidden)
    monkeypatch.setattr(obs, "_persist", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)


@pytest.mark.parametrize("mode", ["delete", "wal", "wal-writer-open"])
def test_unchanged_replica_is_not_opened_by_either_reader(tmp_path, monkeypatch, mode):
    replica = _replica(tmp_path, [_card("t_idle", CHAT, status="done", at=NOW)], mode.split("-")[0])
    writer = sqlite3.connect(replica) if mode == "wal-writer-open" else None  # keeps a live -wal file
    try:
        if writer is not None:
            writer.execute("UPDATE meta SET v=v WHERE k='gateway'")
            writer.commit()
            assert os.path.exists(f"{replica}-wal")
        ledger = tmp_path / "ledger.db"
        krn._reset_for_tests()
        index = _index(replica)
        fresh = obs.observe(replica, ledger, NOW)
        _index(replica), obs.observe(replica, ledger, NOW)  # settle: reads that raced their own close re-read once
        assert index[1] and fresh

        traced = []
        _held(replica).set_trace_callback(traced.append)
        _forbid_sqlite(monkeypatch)
        for _ in range(3):
            assert _index(replica) == index
            assert obs.observe(replica, ledger, NOW + krn.FRESH_S + 1) == fresh
        assert traced and set(traced) == {"PRAGMA data_version"}  # the change key is the only statement
    finally:
        if writer is not None:
            writer.close()


@pytest.mark.parametrize("mode", ["delete", "wal"])
def test_every_commit_is_read_on_the_next_poll(tmp_path, mode):
    replica = _replica(tmp_path, [], mode)
    ledger = tmp_path / "ledger.db"
    writer = sqlite3.connect(replica)
    try:
        krn._reset_for_tests()
        assert _chat_keys(replica) == [] and obs.observe(replica, ledger, NOW) == {}
        done = _card("t_commit", CHAT, status="done", at=NOW)
        writer.execute("INSERT INTO replica VALUES ('hub','tasks',?,?,1)", (done["key"], json.dumps(done["task"])))
        writer.commit()
        assert _chat_keys(replica) == [done["key"]]
        assert _states(obs.observe(replica, ledger, NOW + 1)) == [(done["key"], f"done@{int(NOW)}")]
        blocked = _card("t_commit", CHAT, status="blocked", at=NOW + 5)
        writer.execute("UPDATE replica SET data=?, seq=2 WHERE key=?", (json.dumps(blocked["task"]), done["key"]))
        writer.commit()
        assert _index(replica)[1][("default", CHAT)][0]["task"]["status"] == "blocked"
        assert (done["key"], f"blocked@{int(NOW) + 5}") in _states(obs.observe(replica, ledger, NOW + 6))
    finally:
        writer.close()


def test_metadata_only_changes_are_read(tmp_path):
    card = _card("t_meta", CHAT, status="done", at=NOW)
    replica = _replica(tmp_path, [card])
    ledger = tmp_path / "ledger.db"
    with sqlite3.connect(replica) as conn:
        conn.execute("UPDATE meta SET v='101' WHERE k='head'")
    krn._reset_for_tests()
    assert obs.observe(replica, ledger, NOW) == {}
    assert obs.observe(replica, ledger, NOW) == {}
    assert _chat_keys(replica) == [card["key"]]
    with sqlite3.connect(replica) as conn:
        conn.execute("UPDATE meta SET v='101' WHERE k='cursor'")  # catch-up completes, no task row moves
    assert _states(obs.observe(replica, ledger, NOW)) == [(card["key"], f"done@{int(NOW)}")]
    with sqlite3.connect(replica) as conn:
        conn.execute("UPDATE meta SET v='hub' WHERE k='gateway'")  # the hub's own cards notify locally
    assert _chat_keys(replica) == []


@pytest.mark.parametrize("aggregate", ["equal", "moved"])
@pytest.mark.parametrize("how", ["replace", "recreate"])
def test_replaced_replica_is_reread_even_when_seqs_match(tmp_path, aggregate, how):
    """Same keys and seqs, different contents: neither the index nor the row decode cache may survive."""
    a, b = (_card(t, CHAT, status="done", at=NOW - i) for i, t in enumerate(("t_a", "t_b")))
    replica = _replica(tmp_path / "live", [a, b])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    assert _chat_keys(replica) == sorted([a["key"], b["key"]])
    assert len(obs.observe(replica, ledger, NOW)) == 2

    moved_a = _card("t_a", OTHER, status="blocked", at=NOW + 9)  # same key and seq as a
    if how == "replace":
        staged = _replica(tmp_path / "stage", [moved_a, b])
    else:
        replica.unlink()
        with pytest.raises(sqlite3.Error):
            _index(replica)
        with pytest.raises(sqlite3.Error):
            obs.observe(replica, ledger, NOW)
        staged = replica
        _replica(tmp_path / "live", [moved_a, b])
    if aggregate == "moved":
        with sqlite3.connect(staged) as conn:
            conn.execute("UPDATE replica SET seq=99 WHERE key=?", (b["key"],))
    if how == "replace":
        os.replace(staged, replica)

    assert _chat_keys(replica) == [b["key"]]
    assert _chat_keys(replica, OTHER) == [a["key"]]
    assert (a["key"], f"blocked@{int(NOW) + 9}") in _states(obs.observe(replica, ledger, NOW + 10))


def test_commit_during_a_read_is_read_on_the_next_poll(tmp_path, monkeypatch):
    replica = _replica(tmp_path, [])
    ledger = tmp_path / "ledger.db"
    late = [_card("t_late_index", CHAT, status="done", at=NOW), _card("t_late_obs", CHAT, status="done", at=NOW)]
    real_rows, real_snapshot = krn._task_rows, obs._snapshot

    def rows_then_commit(conn, *args):
        rows = real_rows(conn, *args)
        _insert(replica, late[0], 1)
        return rows

    def snapshot_then_commit(*args):
        got = real_snapshot(*args)
        _insert(replica, late[1], 2)
        return got

    krn._reset_for_tests()
    monkeypatch.setattr(krn, "_task_rows", rows_then_commit)
    assert _chat_keys(replica) == []
    monkeypatch.setattr(krn, "_task_rows", real_rows)
    assert _chat_keys(replica) == [late[0]["key"]]

    monkeypatch.setattr(obs, "_snapshot", snapshot_then_commit)
    assert [key for key, _ in _states(obs.observe(replica, ledger, NOW))] == [late[0]["key"]]
    monkeypatch.setattr(obs, "_snapshot", real_snapshot)
    assert [key for key, _ in _states(obs.observe(replica, ledger, NOW))] == sorted(c["key"] for c in late)


def test_failed_reads_recover_and_unstatable_replicas_are_always_read(tmp_path, monkeypatch):
    replica = _replica(tmp_path, [])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    assert _chat_keys(replica) == [] and obs.observe(replica, ledger, NOW) == {}
    card = _card("t_recover", CHAT, status="done", at=NOW)
    _insert(replica, card, 1)
    real_ro = krn._ro

    def unavailable(path):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(krn, "_ro", unavailable)
    with pytest.raises(sqlite3.Error):
        _index(replica)
    with pytest.raises(sqlite3.Error):
        obs.observe(replica, ledger, NOW)
    opened = []
    monkeypatch.setattr(krn, "_ro", lambda path: opened.append(path) or real_ro(path))
    assert _chat_keys(replica) == [card["key"]]
    assert len(obs.observe(replica, ledger, NOW)) == 1

    real_stat = os.stat

    def stat(path, *args, **kwargs):
        if str(path).startswith(str(replica)):
            raise PermissionError(path)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    opened.clear()
    for _ in range(2):
        assert _chat_keys(replica) == [card["key"]]
        assert len(obs.observe(replica, ledger, NOW)) == 1
    assert len(opened) == 4  # no stamp, no skip


def test_replaced_ledger_is_rewritten_on_an_unchanged_replica(tmp_path, monkeypatch):
    card = _card("t_ledger", CHAT, status="done", at=NOW)
    replica = _replica(tmp_path, [card])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    first = obs.observe(replica, ledger, NOW)
    sqlite3.connect(tmp_path / "new-ledger.db").close()
    os.replace(tmp_path / "new-ledger.db", ledger)
    persisted = []
    real_persist = obs._persist
    monkeypatch.setattr(obs, "_persist", lambda *a: persisted.append(a) or real_persist(*a))
    assert obs.observe(replica, ledger, NOW) == first
    assert len(persisted) == 1
    with sqlite3.connect(ledger) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1


def test_replicas_at_different_paths_do_not_share_results(tmp_path):
    one = _replica(tmp_path / "one", [_card("t_one", CHAT, status="done", at=NOW)])
    two = _replica(tmp_path / "two", [_card("t_two", CHAT, status="blocked", at=NOW)])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    for _ in range(3):
        for path, key, status in ((one, "t_one", "done"), (two, "t_two", "blocked")):
            assert _chat_keys(path) == [f"default/default/{key}"]
            assert _states(obs.observe(path, ledger, NOW)) == [(f"default/default/{key}", f"{status}@{int(NOW)}")]


def test_unchanged_replica_still_releases_reoffers_and_expires_claims(tmp_path):
    card = _card("t_claim", CHAT, status="done", at=NOW)
    replica = _replica(tmp_path, [card])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    first = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=NOW)
    assert len(first) == 1
    assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=NOW + 1) == []  # held in flight
    assert krn.release(first[0], ledger=ledger)
    assert [c["ref"] for c in krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=NOW + 2)] == \
        [first[0]["ref"]]
    assert krn.release(first[0], ledger=ledger)
    assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=NOW + krn.FRESH_S + 1) == []


def _rewrite(path, data):
    with open(path, "r+b") as handle:  # in place: every connection maps this file
        handle.write(data)
        handle.truncate()


def test_wal_commit_published_after_its_frames_is_read_on_the_next_poll(tmp_path):
    """A WAL commit appends and syncs its frames, then publishes them in the -shm index. A poll in between sees
    the new -wal and the old data; when the commit is published no file but the -shm changes."""
    replica = _replica(tmp_path, [], "wal")
    ledger = tmp_path / "ledger.db"
    keep, writer = sqlite3.connect(replica), sqlite3.connect(replica)  # the -wal outlives every poll
    try:
        keep.execute("SELECT 1 FROM meta").fetchall()
        krn._reset_for_tests()
        assert _chat_keys(replica) == [] and obs.observe(replica, ledger, NOW) == {}
        shm = Path(f"{replica}-shm")
        unpublished = shm.read_bytes()
        card = _card("t_gap", CHAT, status="done", at=NOW)
        writer.execute("INSERT INTO replica VALUES ('hub','tasks',?,?,1)", (card["key"], json.dumps(card["task"])))
        writer.commit()
        published = shm.read_bytes()
        assert published != unpublished
        _rewrite(shm, unpublished)  # back inside the gap: the frames are in -wal, the index does not cover them
        assert _chat_keys(replica) == []
        assert obs.observe(replica, ledger, NOW) == {}
        _rewrite(shm, published)  # the commit becomes visible
        assert _chat_keys(replica) == [card["key"]]
        assert _states(obs.observe(replica, ledger, NOW + 1)) == [(card["key"], f"done@{int(NOW)}")]
    finally:
        writer.close()
        keep.close()


_WRITER = textwrap.dedent("""
    import sqlite3, sys
    conn = sqlite3.connect(sys.argv[1], isolation_level=None)
    conn.execute("PRAGMA synchronous=FULL")
    for line in sys.stdin:
        seq, key, data = line.rstrip("\\n").split("\\t")
        conn.execute("INSERT INTO replica VALUES ('hub','tasks',?,?,?)", (key, data, int(seq)))
        print(seq, flush=True)
""")


def test_commits_from_another_process_are_each_read_after_they_return(tmp_path):
    """Smoke: poll without pause while a separate writer process commits; once a commit returns, both readers
    have it on the next poll (the stat-keyed draft missed 594 of 1000 in this loop)."""
    replica = _replica(tmp_path, [], "wal")
    ledger = tmp_path / "ledger.db"
    writer = subprocess.Popen([sys.executable, "-c", _WRITER, str(replica)], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True)
    commands, acks = writer.stdin, writer.stdout
    assert commands is not None and acks is not None
    selector = selectors.DefaultSelector()
    selector.register(acks, selectors.EVENT_READ)
    try:
        krn._reset_for_tests()
        keys, missed = [], []
        for i in range(150):
            card = _card(f"t_mp{i}", CHAT, status="done", at=NOW)
            commands.write(f"{i + 1}\t{card['key']}\t{json.dumps(card['task'])}\n")
            commands.flush()
            while not selector.select(0):
                _index(replica), obs.observe(replica, ledger, NOW)
            assert acks.readline().strip() == str(i + 1)
            keys.append(card["key"])
            if _chat_keys(replica) != sorted(keys) or len(obs.observe(replica, ledger, NOW)) != len(keys):
                missed.append(i)
        assert missed == []
    finally:
        commands.close()
        writer.wait(timeout=30)
        selector.close()


def test_observer_is_reused_and_holds_no_snapshot(tmp_path):
    replica = _replica(tmp_path, [_card("t_reuse", CHAT, status="done", at=NOW)], "wal")
    writer = sqlite3.connect(replica)
    try:
        ro = _observer()
        krn._reset_for_tests()
        first = _key(replica)
        conn = _held(replica)
        traced = []
        conn.set_trace_callback(traced.append)
        assert [ro.fingerprint(replica) for _ in range(3)] == [first] * 3
        assert _held(replica) is conn and len(ro._observers) == 1
        assert traced == ["PRAGMA data_version"] * 3 and not conn.in_transaction
        writer.execute("UPDATE meta SET v='102' WHERE k='head'")
        writer.commit()
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0  # no reader pins the WAL
        moved = _key(replica)
        assert moved != first and (moved.generation, moved.ident) == (first.generation, first.ident)
        krn._reset_for_tests()
        assert _closed(conn) and not ro._observers
    finally:
        writer.close()


def test_replaced_or_missing_replica_reopens_its_observer(tmp_path):
    card = _card("t_swap", CHAT, status="done", at=NOW)
    replica = _replica(tmp_path / "live", [card])
    ro = _observer()
    krn._reset_for_tests()
    first = _key(replica)
    conn = _held(replica)
    os.replace(_replica(tmp_path / "stage", [card]), replica)
    second = _key(replica)
    assert second.ident != first.ident and second.generation != first.generation and _closed(conn)
    conn = _held(replica)
    replica.unlink()
    assert ro.fingerprint(replica) is None
    assert _closed(conn) and os.path.realpath(replica) not in ro._observers
    _replica(tmp_path / "live", [card])
    assert _key(replica).generation not in (first.generation, second.generation)


def test_observers_are_bounded_and_eviction_keeps_decoded_rows(tmp_path, monkeypatch):
    ro = _observer()
    replicas = [_replica(tmp_path / f"r{i}", [_card(f"t_bound{i}", CHAT, status="done", at=NOW)])
                for i in range(ro.MAX_OBSERVERS + 1)]
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    assert len(obs.observe(replicas[0], ledger, NOW)) == 1
    first, conn = _key(replicas[0]), _held(replicas[0])
    for replica in replicas[1:]:
        assert ro.fingerprint(replica) is not None
    assert len(ro._observers) == ro.MAX_OBSERVERS and _closed(conn)
    assert os.path.realpath(replicas[0]) not in ro._observers
    late = _card("t_bound_late", CHAT, status="done", at=NOW)
    _insert(replicas[0], late, 7)
    decoded = _decodes(monkeypatch)
    again = _key(replicas[0])
    assert again.generation != first.generation and ro.file_identity(again) == ro.file_identity(first)
    assert len(obs.observe(replicas[0], ledger, NOW)) == 2
    assert decoded == ["t_bound_late"]  # an evicted observer is not a changed file


def test_observer_failure_is_read_uncached_and_recovers(tmp_path, monkeypatch):
    replica = _replica(tmp_path, [_card("t_fail", CHAT, status="done", at=NOW)], "wal")
    ledger = tmp_path / "ledger.db"
    ro = _observer()
    krn._reset_for_tests()
    index, fresh = _index(replica), obs.observe(replica, ledger, NOW)
    first, conn = _key(replica), _held(replica)
    real = ro._data_version

    def broken(connection):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ro, "_data_version", broken)
    opened, real_ro = [], krn._ro
    monkeypatch.setattr(krn, "_ro", lambda path: opened.append(path) or real_ro(path))
    decoded = _decodes(monkeypatch)
    assert ro.fingerprint(replica) is None and _closed(conn) and not ro._observers
    for _ in range(2):
        assert _index(replica) == index and obs.observe(replica, ledger, NOW) == fresh
    assert len(opened) == 4 and decoded == ["t_fail", "t_fail"]  # unknown: read, keep nothing that skips
    monkeypatch.setattr(ro, "_data_version", real)
    recovered = _key(replica)
    assert recovered.generation != first.generation
    _index(replica), obs.observe(replica, ledger, NOW)
    opened.clear()
    assert _index(replica) == index and obs.observe(replica, ledger, NOW) == fresh and opened == []


def test_commit_racing_a_read_keeps_unchanged_decoded_rows(tmp_path, monkeypatch):
    kept = [_card(f"t_keep{i}", CHAT, status="done", at=NOW) for i in range(3)]
    replica = _replica(tmp_path, kept, "wal")
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    assert len(obs.observe(replica, ledger, NOW)) == 3
    new, late = (_card(t, CHAT, status="done", at=NOW) for t in ("t_new", "t_late"))
    _insert(replica, new, 10)
    real_snapshot = obs._snapshot

    def snapshot_then_commit(*args):
        got = real_snapshot(*args)
        _insert(replica, late, 11)
        return got

    decoded = _decodes(monkeypatch)
    monkeypatch.setattr(obs, "_snapshot", snapshot_then_commit)
    assert len(obs.observe(replica, ledger, NOW)) == 4
    monkeypatch.setattr(obs, "_snapshot", real_snapshot)
    assert len(obs.observe(replica, ledger, NOW)) == 5
    assert decoded == ["t_new", "t_late"]  # the same file: only rows whose seq moved are decoded again
