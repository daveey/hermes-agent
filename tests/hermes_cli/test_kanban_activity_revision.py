"""The annotation's caches key on the content the readers read, not the board file (t_01a19b0b602137c1). [daveey]

On David-MBP the live kanban.db, the sync replicas and hub.db are rewritten ~61 times a minute by bookkeeping (sync
stamps, the dispatcher's pass stamp, a replica's pull stamp) with no task change; each write moved the stat-keyed
fingerprint, so every sidebar poll re-read the boards and re-projected the remote links. A bookkeeping-only write
must not force a recompute; a task write must, at once (read-your-writes).
"""

from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_activity_revision as rev
from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_session_activity as ksa
from hermes_cli import kanban_suspend_ledger as ledger
from tests.hermes_cli import kanban_sync_support as support

ME, OTHER = "macbook", "hub"


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))
    support.write_sync_config(home, True)  # the live layout: synced board with kanban_sync_state
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: [])
    ksa._reset_cache_for_tests()
    links._reset_for_tests()
    yield home
    ksa._reset_cache_for_tests()
    links._reset_for_tests()


@pytest.fixture
def board(home):
    conn = kbc.connect(home / "kanban.db")
    ledger.record_dispatch_pass(conn)  # the live board already has the dispatcher's stamp tables
    try:
        yield conn
    finally:
        conn.close()


def _count_board_reads(monkeypatch) -> list:
    reads: list = []
    real = ksa._read_board

    def traced(path, **kw):
        reads.append(path)
        return real(path, **kw)

    monkeypatch.setattr(ksa, "_read_board", traced)
    return reads


def _bookkeeping(conn) -> None:
    """What the live board saw with no task work: sync and dispatcher stamps."""
    sync.mark_pulled(conn)
    with kbc.write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET hlc_last = hlc_last || 'x'")
    ledger.record_dispatch_pass(conn)


def _expire_counts() -> None:
    with ksa._cache_lock:
        ksa._cache = None


def test_bookkeeping_writes_do_not_move_the_fingerprint_or_reread_the_board(board, monkeypatch):
    kb.create_task(board, title="work", assignee="worker", session_id="s_chat")
    reads = _count_board_reads(monkeypatch)
    before = ksa._activity_fingerprint()
    assert ksa.active_task_counts() == {"s_chat": 1} and len(reads) == 1
    _bookkeeping(board)
    assert ksa._activity_fingerprint() == before
    _expire_counts()  # even a recompute (TTL expired) serves the board from its memo
    assert ksa.active_task_counts() == {"s_chat": 1} and len(reads) == 1


def test_a_task_write_moves_the_fingerprint_and_is_read_at_once(board, monkeypatch):
    tid = kb.create_task(board, title="work", assignee="worker", session_id="s_chat")
    reads = _count_board_reads(monkeypatch)
    assert ksa.active_task_counts() == {"s_chat": 1}
    before = ksa._activity_fingerprint()
    kb.complete_task(board, tid, result="ok")
    assert ksa._activity_fingerprint() != before
    # Read-your-writes: within the count cache's TTL, the very next read sees the write.
    assert ksa.active_task_counts() == {} and len(reads) == 2


@pytest.mark.parametrize("write", [
    "INSERT INTO kanban_notify_subs (task_id, platform, chat_id, created_at) VALUES (:t, 'tui', 's_other', 1)",
    "INSERT INTO task_events (task_id, kind, created_at) VALUES (:t, 'completed', 1)",
    "INSERT INTO task_links (parent_id, child_id) VALUES ('t_parent', :t)",
    "UPDATE tasks SET status = 'blocked' WHERE id = :t",
])
def test_every_table_the_counts_read_moves_the_key(board, write):
    tid = kb.create_task(board, title="work", assignee="worker", session_id="s_chat")
    before = ksa._board_content_key(ksa._board_db_paths()[0])
    with kbc.write_txn(board):
        board.execute(write, {"t": tid})
    assert ksa._board_content_key(ksa._board_db_paths()[0]) != before


def test_projection_kinds_match_the_unseen_reader():
    assert set(rev.PROJECTION_EVENT_KINDS) == set(ksa.UNSEEN_KINDS)


def test_an_unread_event_wakes_notify_but_not_the_links_or_annotation_key(board):
    """A comment or claim event still moves the notify counter, and does not move the links or annotation key.
    An unseen kind and a task-status write move both. This is the notification-loss guard: dropping the
    notify bump would hide the event from the gate.
    """
    tid = kb.create_task(board, title="work", assignee="worker", session_id="s_chat")
    path = ksa._board_db_paths()[0]

    def notify_key():
        return rev.content_key(path, rev.LOCAL_TABLES, lambda p: ksa.cached_ro(p, busy_budget=0))

    before_notify = notify_key()
    before_fp = ksa._activity_fingerprint()
    before_links = ksa._links_fingerprint()
    assert before_fp and rev.is_revision(before_notify)
    with kbc.write_txn(board):
        board.execute("INSERT INTO task_events (task_id, kind, created_at) VALUES (?, 'commented', 1)", (tid,))
    assert notify_key() != before_notify  # notification-loss: the gate key must move
    assert ksa._board_content_key(path) != before_notify
    assert ksa._activity_fingerprint() == before_fp
    assert ksa._links_fingerprint() == before_links

    before_fp = ksa._activity_fingerprint()
    with kbc.write_txn(board):
        board.execute("INSERT INTO task_events (task_id, kind, created_at) VALUES (?, 'claimed', 2)", (tid,))
    assert ksa._activity_fingerprint() == before_fp
    assert notify_key() != before_notify

    before_fp = ksa._activity_fingerprint()
    before_notify = notify_key()
    with kbc.write_txn(board):
        board.execute("INSERT INTO task_events (task_id, kind, created_at) VALUES (?, 'completed', 3)", (tid,))
    assert ksa._activity_fingerprint() != before_fp
    assert notify_key() != before_notify

    before_fp = ksa._activity_fingerprint()
    with kbc.write_txn(board):
        board.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
    assert ksa._activity_fingerprint() != before_fp


def test_claim_and_heartbeat_columns_are_bookkeeping(board):
    tid = kb.create_task(board, title="work", assignee="worker", session_id="s_chat")
    path = ksa._board_db_paths()[0]
    before = ksa._board_content_key(path)
    with kbc.write_txn(board):
        board.execute("UPDATE tasks SET claim_expires = 99, last_heartbeat_at = 98, worker_pid = 7 WHERE id = ?",
                      (tid,))
    assert ksa._board_content_key(path) == before
    with kbc.write_txn(board):
        board.execute("UPDATE tasks SET title = 'renamed' WHERE id = ?", (tid,))
    assert ksa._board_content_key(path) != before


def test_a_missing_trigger_falls_back_to_the_file_stat(board, monkeypatch):
    kb.create_task(board, title="work", assignee="worker", session_id="s_chat")
    path = ksa._board_db_paths()[0]
    assert rev.is_revision(ksa._board_content_key(path))
    board.execute("DROP TRIGGER kanban_activity_rev_task_events_insert")
    board.commit()
    frozen_stat = rev.file_stat(path)
    monkeypatch.setattr(rev, "file_stat", lambda _path: frozen_stat)
    key = ksa._board_content_key(path)
    assert key[0] == "stat"  # the counter no longer vouches for task_events: never trusted
    assert ksa._board_content_key(path) == key
    _bookkeeping(board)
    assert ksa._board_content_key(path) != key  # even when the filesystem tick did not move


def test_reinstall_after_a_gap_moves_the_key(board, home):
    path = ksa._board_db_paths()[0]
    board.execute("DROP TRIGGER kanban_activity_rev_tasks_update")
    board.commit()
    gap = ksa._board_content_key(path)
    rev.ensure_revision(board, rev.LOCAL_TABLES)
    assert rev.is_revision(ksa._board_content_key(path)) and ksa._board_content_key(path) != gap


def test_drop_column_after_drop_triggers_reinstalls_for_the_new_columns(board, home):
    tid = kb.create_task(board, title="work", assignee="worker", session_id="s_chat")
    path = ksa._board_db_paths()[0]
    with pytest.raises(sqlite3.OperationalError):  # the update trigger names every read column
        board.execute("ALTER TABLE task_comments DROP COLUMN origin_gateway")
    rev.drop_triggers(board)
    board.execute("ALTER TABLE task_comments DROP COLUMN origin_gateway")
    board.commit()
    assert ksa._board_content_key(path)[0] == "stat"
    rev.ensure_revision(board, rev.LOCAL_TABLES)
    assert "origin_gateway" not in rev.wanted_triggers(board, ["task_comments"])["kanban_activity_rev_task_comments_update"]
    before = ksa._board_content_key(path)
    assert rev.is_revision(before)
    with kbc.write_txn(board):
        board.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, 'a', 'x', 1)",
                      (tid,))
    assert ksa._board_content_key(path) != before


def test_hub_bookkeeping_does_not_move_the_fingerprint(board, home):
    hub_dir = home.parent / "hub-state"
    hub_dir.mkdir()
    hub = sqlite3.connect(hub_dir / "hub.db")
    hub.executescript("CREATE TABLE replica (coll TEXT, gw TEXT, key TEXT, data TEXT, seq INTEGER, version INTEGER, "
                      "updated_at INTEGER); CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);"
                      "INSERT INTO meta VALUES ('gateway', 'macbook'), ('last_ok', '1');")
    hub.commit()
    before = ksa._activity_fingerprint()
    hub.execute("UPDATE meta SET v = '2' WHERE k = 'last_ok'")
    hub.commit()
    assert ksa._activity_fingerprint() == before
    hub.execute("INSERT INTO replica VALUES ('tasks', 'hub', 'default/t_x', '{}', 1, 1, 1)")
    hub.commit()
    assert ksa._activity_fingerprint() != before
    hub.close()


def _shadow(home):
    path = home / "kanban" / sync.SHADOW_DIR / "default.db"
    conn = kbc.connect(path)
    return path, conn


def test_shadow_key_ignores_pull_stamps_and_unread_tables(home):
    path, conn = _shadow(home)
    try:
        assert rev.tables_for(path) == rev.SHADOW_TABLES
        before = ksa._board_content_key(path, rev.SHADOW_TABLES)
        assert rev.is_revision(before)
        sync.mark_pulled(conn)
        with kbc.write_txn(conn):  # a replicated heartbeat: read uncached by read_runs, never through this key
            conn.execute("INSERT INTO task_events (task_id, kind, created_at) VALUES ('t_hub', 'heartbeat', 1)")
        assert ksa._board_content_key(path, rev.SHADOW_TABLES) == before
        with kbc.write_txn(conn):
            conn.execute("INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, "
                         "released, created_at) VALUES ('u1', 't_hub', ?, 'default', 's1', 'shepherd', 0, 1)", (ME,))
        assert ksa._board_content_key(path, rev.SHADOW_TABLES) != before
    finally:
        conn.close()


def test_replica_rows_are_reread_only_when_their_content_moves(home, monkeypatch):
    path, conn = _shadow(home)
    try:
        with kbc.write_txn(conn):
            conn.execute("INSERT INTO tasks (id, title, status, created_at, created_on) "
                         "VALUES ('t_hub', 'hub card', 'running', 1, ?)", (OTHER,))
            conn.execute("INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, "
                         "released, created_at) VALUES ('u1', 't_hub', ?, 'default', 's1', 'shepherd', 0, 1)", (ME,))
        queries = []
        real = links._read_replica_links
        monkeypatch.setattr(links, "_read_replica_links", lambda *a: queries.append(1) or real(*a))
        first = links.replica_cards(["s1"], ME, replicas=[("default", path)])
        assert [c["id"] for c in first] == ["t_hub"] and len(queries) == 1
        sync.mark_pulled(conn, at=12345.0)
        again = links.replica_cards(["s1"], ME, replicas=[("default", path)])
        assert len(queries) == 1 and again[0]["synced_at"] == 12345.0  # the stamp is still read every time
        with kbc.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = 't_hub'")
        assert links.replica_cards(["s1"], ME, replicas=[("default", path)])[0]["status"] == links.DONE_PENDING_RELEASE
        assert len(queries) == 2
    finally:
        conn.close()


def test_ledger_owners_rebuild_only_for_a_new_projection(monkeypatch):
    links._reset_for_tests()
    cards = [{"gateway": OTHER, "board": "default", "id": f"t_{i}", "profile": "default", "session": "s1"}
             for i in range(3)]
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: list(cards))  # a fresh list, same cards
    first = links._ledger_owners()
    assert links._ledger_owners() is first
    cards[1] = {**cards[1], "profile": "work"}  # a re-projection hands out new card dicts
    assert links._ledger_owners()[("default", "t_1")] == [(OTHER, "work", "s1", False)]
    links._reset_for_tests()


def test_an_unchanged_annotation_reuses_its_frozen_parts(board, monkeypatch):
    tid = kb.create_task(board, title="work", assignee="worker", session_id="s_chat")
    freezes = []
    real = ksa._identity.freeze_annotation
    monkeypatch.setattr(ksa._identity, "freeze_annotation", lambda parts: freezes.append(1) or real(parts))
    first = ksa._read_annotation_parts()
    _bookkeeping(board)
    _expire_counts()  # a real recompute, with an equal answer
    assert ksa._read_annotation_parts() is first and len(freezes) == 1
    kb.complete_task(board, tid, result="ok")
    changed = ksa._read_annotation_parts()
    assert changed is not first and changed[0] == {} and len(freezes) == 2


def test_an_unchanged_schema_checks_trigger_coverage_once(board, monkeypatch):
    """[daveey] Every Desktop poll keys each board; re-scanning ``sqlite_master`` for the triggers each time was most
    of the fingerprint's cost on David-MBP. The coverage verdict is kept per file and ``schema_version`` (a dropped
    or added trigger moves it), so later keys read only the counter row."""
    kb.create_task(board, title="work", assignee="worker", session_id="s_chat")
    path = ksa._board_db_paths()[0]
    scans = []
    real = rev.read_key
    monkeypatch.setattr(rev, "read_key", lambda conn, tables: scans.append(1) or real(conn, tables))
    first = ksa._board_content_key(path)
    assert [ksa._board_content_key(path) for _ in range(4)] == [first] * 4
    assert len(scans) == 1
    kb.create_task(board, title="more", assignee="worker", session_id="s_chat")
    moved = ksa._board_content_key(path)
    assert rev.is_revision(moved) and moved != first and len(scans) == 1
    board.execute("DROP TRIGGER kanban_activity_rev_tasks_update")
    board.commit()
    assert ksa._board_content_key(path)[0] == "stat" and len(scans) == 2
