"""A links pass keys its state.db reads by value: an unchanged board is reconciled again only when one of the
answers it used changed, not every ``LINKS_MAX_AGE_S``. [daveey]

David-MBP 2026-11-19 (task t_01a1f28c5c1c1bb4): about 24 of 47 board reconciles in 15 minutes were the 5-minute
max-age re-run of boards whose inputs had not changed, only to re-read session sources that had not changed either.
"""
import contextvars
import sqlite3
import threading

import pytest

from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_creator_chats as creator_chats
from hermes_cli import kanban_links_chat_key as chat_key
from hermes_cli import kanban_lockguard
from hermes_cli import kanban_session_provenance as provenance
from tests.hermes_cli import test_kanban_sync_steady_links as helpers

kanban_home = helpers.kanban_home
ledger = helpers.ledger
live = helpers.live
scope = helpers.scope
spies = helpers.spies
ksc = helpers.ksc
ME, OTHER, BOARD = helpers.ME, helpers.OTHER, helpers.BOARD


@pytest.fixture(autouse=True)
def cold_source_cache():
    provenance._source_cache.clear()
    yield
    provenance._source_cache.clear()


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(ksc, "_links_clock", lambda: now[0])
    return now


def _state(home, rows=(), *, table=True):
    with sqlite3.connect(home / "state.db") as state:
        if table:
            state.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, source TEXT, created_source TEXT)")
        else:
            state.execute("CREATE TABLE IF NOT EXISTS other (id TEXT)")
        for sid, source in rows:
            state.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?, ?)", (sid, source, source))
    state.close()
    return True


def _creator(live):
    return live.execute("SELECT id FROM tasks WHERE title='creator'").fetchone()[0]


def test_unchanged_chat_sources_skip_beyond_old_max_age(live, ledger, kanban_home, spies, scope, clock):
    _state(kanban_home, [("s_creator", "desktop")])
    settled = helpers._settled(live, ledger, kanban_home, spies)
    for _ in range(3):
        clock[0] += 601.0
        helpers._pass(kanban_home)
    assert spies == {"reconcile": 0, "backfill": 0}
    assert helpers._links(live) == settled


def test_a_board_without_any_state_db_skips_beyond_old_max_age(live, ledger, kanban_home, spies, scope, clock):
    assert not (kanban_home / "state.db").exists()
    settled = helpers._settled(live, ledger, kanban_home, spies)
    clock[0] += 601.0
    helpers._pass(kanban_home)
    assert spies == {"reconcile": 0, "backfill": 0}
    assert helpers._links(live) == settled


def test_changed_chat_source_ignores_warm_provenance_ttl(live, ledger, kanban_home, spies, scope):
    _state(kanban_home, [("s_creator", "desktop")])
    helpers._settled(live, ledger, kanban_home, spies)
    tid = _creator(live)
    assert (tid, "s_creator", "created", 0) in helpers._links(live)
    _state(kanban_home, [("s_creator", "kanban")])
    helpers._pass(kanban_home)
    assert spies["reconcile"] == 1
    assert (tid, "s_creator", "created", 1) in helpers._links(live)


def test_a_session_that_appears_ignores_the_missing_source_ttl(live, ledger, kanban_home, spies, scope):
    """The negative answer is keyed too: a session row written after the pass re-arms the next one."""
    _state(kanban_home)
    helpers._settled(live, ledger, kanban_home, spies)
    tid = _creator(live)
    assert (tid, "s_creator", "created", 0) in helpers._links(live)
    _state(kanban_home, [("s_creator", "cron")])
    helpers._pass(kanban_home)
    assert spies["reconcile"] == 1
    assert (tid, "s_creator", "created", 1) in helpers._links(live)


def test_a_state_db_that_appears_reconciles_on_the_next_pass(live, ledger, kanban_home, spies, scope):
    helpers._settled(live, ledger, kanban_home, spies)
    tid = _creator(live)
    _state(kanban_home, [("s_creator", "kanban")])
    helpers._pass(kanban_home)
    assert spies["reconcile"] == 1
    assert (tid, "s_creator", "created", 1) in helpers._links(live)


def test_an_unrelated_state_db_commit_does_not_reconcile(live, ledger, kanban_home, spies, scope):
    """state.db commits on every chat turn; only an answer the pass used is its key."""
    _state(kanban_home, [("s_creator", "desktop")])
    helpers._settled(live, ledger, kanban_home, spies)
    _state(kanban_home, [("s_unrelated", "kanban")])
    helpers._pass(kanban_home)
    assert spies == {"reconcile": 0, "backfill": 0}


def test_a_source_committed_during_the_pass_rearms_the_next_one(live, ledger, kanban_home, spies, scope, monkeypatch):
    """The key is what the reconcile read, not a read made after it."""
    _state(kanban_home, [("s_creator", "desktop")])
    helpers._settled(live, ledger, kanban_home, spies)
    tid, spied, wrote = _creator(live), links.reconcile, []

    def concurrent(conn, *a, **k):
        out = spied(conn, *a, **k)
        if not wrote:
            wrote.append(_state(kanban_home, [("s_creator", "kanban")]))
        return out

    monkeypatch.setattr(links, "reconcile", concurrent)
    ledger.append(helpers._ledger_row("t_more", "s_chat"))  # arm one pass
    helpers._pass(kanban_home)
    assert wrote and (tid, "s_creator", "created", 0) in helpers._links(live)
    helpers._pass(kanban_home)
    assert (tid, "s_creator", "created", 1) in helpers._links(live)


def test_an_unreadable_state_db_keeps_the_max_age_retry(live, ledger, kanban_home, spies, scope, clock):
    """A swallowed read error records no key: the board is retried by age, and keyed once the store reads."""
    _state(kanban_home, table=False)  # a store without a sessions table: every source read fails
    helpers._settled(live, ledger, kanban_home, spies)
    tid = _creator(live)
    assert ksc._LINKS_DONE[str(kanban_home / "kanban.db")][2] is None
    _state(kanban_home, [("s_creator", "kanban")])
    clock[0] += ksc.LINKS_MAX_AGE_S - 1
    helpers._pass(kanban_home)
    assert spies["reconcile"] == 0
    clock[0] += 2
    helpers._pass(kanban_home)
    assert spies["reconcile"] == 1
    assert (tid, "s_creator", "created", 1) in helpers._links(live)
    for _ in range(3):  # settle the release, then the board is keyed and no age applies
        helpers._pass(kanban_home)
    spies["reconcile"] = 0
    clock[0] += 601.0
    helpers._pass(kanban_home)
    assert spies["reconcile"] == 0


def test_a_new_profile_home_reconciles_on_the_next_pass(live, ledger, kanban_home, spies, scope, tmp_path, monkeypatch):
    homes = [kanban_home]
    monkeypatch.setattr(provenance, "_discover_homes", lambda profile: list(homes))
    helpers._settled(live, ledger, kanban_home, spies)
    helpers._pass(kanban_home)
    assert spies["reconcile"] == 0
    other = tmp_path / "other-profile"
    other.mkdir()
    _state(other, [("s_creator", "kanban")])
    homes.append(other)
    helpers._pass(kanban_home)
    assert spies["reconcile"] == 1
    assert (_creator(live), "s_creator", "created", 1) in helpers._links(live)


def test_each_session_is_read_once_per_pass_and_the_cache_serves_everyone_else(kanban_home, monkeypatch):
    _state(kanban_home, [("sid", "desktop")])
    reads = []
    real = provenance._read_sources
    monkeypatch.setattr(provenance, "_read_sources", lambda path, todo: reads.append(list(todo)) or real(path, todo))
    assert provenance._local_session_sources(["sid"], home=kanban_home) == {"sid": "desktop"}
    with chat_key.pass_reads() as pass_:
        assert provenance._local_session_sources(["sid"], home=kanban_home) == {"sid": "desktop"}
        assert reads == [["sid"]], "outside a board's reconcile the shared cache answers"
        _state(kanban_home, [("sid", "kanban")])
        with pass_.board():
            assert provenance._local_session_sources(["sid", "gone"], home=kanban_home) == {"sid": "kanban", "gone": None}
            assert provenance._local_session_sources(["gone", "sid"], home=kanban_home) == {"gone": None, "sid": "kanban"}
        assert reads == [["sid"], ["sid", "gone"]]
        key = str(kanban_home / "state.db")
        assert pass_.key() == {("source", key, "sid"): "kanban", ("source", key, "gone"): None}
    assert provenance._local_session_sources(["sid"], home=kanban_home) == {"sid": "kanban"}
    assert len(reads) == 2, "the pass refreshed the shared cache"


def test_a_failed_read_is_not_this_passes_answer_for_the_next_board(kanban_home):
    """Two boards share a session: the second must not take the first one's failed read as a certified answer."""
    _state(kanban_home, table=False)
    with chat_key.pass_reads() as pass_:
        for _ in range(2):
            with pass_.board():
                assert provenance._local_session_sources(["sid"], home=kanban_home) == {"sid": None}
            assert pass_.key() is None
        assert pass_.memo == {}


def test_a_thread_that_copied_the_context_does_not_join_the_pass():
    seen = []
    with chat_key.pass_reads() as pass_, pass_.board():
        assert chat_key.active() is pass_
        thread = threading.Thread(target=contextvars.copy_context().run, args=(lambda: seen.append(chat_key.active()),))
        thread.start()
        thread.join()
        chat_key.degrade()
    assert seen == [None]
    assert pass_.key() is None and chat_key.active() is None


def test_a_reconcile_that_raises_leaves_no_key():
    with chat_key.pass_reads() as pass_:
        with pass_.board():
            pass
        assert pass_.key() == {}
        with pytest.raises(RuntimeError), pass_.board():
            raise RuntimeError("reconcile failed")
        assert pass_.key() is None and chat_key.active() is None


def _replays(recorded):
    with chat_key.pass_reads() as pass_:
        return pass_.unchanged(recorded)


def _chain_db(path, rows):
    """A real state.db: each ``(id, parent, end_reason)`` session, in order."""
    from hermes_state import SessionDB
    state = SessionDB(db_path=path)
    try:
        for sid, parent, end_reason in rows:
            state.create_session(sid, "cli", **({"parent_session_id": parent} if parent else {}))
            if end_reason:
                state.end_session(sid, end_reason)
    finally:
        state.close()


def _without_sessions(path):
    for name in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        name.unlink(missing_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE other (id TEXT)")
    db.close()


def test_a_compression_step_is_keyed_by_its_answer(tmp_path):
    path = tmp_path / "state.db"
    _chain_db(path, [("root", None, "compression"), ("tip", "root", None)])
    with chat_key.pass_reads() as pass_, pass_.board():
        db = sqlite3.connect(path)
        try:
            assert chat_key.chain_step(db, path, "root") == "tip"
            assert chat_key.chain_step(db, path, "tip") is None
            _chain_db(path, [("later", "root", "compression")])
            assert chat_key.chain_step(db, path, "root") == "tip", "one answer per pass"
        finally:
            db.close()
    recorded = pass_.key()
    assert recorded == {("step", str(path), "root"): "tip", ("step", str(path), "tip"): None}
    assert not _replays(recorded)
    assert _replays({**recorded, ("step", str(path), "root"): "later"})
    _without_sessions(path)
    assert not _replays(recorded), "an unreadable store is a changed answer"


def test_store_existence_homes_and_unknown_reads_replay_by_value(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    assert _replays({("exists", str(path)): False}) and not _replays({("exists", str(path)): True})
    path.write_bytes(b"")
    assert _replays({("exists", str(path)): True}) and not _replays({("exists", str(path)): False})
    monkeypatch.setattr(provenance, "_discover_homes", lambda profile: [tmp_path / str(profile)])
    assert _replays({("homes", "p"): (str(tmp_path / "p"),)})
    assert not _replays({("homes", "p"): (str(tmp_path / "q"),)})
    assert not _replays({("future-kind", "x"): 1})
    assert _replays({})


def test_a_degraded_replay_is_a_changed_answer(kanban_home):
    key = ("source", str(kanban_home / "state.db"), "sid")
    assert _replays({key: None})
    _state(kanban_home, table=False)
    assert not _replays({key: None})


def _garbage(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a sqlite database" * 64)
    return path


def _degraded(call):
    with chat_key.pass_reads() as pass_:
        with pass_.board():
            call()
        return pass_.key() is None


class _Broken:
    def execute(self, *a, **k):
        raise sqlite3.OperationalError("disk I/O error")


def test_owner_release_and_replica_reader_errors_leave_no_key(live, ledger, kanban_home, scope):
    assert not _degraded(lambda: links._owner_written_releases(BOARD, ME, {}))
    assert not _degraded(lambda: creator_chats._ReplicaReader(live).open())
    _garbage(ksc.shadow_path(BOARD))
    assert _degraded(lambda: links._owner_written_releases(BOARD, ME, {}))
    assert _degraded(lambda: creator_chats._ReplicaReader(live).open())


def test_replica_resolve_and_verdict_read_errors_leave_no_key(live, ledger, kanban_home, scope):
    reader = creator_chats._ReplicaReader(live)
    assert not _degraded(lambda: reader.resolve("t_missing"))
    assert not _degraded(lambda: creator_chats.LinkVerdicts.read(live, reader))
    broken = creator_chats._ReplicaReader(live)
    broken.attempted, broken.replica = True, _Broken()
    assert _degraded(lambda: broken.resolve("t_missing"))
    broken.replica = None
    assert _degraded(lambda: creator_chats.LinkVerdicts.read(_Broken(), broken))


def test_owner_hub_link_errors_leave_no_key(live, ledger, kanban_home, scope, monkeypatch):
    tid = helpers.kb.create_task(live, title="owned", assignee="worker")
    with helpers.kbc.write_txn(live):
        live.execute("INSERT INTO kanban_notify_subs (task_id, platform, chat_id, created_at) VALUES (?, 'hub', ?, 1)",
                     (tid, f"{OTHER}/default/s_remote"))
    assert not _degraded(lambda: links._owner_hub_links(live, BOARD, ME))
    assert [link["chat_session"] for link in links._owner_hub_links(live, BOARD, ME)] == ["s_remote"]
    shadow = ksc.shadow_path(BOARD)
    shadow.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(shadow) as replica:  # a replica without the links table
        replica.execute("CREATE TABLE other (id TEXT)")
    replica.close()
    assert _degraded(lambda: links._owner_hub_links(live, BOARD, ME)), "a replica that opens but does not read"
    real = kanban_lockguard.connect

    def refuse(target, *a, **k):
        if "mode=ro" in str(target):
            raise sqlite3.OperationalError("unable to open database file")
        return real(target, *a, **k)

    monkeypatch.setattr(kanban_lockguard, "connect", refuse)
    assert _degraded(lambda: links._owner_hub_links(live, BOARD, ME)), "a replica that does not open"


class _FailingSubs:
    """A board connection whose subscription reads fail (a lock, a damaged page), everything else intact."""

    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, *a):
        if "FROM kanban_notify_subs" in sql:
            raise sqlite3.OperationalError("database is locked")
        return self.conn.execute(sql, *a)


def test_a_failed_subscription_read_leaves_no_key_but_a_board_without_the_table_does(live, ledger, kanban_home, scope):
    helpers._seed(live, ledger)
    assert not _degraded(lambda: links._local_links(live, ME))
    assert not _degraded(lambda: links._owner_hub_links(live, BOARD, ME))
    assert _degraded(lambda: links._local_links(_FailingSubs(live), ME))
    assert _degraded(lambda: links._owner_hub_links(_FailingSubs(live), BOARD, ME))
    links._local_links(_FailingSubs(live), ME)  # outside a pass: answered as before, no extra read
    with helpers.kbc.write_txn(live):
        live.execute("ALTER TABLE kanban_notify_subs RENAME TO subs_gone")
    assert not _degraded(lambda: links._local_links(live, ME))
    assert not _degraded(lambda: links._owner_hub_links(live, BOARD, ME))


def test_compression_lineage_errors_leave_no_key(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(provenance, "_discover_homes", lambda profile: [home])
    rows = [{"uid": "u_old", "task_id": "t1", "chat_profile": "default", "chat_session": "root", "status": "running"}]
    want = {"u_new": {"task_id": "t1", "chat_profile": "default", "chat_session": "tip", "chat_gateway": ME,
                      "released": 0}}
    with chat_key.pass_reads() as pass_:
        with pass_.board():
            assert links._compressed_away(rows, want, ME) == set()
        assert pass_.key() == {("homes", "default"): (str(home),), ("exists", str(home / "state.db")): False}
    _chain_db(home / "state.db", [("root", None, "compression"), ("tip", "root", None)])
    with chat_key.pass_reads() as pass_:
        with pass_.board():
            assert links._compressed_away(rows, want, ME) == {"u_old"}
        assert pass_.key()[("step", str(home / "state.db"), "root")] == "tip"
    assert links._compressed_away(rows, want, ME) == {"u_old"}, "outside a pass the lineage reads as before"
    _without_sessions(home / "state.db")
    assert _degraded(lambda: links._compressed_away(rows, want, ME)), "a store whose lineage query fails"
    _garbage(home / "state.db")
    assert _degraded(lambda: links._compressed_away(rows, want, ME)), "a store that does not open"
