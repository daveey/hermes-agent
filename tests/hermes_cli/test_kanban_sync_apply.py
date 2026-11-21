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


@pytest.mark.parametrize("stamp", ["source", ""])
def test_alarm_source_is_pulled_but_never_pushed(synced_conn, stamp):
    conn = synced_conn
    tid = "t_remote000000099"
    sync.apply_remote_changes(conn, [_ch(1, "tasks", tid, _task_fields(alarm_source=stamp))], new_cursor=1)
    row = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    assert row.get("alarm_source") == stamp
    before = _outbox_count(conn)
    conn.execute("UPDATE tasks SET alarm_source=? WHERE id=?", ("local-forgery", tid))
    assert _outbox_count(conn) == before
    conn.execute("UPDATE tasks SET title='changed' WHERE id=?", (tid,))
    assert all("alarm_source" not in op["fields"] for op in sync.outbox(conn))
    assert "alarm_source" not in sync.captured_columns(conn, "tasks")


def test_alarm_source_reaches_local_prefilter(synced_conn, monkeypatch):
    from hermes_cli import kanban_worker_node as worker
    from hermes_cli import kanban_worker_placement as placement

    conn = synced_conn
    tid = "t_remote000000098"
    sync.apply_remote_changes(conn, [_ch(1, "tasks", tid, _task_fields(
        created_by="pickup-stall-detector", idempotency_key="pickup-stall:source:queue-full:1",
        created_on="new-owner", alarm_source="source"))], new_cursor=1)
    monkeypatch.setattr(sync, "board_mode", lambda conn: "primary")
    monkeypatch.setattr(worker, "_own", lambda conn: "executor")
    monkeypatch.setattr(worker, "registry", lambda: None)
    monkeypatch.setattr(placement, "local_refusals", lambda conn, status: {})
    assert tid not in worker.local_refusals(conn, "ready")
    monkeypatch.setattr(worker, "_own", lambda conn: "source")
    assert worker.local_refusals(conn, "ready")[tid] == "pickup_stall_source"


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


# --- seconds columns (2026-10-27: the Board wrote task_runs.started_at in ms; readers crashed on "year 58792") ---

def test_millisecond_timestamps_from_the_wire_are_stored_in_seconds(synced_conn):
    """A run claimed through a Board lease: the server's copy of the run row carries ms. A whole-row push of the
    damaged local row would then send ms back, so the apply path must normalize every seconds column."""
    conn = synced_conn
    tid = kb.create_task(conn, title="claimed via lease", assignee="worker")
    kb.claim_task(conn, tid)
    run_uid = conn.execute("SELECT r.uid FROM tasks t JOIN task_runs r ON r.id = t.current_run_id WHERE t.id = ?",
                           (tid,)).fetchone()[0]
    started_s = conn.execute("SELECT started_at FROM task_runs WHERE uid = ?", (run_uid,)).fetchone()[0]
    # The claim's own rows have been pushed (a still-pending local edit of a run keeps the local row).
    conn.execute("DELETE FROM kanban_sync_outbox")
    ms = started_s * 1000 + 955
    page = [
        _ch(1, "task_runs", run_uid, {"task_id": tid, "status": "running", "profile": "worker", "started_at": ms,
                                      "ended_at": None}),
        _ch(2, "task_events", "ev-ms", {"task_id": tid, "run_uid": run_uid, "kind": "claimed",
                                        "payload": '{"holder": "macbook"}', "created_at": ms}),
        _ch(3, "task_comments", "c-ms", {"task_id": tid, "author": "a", "body": "b", "created_at": ms}),
        _ch(4, "tasks", "t_remote_ms000001", _task_fields(created_at=ms, started_at=ms, completed_at=ms)),
    ]
    sync.apply_remote_changes(conn, page, new_cursor=4)
    assert conn.execute("SELECT started_at FROM task_runs WHERE uid = ?", (run_uid,)).fetchone()[0] == started_s
    assert conn.execute("SELECT created_at FROM task_events WHERE uid = 'ev-ms'").fetchone()[0] == started_s
    assert conn.execute("SELECT created_at FROM task_comments WHERE uid = 'c-ms'").fetchone()[0] == started_s
    assert tuple(conn.execute("SELECT created_at, started_at, completed_at FROM tasks WHERE id = 't_remote_ms000001'")
                 .fetchone()) == (started_s, started_s, started_s)
    # Seconds and nulls pass through untouched.
    sync.apply_remote_changes(conn, [_ch(5, "task_runs", run_uid, {"task_id": tid, "ended_at": started_s + 7},
                                                   version=2)],
                              new_cursor=5)
    assert conn.execute("SELECT ended_at FROM task_runs WHERE uid = ?", (run_uid,)).fetchone()[0] == started_s + 7
    assert sync.to_seconds(None) is None and sync.to_seconds(True) is True and sync.to_seconds("x") == "x"


def test_rows_already_in_milliseconds_are_repaired_once_on_open(kanban_home):
    support.write_sync_config(kanban_home, True)
    path = kanban_home / "kanban.db"
    conn = kbc.connect(path)
    try:
        tid = kb.create_task(conn, title="damaged", assignee="worker")
        kb.claim_task(conn, tid)
        run_id, started_s = conn.execute("SELECT id, started_at FROM task_runs WHERE task_id = ?", (tid,)).fetchone()
        conn.execute("UPDATE kanban_sync_state SET applying = 1")
        conn.execute("UPDATE task_runs SET started_at = ? WHERE id = ?", (started_s * 1000 + 1, run_id))
        conn.execute("UPDATE task_events SET created_at = created_at * 1000 WHERE task_id = ?", (tid,))
        conn.execute("UPDATE kanban_sync_state SET applying = 0, seconds_repaired = 0")
        conn.commit()
        outbox_before = _outbox_count(conn)
    finally:
        conn.close()
    sync._SYNCED_PATHS.clear()
    kbc._INITIALIZED_PATHS.clear()
    conn = kbc.connect(path)
    try:
        assert conn.execute("SELECT started_at FROM task_runs WHERE id = ?", (run_id,)).fetchone()[0] == started_s
        assert conn.execute("SELECT MAX(created_at) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0] < sync.MS_THRESHOLD
        assert conn.execute("SELECT seconds_repaired FROM kanban_sync_state").fetchone()[0] == 1
        assert _outbox_count(conn) == outbox_before  # the repair is not captured: the service repairs its own copy
    finally:
        conn.close()
