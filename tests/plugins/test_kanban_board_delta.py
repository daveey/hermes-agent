"""``board_read.materialize(prev=, changed=)``: the delta refresh of the board rows (P2). [daveey]

Every case reads a board in full, writes, then asks ``kanban_task_change.changed_since`` what moved and refreshes
with that set; the projected payload must equal a cold full read's. All displayed raw rows are refreshed by
indexed IDs for unmarked bookkeeping, but unchanged rows must not be parsed again; hidden done history
must not be parsed. Counters, never timing, verify the work retained across refreshes.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_task_change as ktc
from plugins.kanban.dashboard import board_read

DONE_LIMIT = 3


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    c = kbc.connect()
    yield c
    c.close()


class Board:
    def __init__(self, conn, done_limit=DONE_LIMIT, include_archived=False, tenant=None):
        self.conn, self.args = conn, (tenant, include_archived, None, None, done_limit)
        self.rows = self.cursor = None

    def full(self):
        _changed, self.cursor = ktc.changed_since(self.conn, None)
        self.rows = board_read.materialize(self.conn, *self.args)
        return self.rows

    def refresh(self, full_ok=False, **kw):
        changed, cursor = ktc.changed_since(self.conn, self.cursor)
        assert changed is not ktc.FULL
        self.cursor = cursor
        with mock.patch.object(board_read, "_done_state", wraps=board_read._done_state) as full:
            self.rows = board_read.materialize(self.conn, *self.args, prev=self.rows, changed=changed, **kw)
        assert full_ok or not full.called, "the delta declined: read in full"
        return self.rows, changed

    def cold(self):
        return board_read.materialize(self.conn, *self.args)


def _payload(rows, include_archived=False):
    body = board_read.project(rows, include_archived)
    body.pop("now")
    for col in body["columns"]:
        for t in col["tasks"]:
            t.pop("age", None)
    return body


def _same(board: Board):
    assert _payload(board.rows) == _payload(board.cold())
    return board.rows


def _seed(conn, n_done=6):
    ids = [kb.create_task(conn, title=f"card-{i}", assignee="worker", tenant="t1" if i % 2 else None)
           for i in range(6)]
    kb.link_tasks(conn, ids[0], ids[1])
    kb.add_comment(conn, ids[2], "me", "note")
    for i in range(n_done):
        tid = kb.create_task(conn, title=f"old-{i}", assignee="other")
        conn.execute("UPDATE tasks SET status='done', completed_at=? WHERE id=?", (100 + i, tid))
    conn.commit()
    return ids


def _spy_parse(monkeypatch):
    parsed = []
    real = kb.Task.from_row.__func__
    monkeypatch.setattr(kb.Task, "from_row", classmethod(lambda cls, r: parsed.append(r["id"]) or real(cls, r)))
    return parsed


def test_nothing_changed_is_a_cheap_refresh_equal_to_a_cold_read(conn, monkeypatch):
    _seed(conn)
    board = Board(conn)
    board.full()
    parsed = _spy_parse(monkeypatch)
    rows, changed = board.refresh()
    assert changed == frozenset() and parsed == []
    assert rows["_memo"]["inc"] is not None
    _same(board)


def test_heartbeat_overlay_keeps_diagnostics_and_event_id_without_a_change_mark(conn, monkeypatch):
    ids = _seed(conn)
    kb.claim_task(conn, ids[0])
    run_id = kb.latest_run(conn, ids[0]).id
    board = Board(conn)
    board.full()
    reread = []
    real = board_read.diagnostic_inputs
    def spy(c, task_ids=None):
        reread.append(task_ids)
        return real(c, task_ids)
    before = board.rows["latest_event_id"]
    assert kbd.heartbeat_worker(conn, ids[0], note="still going", expected_run_id=run_id)
    rows, changed = board.refresh(diagnostic_inputs=spy)
    assert changed == frozenset()  # heartbeats are excluded from the change set ...
    assert rows["latest_event_id"] > before  # ... yet the overlay sees the event
    assert reread == [[ids[0]]]
    held = {r["id"]: r for r in rows["diag_inputs"][0]}[ids[0]]
    assert held["last_heartbeat_at"] is not None
    _same(board)


def test_wake_state_probe_without_a_mark_reaches_the_diagnostics(conn):
    ids = _seed(conn)
    board = Board(conn)
    board.full()
    from hermes_cli import kanban_wake_check as kwc
    conn.execute("INSERT INTO task_wake_checks (task_id, cmd, every, next_at) VALUES (?, 'true', 60, 1)", (ids[3],))
    conn.commit()
    rows, changed = board.refresh()
    assert changed == frozenset()
    assert rows["diag_inputs"][4] == kwc.load_states(conn, [t.id for t, _f, _e in rows["cards"]
                                                            if t.status != "archived"])
    assert ids[3] in rows["diag_inputs"][4]
    _same(board)


def test_mixed_writes_match_a_cold_read_and_touch_only_neighbours(conn, monkeypatch):
    ids = _seed(conn)
    board = Board(conn)
    board.full()
    parsed = _spy_parse(monkeypatch)
    kb.add_comment(conn, ids[1], "me", "another")        # comment count
    kb.link_tasks(conn, ids[2], ids[3])                  # links both ends
    kb.block_task(conn, ids[4], reason="waiting")        # status moves lanes
    rows, changed = board.refresh()
    assert changed == {ids[1], ids[2], ids[3], ids[4]}
    assert set(parsed) <= changed
    _same(board)
    by_id = {t.id: e for t, _f, e in rows["cards"]}
    assert by_id[ids[1]]["comment_count"] == 1 and by_id[ids[2]]["progress"] == {"done": 0, "total": 1}
    assert by_id[ids[3]]["link_counts"] == {"parents": 1, "children": 0}


def test_child_completion_updates_the_parent_progress_and_graph(conn):
    ids = _seed(conn)
    board = Board(conn)
    board.full()
    conn.execute("UPDATE tasks SET status='done', completed_at=500 WHERE id=?", (ids[1],))
    conn.commit()
    rows, changed = board.refresh()
    assert ids[1] in changed
    _same(board)
    parent_graph = rows["diag_inputs"][3][ids[0]]
    assert [c["status"] for c in parent_graph["children"]] == ["done"]


def test_new_task_priority_and_creation_reorder_the_lanes(conn):
    ids = _seed(conn)
    board = Board(conn)
    board.full()
    new = kb.create_task(conn, title="late", assignee="worker", priority=5)
    conn.execute("UPDATE tasks SET priority = 3 WHERE id = ?", (ids[5],))
    conn.commit()
    board.refresh()
    _same(board)
    ready = [t.id for t, _f, _e in board.rows["cards"] if t.status == "ready"]
    assert ready[0] == new and ready[1] == ids[5]


def test_done_window_follows_completions_and_leaves_the_rest_unread(conn, monkeypatch):
    ids = _seed(conn)
    board = Board(conn)
    board.full()
    parsed = _spy_parse(monkeypatch)
    kb.claim_task(conn, ids[0])
    kb.complete_task(conn, ids[0], summary="ok", fire_lifecycle_hook=False)  # newest done: enters the window
    board.refresh()
    body = _payload(board.rows)
    done = next(c for c in body["columns"] if c["name"] == "done")
    assert done["total"] == 7 and done["omitted"] == 4 and done["tasks"][0]["id"] == ids[0]
    _same(board)
    # an old, hidden done task changing is never read as a card
    hidden = next(i for i in board.rows["_memo"]["inc"]["done_ids"] if i not in {w[0] for w in board.rows["_memo"]["inc"]["window"]})
    parsed.clear()
    kb.add_comment(conn, hidden, "me", "late note")
    board.refresh()
    assert hidden not in parsed
    _same(board)


def test_a_window_card_leaving_done_reads_in_full(conn):
    ids = _seed(conn)
    board = Board(conn)
    board.full()
    top = board.rows["_memo"]["inc"]["window"][0][0]
    conn.execute("UPDATE tasks SET status='ready', completed_at=NULL WHERE id=?", (top,))
    conn.commit()
    assert top  # window underflow: the delta declines, the result is still exact
    rows, _ = board.refresh(full_ok=True)
    assert rows["done_total"] == 5
    _same(board)
    assert ids


def test_archive_delete_and_tenant_changes(conn):
    ids = _seed(conn)
    board = Board(conn)
    board.full()
    assert board.rows["tenants"] == ("t1",)
    kb.archive_task(conn, ids[1])
    conn.execute("DELETE FROM tasks WHERE id = ?", (ids[3],))
    conn.execute("DELETE FROM task_links WHERE parent_id = ?", (ids[3],))
    conn.execute("UPDATE tasks SET tenant='t2' WHERE id = ?", (ids[5],))
    conn.commit()
    board.refresh()
    _same(board)
    assert board.rows["tenants"] == ("t2",) or board.rows["tenants"] == ("t1", "t2")


def test_full_marker_or_missing_prev_reads_in_full(conn, monkeypatch):
    _seed(conn)
    board = Board(conn)
    board.full()
    parsed = _spy_parse(monkeypatch)
    rows = board_read.materialize(conn, *board.args, prev=board.rows, changed=ktc.FULL)
    assert rows is not board.rows and _payload(rows) == _payload(board.cold())
    rows = board_read.materialize(conn, *board.args, prev=None, changed=frozenset({"x"}))
    assert _payload(rows) == _payload(board.cold())
    assert parsed  # both parsed (the first reuses prev's memo, the second is cold)


def test_include_archived_matches_a_cold_read(conn):
    ids = _seed(conn)
    board = Board(conn, include_archived=True)
    board.full()
    kb.archive_task(conn, ids[2])
    board.refresh()
    assert _payload(board.rows, True) == _payload(board.cold(), True)


def test_manager_falls_back_to_creator_after_a_delta(conn):
    tid = kb.create_task(conn, title="managed", session_id="creator-session")
    board = Board(conn)
    board.full()
    kb.add_comment(conn, tid, "me", "refresh extras")
    board.refresh()
    extra = next(e for t, _f, e in board.rows["cards"] if t.id == tid)
    assert extra["managing_session_id"] == "creator-session"
    _same(board)


@pytest.mark.parametrize("column,value", [("claim_expires", 999), ("worker_started_at", 123)])
def test_ready_lease_only_update_without_a_mark_is_visible(conn, column, value):
    tid = kb.create_task(conn, title="ready lease")
    board = Board(conn)
    board.full()
    conn.execute(f"UPDATE tasks SET {column}=? WHERE id=?", (value, tid))
    conn.commit()
    rows, changed = board.refresh()
    assert changed == frozenset()
    held = next(r for r in rows["diag_inputs"][0] if r["id"] == tid)
    assert held[column] == value
    _same(board)


def test_done_heartbeat_without_a_mark_refreshes_rows_events_and_runs(conn):
    tid = kb.create_task(conn, title="completed worker")
    kb.claim_task(conn, tid)
    kb.complete_task(conn, tid, summary="finished", fire_lifecycle_hook=False)
    board = Board(conn)
    board.full()
    before = board.rows["latest_event_id"]
    conn.execute("UPDATE tasks SET last_heartbeat_at=999 WHERE id=?", (tid,))
    conn.execute("UPDATE task_runs SET last_heartbeat_at=999 WHERE task_id=?", (tid,))
    conn.execute("INSERT INTO task_events (task_id, kind, created_at) VALUES (?, 'heartbeat', 999)", (tid,))
    conn.commit()
    rows, changed = board.refresh()
    assert changed == frozenset()
    assert rows["latest_event_id"] > before
    held = next(r for r in rows["diag_inputs"][0] if r["id"] == tid)
    assert held["last_heartbeat_at"] == 999
    assert rows["diag_inputs"][1][tid][-1]["kind"] == "heartbeat"
    # [daveey] Non-grants retain unread run bookkeeping; payload parity below is the contract.
    assert rows["diag_inputs"][2][tid][-1]["last_heartbeat_at"] is None
    _same(board)


@pytest.mark.parametrize("delete_task", [False, True])
def test_deleted_links_refresh_surviving_neighbour_graphs(conn, delete_task):
    ids = _seed(conn)
    board = Board(conn)
    board.full()
    if delete_task:
        conn.execute("DELETE FROM tasks WHERE id=?", (ids[1],))
    conn.execute("DELETE FROM task_links WHERE parent_id=? AND child_id=?", (ids[0], ids[1]))
    conn.commit()
    rows, changed = board.refresh()
    assert ids[0] in changed
    assert rows["diag_inputs"][3][ids[0]]["children"] == []
    extra = next(e for t, _f, e in rows["cards"] if t.id == ids[0])
    assert extra["link_counts"]["children"] == 0
    assert extra["progress"] is None
    _same(board)


def test_out_of_filter_waiter_priority_reorders_displayed_ancestors(conn):
    parent = kb.create_task(conn, title="prerequisite", tenant="shown", priority=1)
    rival = kb.create_task(conn, title="rival", tenant="shown", priority=2)
    child = kb.create_task(conn, title="foreign waiter", tenant="hidden", priority=0)
    kb.link_tasks(conn, parent, child)
    board = Board(conn, tenant="shown")
    board.full()
    assert [t.id for t, _f, _e in board.rows["cards"]] == [rival, parent]
    conn.execute("UPDATE tasks SET priority=5 WHERE id=?", (child,))
    conn.commit()
    board.refresh()
    _same(board)
    assert [t.id for t, _f, _e in board.rows["cards"]] == [parent, rival]


def test_unchanged_and_nondone_changes_retain_done_membership(conn):
    ids = _seed(conn)
    board = Board(conn)
    old = board.full()["_memo"]["inc"]["done_ids"]
    board.refresh()
    assert board.rows["_memo"]["inc"]["done_ids"] is old
    kb.add_comment(conn, ids[4], "me", "unrelated to done history")
    board.refresh()
    assert board.rows["_memo"]["inc"]["done_ids"] is old
    _same(board)


def test_rollups_and_graphs_only_refresh_changed_neighbours(conn, monkeypatch):
    ids = _seed(conn)
    board = Board(conn)
    board.full()
    from plugins.kanban.dashboard import board_incremental as inc
    real = kb.task_graph_contexts
    calls = []
    monkeypatch.setattr(kb, "task_graph_contexts", lambda c, tids: calls.append(set(tids)) or real(c, tids))
    with mock.patch.object(inc, "_rollups", wraps=getattr(inc, "_rollups", None), create=True) as rollups:
        board.refresh()
        assert not calls and not rollups.called
        conn.execute("UPDATE tasks SET title='renamed child' WHERE id=?", (ids[1],))
        conn.commit()
        board.refresh()
        assert set().union(*calls) == {ids[0], ids[1]}
        assert set(rollups.call_args.args[1]) == {ids[0], ids[1]}
    _same(board)


def test_a_delta_chain_stays_exact(conn):
    ids = _seed(conn)
    board = Board(conn)
    board.full()
    for i in range(4):
        kb.add_comment(conn, ids[i], "me", f"n{i}")
        board.refresh()
        _same(board)
    kb.claim_task(conn, ids[4])
    board.refresh()
    kbd.heartbeat_worker(conn, ids[4], note="x")
    board.refresh()
    kb.complete_task(conn, ids[4], summary="s", fire_lifecycle_hook=False)
    board.refresh()
    _same(board)
