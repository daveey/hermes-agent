"""Scratch custody gaps: archived-without-completion retention, Board-claim receipt discovery,
default-path eviction receipts and the explicit ``kanban.scratch_gc_preserve`` pin. [daveey]

Real SQLite eviction/GC with an injected Board transport; no live home or Board calls.
"""
import json
import logging
import shutil
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as ws
from hermes_cli import kanban_host_identity
from hermes_cli import kanban_scratch_custody as custody
from hermes_cli import kanban_scratch_custody_discovery as discovery
from hermes_cli import kanban_scratch_preserve as preserve
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_worker_node as worker
from hermes_cli import kanban_worktree_gc as gc
from tests.hermes_cli import test_kanban_scratch_custody as sc

env = sc.env
NOW, OLD = sc.NOW, sc.OLD
DAY = 86400
HOST = "hermes-worker-2"
TID = "t_0123456789abcdef"


@pytest.fixture
def board(env, monkeypatch):
    """Board rows by (table, uid); ``tasks`` keeps the env fixture's owner semantics."""
    env.rows = {}
    env.asked = []

    class Hub:
        def row_evidence(self, board_slug, table, uid):
            assert not env.conn.in_transaction and board_slug == "default"
            env.asked.append((table, uid))
            if table == "tasks":
                env.calls += 1
                return {"table": table, "uid": uid, "head": 1, "state": env.evidence_state,
                        "row": dict(env.owner), **env.evidence_extra}
            row = env.rows.get((table, uid))
            return {"table": table, "uid": uid, "head": 1, "state": "live" if row else "never_existed",
                    "row": dict(row) if row else None}

    monkeypatch.setattr(client, "Hub", lambda identity: Hub())
    monkeypatch.setattr(kanban_host_identity, "hostname_prefix", lambda: HOST + ":")
    return env


def _hub():
    return client.Hub(None)


def _config(env, **kanban):
    lines = ["kanban:", "  sync:", "    enabled: true"]
    lines += [f"  {key}: {json.dumps(value)}" for key, value in kanban.items()]
    (env.home / "config.yaml").write_text("\n".join(lines) + "\n")


# -- 1. Board 'archived' with completed_at null --------------------------------------------------

def _archived(env, *, updated_at=OLD):
    env.owner.update(status="archived", completed_at=None)
    if updated_at is not None:
        env.owner["updated_at"] = updated_at * 1000  # Board row metadata is milliseconds


def test_archived_without_completed_at_is_reclaimed(board, caplog):
    _archived(board)
    _tid, path, _ = sc.evicted(board)
    with caplog.at_level("INFO"):
        assert gc.gc_task_scratch(board.conn, older_than_days=7) == 1
    assert not path.exists() and custody.receipts(board.conn) == []
    assert "verified_terminal=archived" in caplog.text and "terminal_unverified" not in caplog.text


@pytest.mark.parametrize("offset,removed", [(-1, 0), (0, 1)])
def test_archived_clock_uses_latest_ended_run(board, offset, removed):
    _archived(board)  # archived (updated) long ago, but the last run ended later
    _tid, path, _ = sc.evicted(board, ended=NOW - DAY)
    board.now = NOW - DAY + 7 * DAY + offset
    assert gc.gc_task_scratch(board.conn, older_than_days=7) == removed
    assert path.exists() is (not removed)


@pytest.mark.parametrize("offset,removed", [(-1, 0), (0, 1)])
def test_archived_clock_uses_board_updated_at(board, offset, removed):
    _archived(board, updated_at=NOW - DAY)  # runs ended long ago, archived (updated) later
    _tid, path, _ = sc.evicted(board)
    board.now = NOW - DAY + 7 * DAY + offset
    assert gc.gc_task_scratch(board.conn, older_than_days=7) == removed
    assert path.exists() is (not removed)


@pytest.mark.parametrize("fields", [
    {"lease_holder": "other", "lease_expires": NOW * 1000 + 60000},  # lease still held
    {"completed_at": 0},                                                # present but not a clock
    {"completed_at": True},
    {"status": "done"},                                                 # done still needs completed_at
    {"status": "running"},
])
def test_archived_controls_keep_tree(board, fields):
    _archived(board)
    _tid, path, _ = sc.evicted(board)
    board.owner.update(fields)
    assert gc.gc_task_scratch(board.conn, older_than_days=7) == 0
    assert path.exists()


@pytest.mark.parametrize("control", ["uid", "deleted"])
def test_archived_row_must_be_live_and_match(board, control):
    _archived(board)
    _tid, path, _ = sc.evicted(board)
    if control == "uid":
        board.owner["uid"] = "t_ffffffffffffffff"
    else:
        board.evidence_state = "deleted"
    assert gc.gc_task_scratch(board.conn, older_than_days=7) == 0
    assert path.exists()


def test_archived_clock_sources(board):
    tid, _path, _ = sc.evicted(board)
    _archived(board, updated_at=None)
    assert custody._terminal(_hub(), "default", tid) is None  # no clock at all
    assert custody._terminal(_hub(), "default", tid, runs=[{"id": 1, "ended_at": None}]) is None
    assert custody._terminal(_hub(), "default", tid, runs=[{"id": 1, "ended_at": OLD}])["retention_at"] == OLD
    _archived(board, updated_at=NOW - DAY)
    owner = custody._terminal(_hub(), "default", tid, runs=[{"id": 1, "ended_at": OLD}])
    assert owner["retention_at"] == NOW - DAY and owner["completed_at"] is None


def test_done_retention_clock_is_completed_at(board):
    tid, _path, _ = sc.evicted(board)
    board.owner["updated_at"] = NOW * 1000  # a later edit never moves a completed card's clock
    assert custody._terminal(_hub(), "default", tid, runs=[{"id": 1, "ended_at": NOW}])["retention_at"] == OLD


def test_pressure_terminal_owner_accepts_archived_with_run_clock(board):
    tid, _path, _ = sc.evicted(board)
    _archived(board, updated_at=None)
    owner = custody.terminal_owner(_hub(), "default", dict(sc._receipt(board, tid)))
    assert owner is not None and owner["retention_at"] == OLD


# -- 2. Board-claim receipt discovery ------------------------------------------------------------

def _orphan(name=TID):
    path = kb.workspaces_root() / name
    path.mkdir(parents=True)
    (path / "notes").write_text("keep me")
    return path


def _replica(events):
    shadow = client.shadow_path("default")
    shadow.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(shadow)
    with conn:
        conn.execute("CREATE TABLE IF NOT EXISTS task_events "
                     "(id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, uid TEXT)")
        conn.executemany("INSERT INTO task_events(task_id, kind, uid) VALUES (?,?,?)", events)
    conn.close()


def _claimed(env, tid=TID, *, n=1, lock=HOST + ":4242", ended=OLD, kind="scratch", run_task=None):
    """Board task row + ``n`` claims by ``lock`` (each its own run) + the replica listing them."""
    env.owner.update(uid=tid, workspace_kind=kind)
    events = []
    for i in range(n):
        ev, run = f"ev-{tid}-{i}", f"run-{tid}-{i}"
        env.rows[("task_events", ev)] = {"uid": ev, "task_id": tid, "kind": "claimed", "run_uid": run,
                                         "payload": json.dumps({"lock": lock, "run_id": 7 + i})}
        env.rows[("task_runs", run)] = {"uid": run, "task_id": run_task or tid, "ended_at": ended}
        events.append((tid, "claimed", ev))
    _replica(events)


def _receipt(env, tid=TID):
    """The custody receipt row for tid, or None (also when no receipt table exists yet)."""
    return next((r for r in custody.receipts(env.conn) if r["task_id"] == tid), None)


def _evidence(env, tid=TID):
    row = _receipt(env, tid)
    return None if row is None else json.loads(row["evidence"])


def test_board_claim_discovery_writes_receipt_then_gc_reclaims(board, caplog):
    path = _orphan()
    _claimed(board)
    with caplog.at_level("INFO"):
        assert gc.gc_task_scratch(board.conn, older_than_days=7) == 1
    assert not path.exists()
    assert f"Scratch custody discovered: task={TID}" in caplog.text
    assert "verified_terminal=done" in caplog.text
    assert not board.conn.execute("SELECT 1 FROM kanban_sync_outbox").fetchone()
    assert not board.conn.execute("SELECT 1 FROM task_events WHERE task_id=?", (TID,)).fetchone()


def test_discovery_never_deletes(board):
    path = _orphan()
    _claimed(board)
    assert discovery.discover(board.conn, "default", _hub(), [path]) == 1
    assert (path / "notes").read_text() == "keep me"
    assert _evidence(board) == {"kind": "board", "board": "default",
                                "runs": [{"id": f"run-{TID}-0", "ended_at": OLD}]}
    assert _receipt(board, TID)["workspace_path"] == str(path)


def test_discovered_receipt_waits_for_retention(board):
    path = _orphan()
    _claimed(board)
    board.owner["completed_at"] = NOW  # done just now: discovery records, retention keeps
    assert gc.gc_task_scratch(board.conn, older_than_days=7) == 0
    assert path.exists() and _evidence(board)["kind"] == "board"


def _event(env):
    return env.rows[("task_events", f"ev-{TID}-0")]


_BREAK = {
    "no_board_row": lambda env: setattr(env, "evidence_state", "never_existed"),
    "event_other_task": lambda env: _event(env).update(task_id="t_ffffffffffffffff"),
    "event_not_claimed": lambda env: _event(env).update(kind="blocked"),
    "event_missing": lambda env: env.rows.pop(("task_events", f"ev-{TID}-0")),
    "bad_payload": lambda env: _event(env).update(payload="not json"),
    "one_of_ours_live": lambda env: env.rows[("task_runs", f"run-{TID}-1")].update(ended_at=None),
}


@pytest.mark.parametrize("control", [
    "dir_kind", "no_board_row", "foreign_lock", "run_live", "run_other_task", "event_other_task",
    "event_not_claimed", "event_missing", "no_replica", "bad_payload", "one_of_ours_live", "substring_host",
])
def test_discovery_board_controls_keep_owner_unverified(board, caplog, control):
    path = _orphan()
    if control == "no_replica":
        board.owner.update(uid=TID, workspace_kind="scratch")
    else:
        lock = {"foreign_lock": "hermes-worker-1:4242", "substring_host": HOST + "0:4242"}.get(control, HOST + ":4242")
        _claimed(board, kind="dir" if control == "dir_kind" else "scratch", lock=lock,
                 ended=None if control == "run_live" else OLD,
                 run_task="t_ffffffffffffffff" if control == "run_other_task" else None,
                 n=2 if control == "one_of_ours_live" else 1)
    _BREAK.get(control, lambda env: None)(board)
    with caplog.at_level("WARNING"):
        assert gc.gc_task_scratch(board.conn, older_than_days=7) == 0
    assert (path / "notes").read_text() == "keep me"
    assert _receipt(board, TID) is None
    assert f"reason=owner_unverified path={path}" in caplog.text


def test_foreign_live_claim_does_not_block_our_ended_claim(board):
    path = _orphan()
    _claimed(board)
    board.rows[("task_events", "ev-foreign")] = {"uid": "ev-foreign", "task_id": TID, "kind": "claimed",
                                                 "run_uid": "run-foreign",
                                                 "payload": json.dumps({"lock": "hermes-worker-1:9"})}
    board.rows[("task_runs", "run-foreign")] = {"uid": "run-foreign", "task_id": TID, "ended_at": None}
    _replica([(TID, "claimed", "ev-foreign")])
    assert discovery.discover(board.conn, "default", _hub(), [path]) == 1
    assert [r["id"] for r in _evidence(board)["runs"]] == [f"run-{TID}-0"]


@pytest.mark.parametrize("name", ["t_0123456789abcde", "t_0123456789abcdef0", "t_0123456789ABCDEF",
                                  "x_0123456789abcdef", "notes"])
def test_discovery_requires_exact_card_name(board, name):
    _orphan(name)
    assert discovery.candidates(board.conn, "default") == []


def test_discovery_candidates_skip_symlink_rows_receipts_and_pins(board, tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    link = kb.workspaces_root() / "t_1111111111111111"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(real, target_is_directory=True)
    shared = _orphan("t_2222222222222222")
    sharer = kb.create_task(board.conn, title="dir sharer")
    with kb.write_txn(board.conn):
        board.conn.execute("UPDATE tasks SET workspace_kind='dir', workspace_path=? WHERE id=?",
                           (str(shared / "sub"), sharer))
    pinned = _orphan("t_3333333333333333")
    _config(board, scratch_gc_preserve=[pinned.name])
    plain = _orphan("t_4444444444444444")
    _tid, receipted, _ = sc.evicted(board)  # already holds an eviction receipt
    found = discovery.candidates(board.conn, "default")
    assert plain in found and receipted not in found
    assert not {link, shared, pinned} & set(found)


def test_discovery_rechecks_local_row_inside_write_txn(board, monkeypatch):
    path = _orphan()
    _claimed(board)
    real = discovery.board_runs

    def pickup_during_board_read(hub, board_slug, tid):
        runs = real(hub, board_slug, tid)
        with kb.write_txn(board.conn):
            board.conn.execute("INSERT INTO tasks(id, title, status, created_at, workspace_kind) "
                               "VALUES (?, 'picked up', 'running', 1, 'scratch')", (tid,))
        assert board.conn.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone()  # race really committed
        return runs

    monkeypatch.setattr(discovery, "board_runs", pickup_during_board_read)
    assert discovery.discover(board.conn, "default", _hub(), [path]) == 0
    assert _receipt(board, TID) is None and path.exists()


def test_board_error_keeps_dir_and_existing_receipts_still_run(board, caplog, monkeypatch):
    path = _orphan()
    _claimed(board)
    board.owner["uid"] = ""  # the eviction below rebinds the owner row to its own card
    evicted_tid, evicted_path, _ = sc.evicted(board)

    def unavailable(hub, board_slug, tid):
        raise client.HubError(503, "board down")

    monkeypatch.setattr(discovery, "board_runs", unavailable)
    with caplog.at_level("WARNING"):
        assert gc.gc_task_scratch(board.conn, older_than_days=7) == 1
    assert path.exists() and _receipt(board, TID) is None
    assert not evicted_path.exists() and sc._receipt(board, evicted_tid) is None
    assert "Scratch custody discovery stopped" in caplog.text


def test_discovery_refuses_inside_caller_transaction(board):
    path = _orphan()
    _claimed(board)
    with kb.write_txn(board.conn):
        assert discovery.discover(board.conn, "default", _hub(), [path]) == 0
    assert board.asked == []


# -- 3. retain() records the default path for NULL workspace_path --------------------------------

def test_null_workspace_path_eviction_records_default_tree(board):
    tid, path, run = sc.evicted(board, path_mode="missing")
    receipt = sc._receipt(board, tid)
    assert receipt is not None and receipt["workspace_path"] == str(kb.workspaces_root() / tid)
    assert json.loads(receipt["evidence"]) == {"kind": "eviction", "runs": [{"id": run, "ended_at": OLD}]}
    assert gc.gc_task_scratch(board.conn, older_than_days=7) == 1
    assert not path.exists()


@pytest.mark.parametrize("shape", ["absent", "symlink"])
def test_null_workspace_path_without_plain_default_tree_records_nothing(board, tmp_path, shape):
    tid, path, _ = sc.task(board, path_mode="missing")
    shutil.rmtree(path)
    if shape == "symlink":
        (tmp_path / "real").mkdir()
        path.symlink_to(tmp_path / "real", target_is_directory=True)
    assert worker.evict(board.conn) == [tid]
    assert custody.receipts(board.conn) == []


# -- 4. kanban.scratch_gc_preserve ----------------------------------------------------------------

def test_preserved_custody_receipt_is_kept_without_board_call(board, caplog):
    tid, path, _ = sc.evicted(board)
    _config(board, scratch_gc_preserve=[tid])
    with caplog.at_level("INFO"):
        assert gc.gc_task_scratch(board.conn, older_than_days=7) == 0
    assert path.exists() and sc._receipt(board, tid) is not None
    assert board.asked == [] and "reason=scratch_gc_preserve" in caplog.text


def _local_done(env, *, completed_at=OLD):
    tid = kb.create_task(env.conn, title="local done")
    path = kb.workspaces_root() / tid
    path.mkdir(parents=True)
    (path / "notes").write_text("keep me")
    with kb.write_txn(env.conn):
        env.conn.execute("UPDATE tasks SET status='done', workspace_kind='scratch', workspace_path=?, "
                         "completed_at=? WHERE id=?", (str(path), completed_at, tid))
    return tid, path


def test_preserved_local_terminal_tree_is_kept_by_retention_gc(board):
    tid, path = _local_done(board)
    _config(board, scratch_gc_preserve=[tid])
    assert gc.gc_task_scratch(board.conn, older_than_days=7) == 0
    assert (path / "notes").read_text() == "keep me"
    _config(board)
    assert gc.gc_task_scratch(board.conn, older_than_days=7) == 1  # control: unpinned goes
    assert not path.exists()


def test_preserved_tree_survives_completion_cleanup(board):
    tid, path = _local_done(board)
    _config(board, scratch_gc_preserve=[tid])
    ws._cleanup_workspace(board.conn, tid)
    assert (path / "notes").read_text() == "keep me"
    _config(board)
    ws._cleanup_workspace(board.conn, tid)  # control: the same call removes an unpinned tree
    assert not path.exists()


def test_owned_job_veto_honours_pin_for_external_removers(board, tmp_path):
    tree = tmp_path / "t_aaaaaaaaaaaaaaaa"
    tree.mkdir()
    (tree / "nested").mkdir()
    other = tmp_path / "t_bbbbbbbbbbbbbbbb"
    other.mkdir()
    assert gc.defer_owned_job_cleanup("t_bbbbbbbbbbbbbbbb", other) is False  # control
    _config(board, scratch_gc_preserve=["t_aaaaaaaaaaaaaaaa"])
    assert gc.defer_owned_job_cleanup("t_aaaaaaaaaaaaaaaa", tree) is True
    assert gc.defer_owned_job_cleanup("t_bbbbbbbbbbbbbbbb", tree / "nested") is True  # path component
    assert gc.defer_owned_job_cleanup("t_bbbbbbbbbbbbbbbb", other) is False


def test_preserved_unowned_dir_is_not_reported_owner_unverified(board, caplog):
    path = _orphan()
    _config(board, scratch_gc_preserve=[TID])
    with caplog.at_level("INFO"):
        assert gc.gc_task_scratch(board.conn, older_than_days=7) == 0
    assert path.exists() and "owner_unverified" not in caplog.text
    assert f"reason=scratch_gc_preserve path={path}" in caplog.text


@pytest.mark.parametrize("value,expected", [
    (None, frozenset()),
    ([], frozenset()),
    (["t_aaaaaaaaaaaaaaaa", " t_b "], frozenset({"t_aaaaaaaaaaaaaaaa", "t_b"})),
    ("t_aaaaaaaaaaaaaaaa", None),       # a bare string is not a list
    ([1], None),
    ([""], None),
    (["a/b"], None),
    ({"t_a": True}, None),
])
def test_preserve_setting_parse_fails_closed(board, caplog, value, expected):
    _config(board, scratch_gc_preserve=value)
    with caplog.at_level(logging.WARNING):
        assert preserve.preserved_ids() == expected
    assert preserve.preserved("t_cccccccccccccccc", "/x/t_cccccccccccccccc") is (expected is None)


def test_unreadable_config_fails_closed(board, monkeypatch):
    from hermes_cli import config

    def boom():
        raise RuntimeError("unreadable")

    monkeypatch.setattr(config, "load_config_readonly", boom)
    assert preserve.preserved_ids() is None
    assert preserve.preserved("t_cccccccccccccccc") is True


def test_preserve_matches_id_component_and_detached_names(monkeypatch):
    monkeypatch.setattr(preserve, "preserved_ids", lambda: frozenset({"t_aaaaaaaaaaaaaaaa"}))
    assert preserve.preserved("t_aaaaaaaaaaaaaaaa")
    assert preserve.preserved(None, "/w/t_aaaaaaaaaaaaaaaa/sub")
    assert preserve.preserved(None, "/w/.t_aaaaaaaaaaaaaaaa.gc-rm-0123")
    assert preserve.preserved(None, "/w/.t_aaaaaaaaaaaaaaaa.gc-77")
    assert not preserve.preserved("t_bbbbbbbbbbbbbbbb", "/w/t_bbbbbbbbbbbbbbbb")
    assert not preserve.preserved(None, "/w/xt_aaaaaaaaaaaaaaaa")
    assert not preserve.preserved(None, None)
