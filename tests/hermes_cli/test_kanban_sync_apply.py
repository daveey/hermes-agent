"""``apply_remote_changes`` (core patch 0193): one transaction per feed page,
echo suppression, run uid translation, hooks once after commit, cursor atomic
with the rows, dispatcher wake. Design: hermes-mods ``core/kanban-central-service.md``."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli.plugins import get_plugin_manager
from tests.hermes_cli import kanban_sync_support as support


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def synced_conn(kanban_home):
    yield from support.synced_conn(kanban_home)

OTHER = "gw_other"


def _ch(seq, table, uid, fields, version=1, op="upsert", client_op_id=None):
    return sync.RemoteChange(seq, table, uid, op, fields, version, f"{seq:013d}-0000-{OTHER}", OTHER, client_op_id)


def _task_fields(**over):
    return {"title": "remote card", "status": "ready", "created_at": 1_700_000_000, "workspace_kind": "scratch",
            "assignee": "worker", **over}


def _outbox_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM kanban_sync_outbox").fetchone()[0]


@pytest.fixture
def captured_hooks():
    mgr = get_plugin_manager()
    saved = {k: list(v) for k, v in mgr._hooks.items()}
    events: list[tuple[str, dict]] = []
    for hook in ("kanban_task_claimed", "kanban_task_completed", "kanban_task_blocked", "on_kanban_task_updated"):
        mgr._hooks.setdefault(hook, []).append(lambda _h=hook, **kw: events.append((_h, kw)))
    try:
        yield events
    finally:
        mgr._hooks = saved


def test_remote_changes_are_not_echoed_into_the_outbox(synced_conn):
    conn = synced_conn
    before = _outbox_count(conn)
    sync.apply_remote_changes(conn, [_ch(1, "tasks", "t_remote000000001", _task_fields())], new_cursor=1)
    assert kb.get_task(conn, "t_remote000000001").title == "remote card"
    assert _outbox_count(conn) == before
    assert conn.execute("SELECT applying FROM kanban_sync_state").fetchone()[0] == 0


def test_own_op_coming_back_only_clears_its_outbox_row(synced_conn):
    conn = synced_conn
    tid = kb.create_task(conn, title="mine")
    [op] = [o for o in sync.outbox(conn) if o["table_name"] == "tasks"]
    echo = _ch(1, "tasks", tid, {**op["fields"], "title": "server copy"}, version=1, client_op_id=op["op_id"])
    result = sync.apply_remote_changes(conn, [echo], new_cursor=1)
    assert (result.skipped_echo, result.applied) == (1, 0)
    assert op["op_id"] not in {o["op_id"] for o in sync.outbox(conn)}
    assert kb.get_task(conn, tid).title == "mine"
    assert conn.execute("SELECT sync_version FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == 1


def test_run_uid_translates_to_local_run_ids_in_any_order(synced_conn):
    conn = synced_conn
    tid = "t_remote000000002"
    page = [
        _ch(1, "tasks", tid, _task_fields(status="running")),
        # event first: its run uid is unknown yet and gets a placeholder run row
        _ch(2, "task_events", "ev-1", {"task_id": tid, "run_uid": "run-A", "kind": "claimed", "payload": "{}",
                                       "created_at": 5}),
        _ch(3, "task_runs", "run-A", {"task_id": tid, "status": "running", "profile": "worker", "started_at": 5}),
        _ch(4, "tasks", tid, {"current_run_uid": "run-A"}, version=2),
    ]
    sync.apply_remote_changes(conn, page, new_cursor=4)
    run_id = conn.execute("SELECT id FROM task_runs WHERE uid = 'run-A'").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE uid = 'run-A'").fetchone()[0] == 1
    assert conn.execute("SELECT run_id FROM task_events WHERE uid = 'ev-1'").fetchone()[0] == run_id
    assert kb.get_task(conn, tid).current_run_id == run_id
    assert conn.execute("SELECT status, profile FROM task_runs WHERE id = ?", (run_id,)).fetchone()[:] == (
        "running", "worker")


def test_local_only_columns_are_never_written_from_the_wire(synced_conn):
    conn = synced_conn
    tid = kb.create_task(conn, title="local", assignee="worker")
    kb.claim_task(conn, tid)
    lock, path = conn.execute("SELECT claim_lock, workspace_path FROM tasks WHERE id = ?", (tid,)).fetchone()
    sync.apply_remote_changes(conn, [_ch(1, "tasks", tid, {
        "title": "renamed", "claim_lock": "evil:1", "workspace_path": "/elsewhere", "worker_pid": 1,
        "not_a_column": "ignored",
    })], new_cursor=1)
    row = conn.execute("SELECT title, claim_lock, workspace_path, worker_pid FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert tuple(row) == ("renamed", lock, path, None)


def test_hooks_fire_once_after_commit_with_remote_kwargs(synced_conn, captured_hooks):
    conn = synced_conn
    tid = "t_remote000000003"
    sync.apply_remote_changes(conn, [_ch(1, "tasks", tid, _task_fields())], new_cursor=1)
    captured_hooks.clear()
    seen_status = []
    mgr = get_plugin_manager()

    def _reads_committed_state(**_kw):
        # A separate connection sees the completion: the hook runs after COMMIT.
        with kbc.connect_closing(Path(conn.execute("PRAGMA database_list").fetchone()[2])) as other:
            seen_status.append(other.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0])

    mgr._hooks["kanban_task_completed"].append(_reads_committed_state)
    page = [_ch(2, "tasks", tid, {"status": "done", "result": "shipped", "completed_at": 9}, version=2)]
    sync.apply_remote_changes(conn, page, new_cursor=2)
    sync.apply_remote_changes(conn, page, new_cursor=2)  # replayed page: idempotent, no second hook
    completed = [kw for name, kw in captured_hooks if name == "kanban_task_completed"]
    assert len(completed) == 1
    assert completed[0]["task_id"] == tid and completed[0]["remote"] is True
    assert completed[0]["origin_gateway"] == OTHER
    assert seen_status == ["done"]
    updated = [kw for name, kw in captured_hooks if name == "on_kanban_task_updated"]
    assert len(updated) == 1 and set(updated[0]["changed_fields"]) == {"result", "completed_at"}


def test_cursor_advances_atomically_with_the_rows(synced_conn):
    conn = synced_conn
    bad_page = [
        _ch(1, "tasks", "t_remote000000004", _task_fields()),
        _ch(2, "no_such_table", "x", {}),
    ]
    with pytest.raises(ValueError):
        sync.apply_remote_changes(conn, bad_page, new_cursor=2)
    assert kb.get_task(conn, "t_remote000000004") is None
    assert tuple(conn.execute("SELECT cursor, applying FROM kanban_sync_state").fetchone()) == (0, 0)
    sync.apply_remote_changes(conn, bad_page[:1], new_cursor=1)
    assert conn.execute("SELECT cursor FROM kanban_sync_state").fetchone()[0] == 1
    assert kb.get_task(conn, "t_remote000000004") is not None


def test_remote_promotion_wakes_the_dispatcher_once(synced_conn):
    conn = synced_conn
    tid = "t_remote000000005"
    sync.apply_remote_changes(conn, [_ch(1, "tasks", tid, _task_fields(status="todo"))], new_cursor=1)
    assert sync.consume_dispatcher_wake() is False
    sync.apply_remote_changes(conn, [_ch(2, "tasks", tid, {"status": "ready"}, version=2)], new_cursor=2)
    assert sync.consume_dispatcher_wake() is True
    assert sync.consume_dispatcher_wake() is False


def test_remote_delete_tombstones_and_removes_relations(synced_conn):
    conn = synced_conn
    tid = kb.create_task(conn, title="will vanish")
    kb.add_comment(conn, tid, "me", "x")
    before = _outbox_count(conn)
    sync.apply_remote_changes(conn, [_ch(1, "tasks", tid, {}, version=3, op="delete")], new_cursor=1)
    assert kb.get_task(conn, tid) is None
    assert conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (tid,)).fetchone()[0] == 0
    assert conn.execute("SELECT 1 FROM kanban_sync_tombstones WHERE row_uid = ?", (tid,)).fetchone()
    assert _outbox_count(conn) == before


def test_apply_refuses_an_unsynced_board(kanban_home):
    with kbc.connect_closing(kanban_home / "plain.db") as conn:
        with pytest.raises(RuntimeError):
            sync.apply_remote_changes(conn, [], new_cursor=1)
