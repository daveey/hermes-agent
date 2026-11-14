"""Narrow key, own boards and sync shadows: keyed by origin content, never by raw revision. [daveey]

t_01a1db1387f61a98: the key hashed every board's and shadow's content key verbatim, so any commit to the shared
default board or shadow (another gateway's card, a run row, a pulled event) moved all 8 scopes' keys on hermes-hub
and each refreshed every 12 s. Each "keeps" test asserts the board's content key DID move (the old key moved with
it) while the narrow key did not.
"""
import sqlite3
import time

import pytest

from hermes_cli import kanban_activity_revision as revision
from hermes_cli import kanban_annotation_local as own
from hermes_cli import kanban_annotation_narrow as narrow
from hermes_cli import kanban_session_activity as ksa
from hermes_cli.kanban_db_sync import SHADOW_DIR
from tests.hermes_cli.test_kanban_annotation_narrow import SCOPE, hub  # fixture

PROFILE = SCOPE[1]
ME = "me"
REQUEST = '{"action": "Reply A or B. Unblock = build it; archive = drop it.", "why_human": "taste", ' \
          '"interaction": "reply", "scope": "this card", "evidence": "two options"}'


def _sessions(ids):
    from hermes_constants import get_hermes_home

    db = get_hermes_home() / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY)")
        conn.executemany("INSERT OR IGNORE INTO sessions (id) VALUES (?)", [(i,) for i in ids])
    conn.close()


@pytest.fixture
def world(hub, tmp_path, monkeypatch):
    from hermes_cli import kanban_db_connect as kdb

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    board = kdb.init_db(tmp_path / "kanban.db")
    conn = sqlite3.connect(board, isolation_level=None)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert {"session_id", "human_request", "block_kind"} <= cols
    for tid, status, sid in [("t1", "running", "mine1"), ("t2", "todo", "mine1"), ("tp", "running", "other"),
                             ("tx", "running", "other")]:
        conn.execute("INSERT INTO tasks (id, title, status, session_id, created_at) VALUES (?, ?, ?, ?, 1)",
                     (tid, f"title {tid}", status, sid))
    conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES ('tp', 't2')")
    _sessions(["mine1", "mine2"])

    shadow = tmp_path / SHADOW_DIR / "default.db"
    shadow.parent.mkdir(parents=True)
    sconn = sqlite3.connect(shadow, isolation_level=None)
    sconn.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT, created_at INTEGER, "
        "started_at INTEGER, priority INTEGER, block_kind TEXT, human_request TEXT, created_on TEXT, "
        "lease_expires INTEGER);"
        "CREATE TABLE task_links (parent_id TEXT, child_id TEXT);"
        "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, status TEXT);"
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT);"
        "CREATE TABLE task_comments (id INTEGER PRIMARY KEY, task_id TEXT, body TEXT);"
        "CREATE TABLE task_chat_links (uid TEXT, task_id TEXT, chat_gateway TEXT, chat_profile TEXT, "
        "chat_session TEXT, kind TEXT, released INTEGER, created_at INTEGER);"
        "CREATE TABLE kanban_sync_state (board_id TEXT PRIMARY KEY, cursor INTEGER, gateway_id TEXT, "
        "hlc_last TEXT, mode TEXT, applying INTEGER, pending_run_uid TEXT, pulled_at REAL);")
    revision.ensure_revision(sconn, revision.SHADOW_TABLES)
    for tid, status, origin in [("r1", "running", "owner"), ("r2", "running", "owner")]:
        sconn.execute("INSERT INTO tasks VALUES (?, ?, ?, 'w', 1, NULL, 0, NULL, NULL, ?, 100)",
                      (tid, f"title {tid}", status, origin))
    sconn.execute("INSERT INTO task_chat_links VALUES ('u1', 'r1', ?, ?, 'mine1', 'created', 0, 1)", (ME, PROFILE))
    sconn.execute("INSERT INTO kanban_sync_state VALUES ('default', 0, ?, '', 'shadow', 0, NULL, ?)",
                  (ME, time.time()))
    monkeypatch.setattr(own, "_shadow_gateway", lambda: ME)
    narrow.reset()
    yield {"board": board, "conn": conn, "shadow": shadow, "sconn": sconn}
    conn.close()
    sconn.close()
    narrow.reset()


def _full(w):
    return ("hubsig", (str(w["board"]), ksa._board_content_key(w["board"])),
            (str(w["shadow"]), ksa._board_content_key(w["shadow"], revision.SHADOW_TABLES)))


def key(w):
    return narrow.key(SCOPE, _full(w), [])


def _keeps(w, conn, sql, args=()):
    before, content = key(w), _full(w)
    assert before is not None
    conn.execute(sql, args)
    assert _full(w) != content, "the board's raw revision must move (the old key moved with it)"
    assert key(w) == before


def _moves(w, conn, sql, args=()):
    before = key(w)
    assert before is not None
    conn.execute(sql, args)
    after = key(w)
    assert after is not None and after != before


def test_key_is_revision_backed(world):
    full = _full(world)
    assert revision.is_revision(full[1][1]) and revision.is_revision(full[2][1])


# -- own board: liveness and other origins' cards keep the key -----------------------------------------------------

def test_other_origin_card_commit_keeps_key(world):
    _keeps(world, world["conn"], "UPDATE tasks SET status='blocked' WHERE id='tx'")


def test_new_card_of_another_origin_keeps_key(world):
    _keeps(world, world["conn"], "INSERT INTO tasks (id, title, status, session_id, created_at) "
                                 "VALUES ('ty', 'y', 'ready', 'remote-chat', 1)")


def test_run_and_event_rows_keep_key(world):
    _keeps(world, world["conn"], "INSERT INTO task_events (task_id, kind, created_at) VALUES ('t1', 'spawned', 1)")
    _keeps(world, world["conn"], "INSERT INTO task_comments (task_id, author, body, created_at) "
                                 "VALUES ('tx', 'a', 'b', 1)")


def test_own_card_title_edit_keeps_key(world):
    _keeps(world, world["conn"], "UPDATE tasks SET title='renamed' WHERE id='t1'")


# -- own board: status, membership and waits move it --------------------------------------------------------------

def test_own_card_status_moves_key(world):
    _moves(world, world["conn"], "UPDATE tasks SET status='blocked' WHERE id='t1'")


def test_own_card_finishing_moves_key(world):
    _moves(world, world["conn"], "UPDATE tasks SET status='done' WHERE id='t1'")


def test_new_own_card_moves_key(world):
    _moves(world, world["conn"], "INSERT INTO tasks (id, title, status, session_id, created_at) "
                                 "VALUES ('t3', 'z', 'ready', 'mine2', 1)")


def test_card_rehomed_to_this_origin_moves_key(world):
    _moves(world, world["conn"], "UPDATE tasks SET session_id='mine2' WHERE id='tx'")


def test_human_request_moves_key(world):
    _moves(world, world["conn"], "UPDATE tasks SET status='blocked', block_kind='needs_input', human_request=? "
                                 "WHERE id='t1'", (REQUEST,))


def test_shepherd_sub_moves_key(world):
    _moves(world, world["conn"], "INSERT INTO kanban_notify_subs (task_id, platform, chat_id, created_at) "
                                 "VALUES ('tx', 'tui', 'mine2', 1)")


def test_parent_status_moves_key(world):
    _moves(world, world["conn"], "UPDATE tasks SET status='done' WHERE id='tp'")


def test_new_dependency_moves_key(world):
    _moves(world, world["conn"], "INSERT INTO task_links (parent_id, child_id) VALUES ('tx', 't1')")


# -- sync shadow: liveness keeps it, link and linked card changes move it -----------------------------------------

def test_unlinked_shadow_card_keeps_key(world):
    _keeps(world, world["sconn"], "UPDATE tasks SET status='blocked' WHERE id='r2'")


def test_shadow_lease_and_events_keep_key(world):
    before = key(world)
    world["sconn"].execute("UPDATE tasks SET lease_expires=999 WHERE id='r1'")  # bookkeeping: no revision either
    assert key(world) == before
    _keeps(world, world["sconn"], "INSERT INTO task_events (task_id, kind) VALUES ('r1', 'spawned')")
    _keeps(world, world["sconn"], "INSERT INTO task_runs (task_id, status) VALUES ('r1', 'running')")


def test_pull_stamp_keeps_key_until_stale(world, monkeypatch):
    before = key(world)
    world["sconn"].execute("UPDATE kanban_sync_state SET pulled_at = pulled_at + 5")
    assert key(world) == before
    world["sconn"].execute("UPDATE kanban_sync_state SET pulled_at = 0")
    assert key(world) != before  # the replica went stale: a verdict flip


def test_linked_shadow_card_status_moves_key(world):
    _moves(world, world["sconn"], "UPDATE tasks SET status='blocked' WHERE id='r1'")


def test_new_and_released_link_move_key(world):
    _moves(world, world["sconn"], "INSERT INTO task_chat_links VALUES ('u2', 'r2', ?, ?, 'mine1', 'shepherd', 0, 1)",
           (ME, PROFILE))
    _moves(world, world["sconn"], "UPDATE task_chat_links SET released=1 WHERE uid='u1'")


def test_other_profiles_link_keeps_key(world):
    _keeps(world, world["sconn"], "INSERT INTO task_chat_links VALUES ('u3', 'r2', ?, 'someone-else', 'c', "
                                  "'created', 0, 1)", (ME,))


# -- fail closed ---------------------------------------------------------------------------------------------------

def test_unreadable_board_is_unknown(world):
    full = list(_full(world))
    full[1] = (full[1][0], ("stat", ((1, 2, 3, 4), None), object()))
    assert narrow.key(SCOPE, tuple(full), []) is None


def test_failed_board_read_is_unknown_and_not_memoised(world, monkeypatch):
    assert key(world) is not None
    world["conn"].execute("UPDATE tasks SET status='blocked' WHERE id='tx'")
    real = own._board_rows

    def broken(conn):
        raise sqlite3.DatabaseError("database disk image is malformed")
    monkeypatch.setattr(own, "_board_rows", broken)
    assert key(world) is None
    monkeypatch.setattr(own, "_board_rows", real)
    assert key(world) is not None


def test_board_read_once_then_patched_per_revision(world):
    key(world)
    reads, patches = own.stats["board_reads"], own.stats["board_patches"]
    for _ in range(3):
        key(world)
    assert (own.stats["board_reads"], own.stats["board_patches"]) == (reads, patches)
    world["conn"].execute("UPDATE tasks SET status='blocked' WHERE id='tx'")
    key(world)
    assert (own.stats["board_reads"], own.stats["board_patches"]) == (reads, patches + 1)  # changed cards only


@pytest.mark.parametrize("seed", range(4))
def test_patched_rows_equal_a_full_read(world, seed):
    """Incremental board rows (per-task change cursor) always give the key a fresh full read gives."""
    import random

    rng = random.Random(seed)
    conn = world["conn"]
    ids = ["t1", "t2", "tp", "tx", "n1", "n2"]
    ops = [
        ("UPDATE tasks SET status=? WHERE id=?", lambda a, b: (rng.choice(["running", "blocked", "done", "todo"]), a)),
        ("INSERT OR IGNORE INTO tasks (id, title, status, session_id, created_at) VALUES (?, 'n', 'ready', ?, 1)",
         lambda a, b: (a, rng.choice(["mine1", "mine2", "other"]))),
        ("INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)", lambda a, b: (a, b)),
        ("DELETE FROM task_links WHERE parent_id=? OR child_id=?", lambda a, b: (a, a)),
        ("INSERT OR IGNORE INTO kanban_notify_subs (task_id, platform, chat_id, created_at) VALUES (?, 'tui', ?, 1)",
         lambda a, b: (a, rng.choice(["mine2", "other"]))),
        ("DELETE FROM tasks WHERE id=?", lambda a, b: (a,)),
    ]
    key(world)
    for step in range(25):
        a, b = rng.sample(ids, 2)
        op = rng.randrange(len(ops))
        sql, args = ops[op]
        conn.execute(sql, args(a, b))
        incremental = key(world)
        own.reset()
        narrow.reset()
        assert key(world) == incremental, f"step {step} op {op}"
    assert own.stats["board_patches"] > 0
