"""A 410 on a primary board's LIVE file rebuilds it in place from a snapshot (t_01a1d5f9de4020fa).

A snapshot carries only live rows and ``resync`` only upserts, so a row of this gateway's cards that the Board deleted
below its retention floor stayed in the live file forever. The rebuild now sweeps such rows, and only those: rows of
this gateway's cards (``created_on`` this gateway, or unset), absent from the snapshot, with no outbox op (an unpushed
or refused local write), no local tombstone, and not written after the rebuild began. A picked-up copy of another
gateway's card is another partition's row and is never swept. The replica path already unlinks and rebuilds its file.
"""
from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_resync_sweep as sweep
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeHub, FakeService

ME, OTHER, BOARD = "macbook", "mini", "default"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def svc():
    return FakeService()


@pytest.fixture
def live(kanban_home):
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(conn, BOARD, ME)
    sync.set_mode(conn, "primary")
    try:
        yield conn
    finally:
        conn.close()


class OpsDown(FakeHub):
    """/ops answers 503 while /changes and the snapshot keep working: a local write stays in the outbox."""

    def ops(self, board, ops):
        raise ksc.HubError(503, "service unavailable")


def _round(kanban_home, hub):
    return ksc.sync_board(BOARD, kanban_home / "kanban.db", hub, ME)


def _board_deletes(svc, *rows):
    hlc = f"{time.time_ns() // 1_000_000:013d}-0000-{OTHER}"
    res = svc.hub(OTHER).ops(BOARD, [{"client_op_id": f"del-{i}", "op": "delete", "table": table, "uid": uid,
                                      "hlc": hlc, "fields": {}} for i, (table, uid) in enumerate(rows)])
    assert [r["status"] for r in res["results"]] == ["applied"] * len(rows)


def _ids(conn, table="tasks", key="id"):
    return {r[0] for r in conn.execute(f"SELECT {key} FROM {table}")}


def _picked_up_foreign_copy(conn, task_id):
    """A copy of another gateway's card in this file (pickup), which the Board no longer holds."""
    with kbc.write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET applying = 1")
        conn.execute("INSERT INTO tasks (id, title, status, created_at, created_on) VALUES (?, 'foreign', 'ready', 1, ?)",
                     (task_id, OTHER))
        conn.execute("UPDATE kanban_sync_state SET applying = 0")


def test_a_410_on_the_live_file_sweeps_own_rows_the_board_deleted_and_keeps_local_work(live, svc, kanban_home):
    gone = kb.create_task(live, title="deleted on the Board")
    kept = kb.create_task(live, title="still on the Board")
    kb.add_comment(live, kept, "me", "a comment the Board deleted")
    kb.add_comment(live, kept, "me", "a comment the Board keeps")
    assert _round(kanban_home, svc.hub(ME)).error is None
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox").fetchone()  # everything acked
    dropped, comment_kept = [r[0] for r in live.execute("SELECT uid FROM task_comments ORDER BY id")]
    _board_deletes(svc, ("task_comments", dropped), ("tasks", gone))
    svc.compact(BOARD)  # the deletes are below the floor now: the live cursor gets 410
    assert int(sync.sync_state(live)["cursor"]) < svc.board(BOARD)["floor"]
    # The rows were written well before the rebuild: on a fast runner the 410 round can begin in the same second they
    # were created, and the `created_at < since` guard rightly spares a row written in the second the rebuild began.
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_state SET applying = 1")
        live.execute("UPDATE tasks SET created_at = created_at - 60")
        live.execute("UPDATE task_comments SET created_at = created_at - 60")
        live.execute("UPDATE kanban_sync_state SET applying = 0")

    local = kb.create_task(live, title="unpushed local card")  # its op waits in the outbox
    _picked_up_foreign_copy(live, "t_foreign_copy")
    with pytest.raises(ksc.HubError):  # the send after the pulls fails: the local card stays unpushed
        _round(kanban_home, OpsDown(svc, ME))

    tasks = _ids(live)
    assert gone not in tasks  # RED before: the snapshot only upserts, the Board's delete never reached this file
    assert dropped not in _ids(live, "task_comments", "uid")
    assert {kept, local, "t_foreign_copy"} <= tasks
    assert comment_kept in _ids(live, "task_comments", "uid")
    assert live.execute("SELECT 1 FROM kanban_sync_outbox WHERE row_uid = ? AND state = 'pending'",
                        (local,)).fetchone()
    assert live.execute("SELECT 1 FROM kanban_sync_tombstones WHERE table_name = 'tasks' AND row_uid = ?",
                        (gone,)).fetchone()  # a feed delete's tombstone: a stale copy can never bring it back
    assert int(sync.sync_state(live)["cursor"]) == svc.board(BOARD)["seq"]


def test_the_sweep_skips_rows_held_by_the_outbox_a_tombstone_or_written_after_the_rebuild_began(live, svc):
    held = kb.create_task(live, title="refused edit")
    stone = kb.create_task(live, title="tombstoned")
    fresh = kb.create_task(live, title="written during the rebuild")
    absent = kb.create_task(live, title="really gone")
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_sync_outbox WHERE row_uid != ?", (held,))
        live.execute("UPDATE kanban_sync_outbox SET state = 'rejected'")  # still a local write the Board never took
        live.execute("INSERT INTO kanban_sync_tombstones (table_name, row_uid, deleted_at) VALUES ('tasks', ?, 1)",
                     (stone,))
        live.execute("UPDATE kanban_sync_state SET applying = 1")  # capture off: no outbox op keeps 'fresh'
        live.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (int(time.time()) + 60, fresh))
        live.execute("UPDATE kanban_sync_state SET applying = 0")
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE row_uid = ?", (fresh,)).fetchone()
    swept = sweep.sweep_absent(live, {t: set() for t in ksc.IMPORT_ORDER}, BOARD, ME, since=int(time.time()) + 1)
    assert swept.get("tasks") == 1
    assert _ids(live) == {held, stone, fresh}
    assert absent not in _ids(live)


def test_a_row_that_gains_an_outbox_op_after_the_candidate_read_is_not_swept(live, svc, monkeypatch):
    racer = kb.create_task(live, title="edited between the candidate read and the apply")
    absent = kb.create_task(live, title="really gone")
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_sync_outbox")
    real_apply = sync.apply_remote_changes

    def local_edit_then_apply(conn, changes, **kw):  # a local write lands after the candidates were read
        assert {c.uid for c in changes if c.table == "tasks"} == {racer, absent}
        with kbc.write_txn(conn):
            conn.execute("UPDATE tasks SET title = 'edited locally' WHERE id = ?", (racer,))
        assert conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE row_uid = ?", (racer,)).fetchone()
        return real_apply(conn, changes, **kw)

    monkeypatch.setattr(sync, "apply_remote_changes", local_edit_then_apply)
    swept = sweep.sweep_absent(live, {t: set() for t in ksc.IMPORT_ORDER}, BOARD, ME, since=int(time.time()) + 60)
    assert swept.get("tasks") == 1  # the in-transaction re-check refused the racer
    assert racer in _ids(live)
    assert absent not in _ids(live)


def test_an_incomplete_snapshot_sweeps_nothing(live, svc):
    tid = kb.create_task(live, title="acked")
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_sync_outbox")
    assert sweep.sweep_absent(live, {}, BOARD, ME, since=int(time.time()) + 60) == {}
    assert tid in _ids(live)


def _acked_card(conn, title):
    """An own card the Board holds: every outbox op is acked, so only an absent row of it is a sweep candidate."""
    tid = kb.create_task(conn, title=title)
    with kbc.write_txn(conn):
        conn.execute("DELETE FROM kanban_sync_outbox")
    return tid


def _acked_child(conn, table, task_id, uid, created_at):
    """A child row the Board acked: written with capture off (``applying = 1``), so no outbox op holds it."""
    with kbc.write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET applying = 1")
        if table == "task_events":
            conn.execute("INSERT INTO task_events (task_id, kind, created_at, uid) VALUES (?, 'note', ?, ?)",
                         (task_id, created_at, uid))
        else:
            conn.execute("INSERT INTO task_comments (task_id, author, body, created_at, uid) "
                         "VALUES (?, 'me', 'c', ?, ?)", (task_id, created_at, uid))
        conn.execute("UPDATE kanban_sync_state SET applying = 0")
    assert not conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE row_uid = ?", (uid,)).fetchone()


def _seen_cards(*cards):
    """A complete snapshot that still holds ``cards`` but none of their child rows."""
    return {t: (set(cards) if t == "tasks" else set()) for t in ksc.IMPORT_ORDER}


def test_an_event_older_than_the_board_event_window_is_kept_and_a_recent_absent_one_is_swept(live, svc):
    card = _acked_card(live, "card with old and recent events")
    now = int(time.time())
    _acked_child(live, "task_events", card, "ev-old", now - ksc.EVENT_IMPORT_WINDOW_S - 3600)
    _acked_child(live, "task_events", card, "ev-recent", now - 60)
    sweep.sweep_absent(live, _seen_cards(card), BOARD, ME, since=now + 60)
    events = _ids(live, "task_events", "uid")
    assert "ev-old" in events  # the Board dropped it by retention, without a delete: its absence is no deletion
    assert "ev-recent" not in events
    assert card in _ids(live)


@pytest.mark.parametrize("state", ["pending", "rejected"])
def test_a_child_row_of_a_card_with_an_outbox_op_is_not_swept(live, svc, state):
    held = kb.create_task(live, title="card with an unacked or refused write")
    free = kb.create_task(live, title="acked card")
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_sync_outbox WHERE NOT (table_name = 'tasks' AND row_uid = ?)", (held,))
        live.execute("UPDATE kanban_sync_outbox SET state = ?", (state,))
    assert live.execute("SELECT count(*) FROM kanban_sync_outbox WHERE row_uid = ?", (held,)).fetchone()[0] >= 1
    now = int(time.time())
    _acked_child(live, "task_comments", held, "c-held", now - 60)
    _acked_child(live, "task_comments", free, "c-free", now - 60)
    sweep.sweep_absent(live, _seen_cards(held, free), BOARD, ME, since=now + 60)
    comments = _ids(live, "task_comments", "uid")
    assert "c-held" in comments  # its card's outbox op is a local write the Board has not taken: hold its rows
    assert "c-free" not in comments


def test_a_child_row_written_after_the_rebuild_began_is_not_swept(live, svc):
    card = _acked_card(live, "card with comments around the rebuild start")
    since = int(time.time()) - 600
    _acked_child(live, "task_comments", card, "c-before", since - 1)
    _acked_child(live, "task_comments", card, "c-same", since)
    _acked_child(live, "task_comments", card, "c-after", since + 1)
    sweep.sweep_absent(live, _seen_cards(card), BOARD, ME, since=since)
    comments = _ids(live, "task_comments", "uid")
    assert "c-after" in comments  # written after the rebuild began: the snapshot could not hold it
    assert "c-same" in comments  # the rebuild's first second: it may postdate held_at_start, so it is spared
    assert "c-before" not in comments


def test_a_swept_card_sweeps_its_children_first_so_each_child_gets_its_own_tombstone(live, svc):
    card = _acked_card(live, "card the Board deleted with its comment")
    _acked_child(live, "task_comments", card, "c-gone", int(time.time()) - 60)
    swept = sweep.sweep_absent(live, _seen_cards(), BOARD, ME, since=int(time.time()) + 60)
    assert swept.get("tasks") == 1 and swept.get("task_comments") == 1
    assert card not in _ids(live) and "c-gone" not in _ids(live, "task_comments", "uid")
    stones = {(r[0], r[1]) for r in live.execute("SELECT table_name, row_uid FROM kanban_sync_tombstones")}
    assert {("tasks", card), ("task_comments", "c-gone")} <= stones


class AckedMidRebuild(FakeHub):
    """A concurrent ``reconcile`` (it pushes without the round's lock): once the ``task_links`` snapshot is read to
    its end, it pushes this gateway's pending outbox from another connection and the Board acks it."""

    def __init__(self, svc, gateway, path):
        super().__init__(svc, gateway)
        self._path, self._pushed = path, False

    def snapshot_page(self, board, table, after="", limit=1000):
        if table == "task_runs" and not self._pushed:
            self._pushed = True
            other = kbc.connect(self._path)
            try:
                ksc.push(other, FakeHub(self.svc, ME), board, ME, primary=True)
            finally:
                other.close()
        return super().snapshot_page(board, table, after, limit)


def test_a_row_whose_outbox_op_is_acked_during_the_rebuild_is_not_swept(live, svc, kanban_home):
    parent = kb.create_task(live, title="parent on the Board")
    child = kb.create_task(live, title="child on the Board")
    assert _round(kanban_home, svc.hub(ME)).error is None
    new = kb.create_task(live, title="pending when the rebuild starts")
    kb.link_tasks(live, parent, child)
    link = f"{parent}>{child}"
    with kbc.write_txn(live):  # both older than the rebuild: only the outbox op protects them
        live.execute("UPDATE tasks SET created_at = created_at - 60 WHERE id = ?", (new,))
    assert {r[0] for r in live.execute("SELECT row_uid FROM kanban_sync_outbox")} >= {new, link}

    sweep.resync_live(live, AckedMidRebuild(svc, ME, kanban_home / "kanban.db"), BOARD, ME)

    assert not live.execute("SELECT 1 FROM kanban_sync_outbox").fetchone()  # the concurrent push acked both
    assert new in svc.board(BOARD)["rows"]["tasks"] and link in svc.board(BOARD)["rows"]["task_links"]
    assert new in _ids(live)  # RED before: absent from the tasks snapshot, op acked before the sweep: deleted
    assert (parent, child) in {tuple(r) for r in live.execute("SELECT parent_id, child_id FROM task_links")}
    assert not live.execute("SELECT 1 FROM kanban_sync_tombstones").fetchone()


class LinkedMidRebuild(FakeHub):
    """After ``held_at_start`` and after the ``task_links`` snapshot was read to its end, a link is written locally
    and a concurrent ``reconcile`` (no round lock) pushes it from another connection: the Board acks and holds it."""

    def __init__(self, svc, gateway, path, parent, child):
        super().__init__(svc, gateway)
        self._path, self._link, self._done = path, (parent, child), False

    def snapshot_page(self, board, table, after="", limit=1000):
        if table == "task_runs" and not self._done:
            self._done = True
            other = kbc.connect(self._path)
            try:
                kb.link_tasks(other, *self._link)
                ksc.push(other, FakeHub(self.svc, ME), board, ME, primary=True)
            finally:
                other.close()
        return super().snapshot_page(board, table, after, limit)


def test_a_link_written_and_acked_after_the_links_snapshot_is_not_swept(live, svc, kanban_home):
    parent = kb.create_task(live, title="parent on the Board")
    child = kb.create_task(live, title="child on the Board")
    assert _round(kanban_home, svc.hub(ME)).error is None
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox").fetchone()  # nothing held at the rebuild's start
    link = f"{parent}>{child}"

    swept = sweep.resync_live(live, LinkedMidRebuild(svc, ME, kanban_home / "kanban.db", parent, child), BOARD, ME)

    assert not live.execute("SELECT 1 FROM kanban_sync_outbox").fetchone()  # the concurrent push acked the link
    assert link in svc.board(BOARD)["rows"]["task_links"]
    assert "task_links" not in swept
    # RED before: no op, no creation time, absent from the links snapshot: deleted and tombstoned
    assert (parent, child) in {tuple(r) for r in live.execute("SELECT parent_id, child_id FROM task_links")}
    assert not live.execute("SELECT 1 FROM kanban_sync_tombstones").fetchone()


def test_a_link_in_the_file_when_the_rebuild_began_is_still_swept(live, svc):
    parent = kb.create_task(live, title="parent")
    child = kb.create_task(live, title="child")
    kb.link_tasks(live, parent, child)
    with kbc.write_txn(live):  # the backdate is a local write too: clear the outbox after it
        live.execute("UPDATE tasks SET created_at = created_at - 60")
        live.execute("UPDATE task_events SET created_at = created_at - 60")  # the linked event predates the rebuild
        live.execute("DELETE FROM kanban_sync_outbox")
    held = sweep.held_at_start(live)
    assert held.links == {f"{parent}>{child}"}
    seen = {"task_links": set(), "tasks": {parent, child}}  # task_events unread: the backdated events stay
    assert sweep.sweep_absent(live, seen, BOARD, ME, since=int(time.time()), held=held) == {"task_links": 1}
    assert not live.execute("SELECT 1 FROM task_links").fetchone()


class RelinkMidRebuild(FakeHub):
    """After ``held_at_start``: before the ``task_links`` snapshot is read, a link is unlinked locally and a concurrent
    ``reconcile`` acks the delete; after that snapshot was read to its end, the same key is linked again and acked.
    The key was in the file when the rebuild began, but the row the Board holds is a new one."""

    def __init__(self, svc, gateway, path, parent, child):
        super().__init__(svc, gateway)
        self._path, self._link, self._steps = path, (parent, child), set()

    def _push(self, board, act):
        other = kbc.connect(self._path)
        try:
            act(other, *self._link)
            ksc.push(other, FakeHub(self.svc, ME), board, ME, primary=True)
        finally:
            other.close()

    def snapshot_page(self, board, table, after="", limit=1000):
        if table == "task_links" and "unlink" not in self._steps:
            self._steps.add("unlink")
            self._push(board, kb.unlink_tasks)
        if table == "task_runs" and "relink" not in self._steps:
            self._steps.add("relink")
            self._push(board, kb.link_tasks)
        return super().snapshot_page(board, table, after, limit)


def test_a_link_unlinked_and_relinked_mid_rebuild_is_not_swept(live, svc, kanban_home):
    parent = kb.create_task(live, title="parent")
    child = kb.create_task(live, title="child")
    kb.link_tasks(live, parent, child)
    with kbc.write_txn(live):  # the first link predates the rebuild: only the mid-rebuild relink may spare it
        live.execute("UPDATE task_events SET created_at = created_at - 60")
    assert _round(kanban_home, svc.hub(ME)).error is None
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox").fetchone()  # nothing held at the rebuild's start

    swept = sweep.resync_live(live, RelinkMidRebuild(svc, ME, kanban_home / "kanban.db", parent, child), BOARD, ME)

    assert f"{parent}>{child}" in svc.board(BOARD)["rows"]["task_links"]
    assert "task_links" not in swept
    # RED before: the key was in Held.links, absent from the links snapshot, no op: deleted and tombstoned
    assert (parent, child) in {tuple(r) for r in live.execute("SELECT parent_id, child_id FROM task_links")}
    assert not live.execute("SELECT 1 FROM kanban_sync_tombstones WHERE table_name = 'task_links'").fetchone()


def test_a_row_written_in_the_second_the_rebuild_began_is_not_swept(live, svc):
    same = kb.create_task(live, title="written in the rebuild's first second")
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_sync_outbox")
    since = int(live.execute("SELECT created_at FROM tasks WHERE id = ?", (same,)).fetchone()[0])
    assert sweep.sweep_absent(live, {t: set() for t in ksc.IMPORT_ORDER}, BOARD, ME, since=since) == {}
    assert same in _ids(live)  # RED before: the guard was created_at <= since
