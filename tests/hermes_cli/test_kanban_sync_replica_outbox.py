"""A replica queues no outbox op; ops it captured before the guard are undone from the Board. [daveey]

Hub incident 2026-11-23 (see ``kanban_sync_pending.discard_replica_writes``): a phantom run the legacy backfill wrote
into the hub replica left two ``pending`` ops there. The pull kept the card's ``current_run_uid`` on that run over
every Board echo, and divergence repair re-sent the live value every ~30 s.
"""
from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_sync_pending as pending
from tests.hermes_cli import test_kanban_cross_gateway_parents as fixtures

kanban_home = fixtures.kanban_home
live = fixtures.live
svc = fixtures.svc


@pytest.fixture(autouse=True)
def _fresh_rotation(monkeypatch):
    """Each test reads from the start: the read rotation is process state."""
    monkeypatch.setattr(pending, "_NEXT_READ", {})


def _board_card_with_run(live, kanban_home, svc):
    """A card of this gateway, claimed and finished here, synced: the Board holds its run."""
    tid = kb.create_task(live, title="replica outbox", assignee="default")
    client.sync_board(fixtures.BOARD, kanban_home / "kanban.db", svc.hub(fixtures.ME), fixtures.ME)
    claimed = kb.claim_task(live, tid)
    assert claimed is not None
    run_uid = live.execute("SELECT r.uid FROM tasks t JOIN task_runs r ON r.id = t.current_run_id WHERE t.id = ?",
                           (tid,)).fetchone()[0]
    for _ in range(3):
        client.sync_board(fixtures.BOARD, kanban_home / "kanban.db", svc.hub(fixtures.ME), fixtures.ME)
    return tid, run_uid


def _guarded(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                        (pending.REPLICA_GUARD,)).fetchone() is not None


def _contaminate(replica, tid):
    """What the legacy backfill did to the hub replica, in a file from before the guard: a captured run insert and
    a captured card update. The guard is back in place afterwards, as the next ``open_shadow`` puts it."""
    replica.execute(f"DROP TRIGGER {pending.REPLICA_GUARD}")
    before = {r[0] for r in replica.execute("SELECT op_id FROM kanban_sync_outbox")}
    cur = replica.execute("INSERT INTO task_runs (task_id, profile, status, started_at) "
                          "VALUES (?, 'default', 'running', 1)", (tid,))
    replica.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (cur.lastrowid, tid))
    replica.commit()
    phantom = replica.execute("SELECT uid FROM task_runs WHERE id = ?", (cur.lastrowid,)).fetchone()[0]
    ops = {(r[1], r[2]) for r in replica.execute("SELECT op_id, table_name, row_uid FROM kanban_sync_outbox")
           if r[0] not in before}
    assert ops == {("task_runs", phantom), ("tasks", tid)}  # the replica captured both writes
    pending.guard_replica(replica)
    assert _guarded(replica)
    return phantom


def _current_run_uid(conn, tid):
    row = conn.execute("SELECT r.uid FROM tasks t LEFT JOIN task_runs r ON r.id = t.current_run_id WHERE t.id = ?",
                       (tid,)).fetchone()
    return row[0] if row else None


def test_pull_drops_replica_writes_and_takes_the_board_run(live, kanban_home, svc):
    tid, run_uid = _board_card_with_run(live, kanban_home, svc)
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    try:
        assert _current_run_uid(replica, tid) == run_uid
        phantom = _contaminate(replica, tid)
        assert pending.pending_task_fields(replica) == {tid: {"current_run_uid"}}  # what masked every echo
        client.pull(replica, svc.hub(fixtures.ME), fixtures.BOARD, observers=False)
        assert replica.execute("SELECT COUNT(*) FROM kanban_sync_outbox").fetchone()[0] == 0
        assert replica.execute("SELECT COUNT(*) FROM task_runs WHERE uid = ?", (phantom,)).fetchone()[0] == 0
        assert _current_run_uid(replica, tid) == run_uid
        assert pending.pending_task_fields(replica) == {}
    finally:
        replica.close()


def test_without_the_discard_the_pull_keeps_the_phantom_run(live, kanban_home, svc, monkeypatch):
    """The failure the discard removes: the pending op keeps the replica on the phantom run through a Board echo."""
    tid, run_uid = _board_card_with_run(live, kanban_home, svc)
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    try:
        phantom = _contaminate(replica, tid)
        monkeypatch.setattr(client, "discard_replica_writes", lambda *a, **k: 0)  # a round pulls the replica too
        live.execute("UPDATE tasks SET priority = 7 WHERE id = ?", (tid,))  # a new Board version of the card
        live.commit()
        client.sync_board(fixtures.BOARD, kanban_home / "kanban.db", svc.hub(fixtures.ME), fixtures.ME)
        client.pull(replica, svc.hub(fixtures.ME), fixtures.BOARD, observers=False)
        assert replica.execute("SELECT priority FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == 7
        assert _current_run_uid(replica, tid) == phantom != run_uid  # the Board still names run_uid
    finally:
        replica.close()


def test_a_live_file_keeps_its_outbox(live, kanban_home, svc):
    """Only a replica's ops are dropped: a live board's outbox is its unsent local work."""
    tid = kb.create_task(live, title="unsent", assignee="default")
    before = live.execute("SELECT COUNT(*) FROM kanban_sync_outbox").fetchone()[0]
    assert before > 0
    assert pending.discard_replica_writes(live, svc.hub(fixtures.ME), fixtures.BOARD) == 0
    assert not _guarded(live)  # the live board's writes are its work: only a replica is guarded
    assert live.execute("SELECT COUNT(*) FROM kanban_sync_outbox").fetchone()[0] == before
    assert kb.get_task(live, tid) is not None


def test_failed_board_read_leaves_the_replica_for_the_next_round(live, kanban_home, svc):
    tid, _run_uid = _board_card_with_run(live, kanban_home, svc)
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    try:
        phantom = _contaminate(replica, tid)

        class Down:
            def row_evidence(self, *_a):
                raise OSError("board unreachable")

        assert pending.discard_replica_writes(replica, Down(), fixtures.BOARD) == 0
        assert replica.execute("SELECT COUNT(*) FROM kanban_sync_outbox").fetchone()[0] == 2
        assert _current_run_uid(replica, tid) == phantom
        assert sync.sync_state(replica)["applying"] in (0, None)
    finally:
        replica.close()


def test_a_guarded_replica_queues_no_op_and_the_next_board_version_heals(live, kanban_home, svc):
    """The guard: a write outside the pull (a migration, the containment install, a stray bug) still runs, queues
    nothing, and so cannot mask the Board's next version of the row."""
    tid, _run_uid = _board_card_with_run(live, kanban_home, svc)
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    try:
        assert _guarded(replica)
        replica.execute("UPDATE tasks SET priority = 9 WHERE id = ?", (tid,))
        replica.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'note', '{}', 1)",
                        (tid,))
        replica.commit()
        assert replica.execute("SELECT priority FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == 9
        assert replica.execute("SELECT COUNT(*) FROM kanban_sync_outbox").fetchone()[0] == 0
        assert pending.pending_task_fields(replica) == {}
        live.execute("UPDATE tasks SET priority = 3 WHERE id = ?", (tid,))
        live.commit()
        client.sync_board(fixtures.BOARD, kanban_home / "kanban.db", svc.hub(fixtures.ME), fixtures.ME)
        client.pull(replica, svc.hub(fixtures.ME), fixtures.BOARD, observers=False)
        assert replica.execute("SELECT priority FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == 3
    finally:
        replica.close()


def test_a_guarded_replica_still_takes_the_feed(live, kanban_home, svc):
    """The pull writes under ``applying``, which the capture triggers skip, so the guard never sees it."""
    tid, _run_uid = _board_card_with_run(live, kanban_home, svc)
    live.execute("UPDATE tasks SET priority = 5, title = 'renamed' WHERE id = ?", (tid,))
    live.commit()
    kb.complete_task(live, tid, result="done")
    for _ in range(2):
        client.sync_board(fixtures.BOARD, kanban_home / "kanban.db", svc.hub(fixtures.ME), fixtures.ME)
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    try:
        assert _guarded(replica)
        row = replica.execute("SELECT priority, title, status FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert tuple(row) == (5, "renamed", "done")
        assert replica.execute("SELECT COUNT(*) FROM kanban_sync_outbox").fetchone()[0] == 0
    finally:
        replica.close()


def test_a_failed_restore_keeps_the_ops_for_the_next_round(live, kanban_home, svc, monkeypatch):
    """The ops go only after the Board's rows are in: a failing apply leaves every op, and the next round heals."""
    tid, run_uid = _board_card_with_run(live, kanban_home, svc)
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    try:
        _contaminate(replica, tid)
        real = sync.apply_remote_changes

        def failing(*_a, **_k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(sync, "apply_remote_changes", failing)
        # logged, not raised: the feed pull after it still runs
        assert pending.discard_replica_writes(replica, svc.hub(fixtures.ME), fixtures.BOARD) == 0
        assert replica.execute("SELECT COUNT(*) FROM kanban_sync_outbox").fetchone()[0] == 2
        assert pending.pending_task_fields(replica) == {}  # ``discarding`` ops no longer mask the card
        monkeypatch.setattr(sync, "apply_remote_changes", real)
        assert pending.discard_replica_writes(replica, svc.hub(fixtures.ME), fixtures.BOARD) == 2
        assert replica.execute("SELECT COUNT(*) FROM kanban_sync_outbox").fetchone()[0] == 0
        assert _current_run_uid(replica, tid) == run_uid
    finally:
        replica.close()


def test_a_stray_delete_is_restored_from_the_board(live, kanban_home, svc):
    """A captured delete also left a replica tombstone, which would make the apply skip the Board's row."""
    tid, run_uid = _board_card_with_run(live, kanban_home, svc)
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    try:
        replica.execute(f"DROP TRIGGER {pending.REPLICA_GUARD}")  # a file from before the guard
        replica.execute(f"DROP TRIGGER {pending.REPLICA_TOMBSTONE_GUARD}")
        replica.execute("DELETE FROM tasks WHERE id = ?", (tid,))
        replica.commit()
        assert replica.execute("SELECT COUNT(*) FROM kanban_sync_tombstones WHERE row_uid = ?", (tid,)).fetchone()[0]
        pending.guard_replica(replica)
        assert pending.discard_replica_writes(replica, svc.hub(fixtures.ME), fixtures.BOARD) == 1
        assert replica.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone() is not None
        assert _current_run_uid(replica, tid) == run_uid
        assert replica.execute("SELECT COUNT(*) FROM kanban_sync_tombstones WHERE row_uid = ?", (tid,)).fetchone()[0] == 0
        assert replica.execute("SELECT COUNT(*) FROM kanban_sync_outbox").fetchone()[0] == 0
    finally:
        replica.close()


def test_a_phantom_link_is_removed(live, kanban_home, svc):
    """``task_links`` ops are keyed ``parent>child``: the Board never had this edge, so it goes."""
    tid, _run_uid = _board_card_with_run(live, kanban_home, svc)
    other = kb.create_task(live, title="other", assignee="default")
    client.sync_board(fixtures.BOARD, kanban_home / "kanban.db", svc.hub(fixtures.ME), fixtures.ME)
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    try:
        client.pull(replica, svc.hub(fixtures.ME), fixtures.BOARD, observers=False)
        replica.execute(f"DROP TRIGGER {pending.REPLICA_GUARD}")
        replica.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (other, tid))
        replica.commit()
        assert [tuple(r) for r in replica.execute("SELECT table_name, row_uid FROM kanban_sync_outbox")] == [
            ("task_links", f"{other}>{tid}")]
        pending.guard_replica(replica)
        assert pending.discard_replica_writes(replica, svc.hub(fixtures.ME), fixtures.BOARD) == 1
        assert replica.execute("SELECT COUNT(*) FROM task_links WHERE parent_id = ? AND child_id = ?",
                               (other, tid)).fetchone()[0] == 0
        assert replica.execute("SELECT COUNT(*) FROM kanban_sync_outbox").fetchone()[0] == 0
    finally:
        replica.close()


class _Attesting:
    """The fixture Board, with one row's evidence replaced."""

    def __init__(self, hub, key, state):
        self.hub, self.key, self.state = hub, key, state

    def row_evidence(self, board, table, uid):
        if (table, uid) == self.key:
            return {"state": self.state, "row": None}
        return self.hub.row_evidence(board, table, uid)

    def __getattr__(self, name):
        return getattr(self.hub, name)


def test_a_row_the_board_deleted_is_deleted_here(live, kanban_home, svc):
    tid, run_uid = _board_card_with_run(live, kanban_home, svc)
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    try:
        phantom = _contaminate(replica, tid)
        hub = _Attesting(svc.hub(fixtures.ME), ("task_runs", phantom), "deleted")
        assert pending.discard_replica_writes(replica, hub, fixtures.BOARD) == 2
        assert replica.execute("SELECT COUNT(*) FROM task_runs WHERE uid = ?", (phantom,)).fetchone()[0] == 0
        assert _current_run_uid(replica, tid) == run_uid
        assert replica.execute("SELECT COUNT(*) FROM kanban_sync_outbox").fetchone()[0] == 0
    finally:
        replica.close()


def test_a_row_below_the_board_history_floor_is_gone_too(live, kanban_home, svc):
    """``unknown``: the Board holds no row and no tombstone for it, so for a replica it is gone like the rest."""
    tid, run_uid = _board_card_with_run(live, kanban_home, svc)
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    try:
        phantom = _contaminate(replica, tid)
        hub = _Attesting(svc.hub(fixtures.ME), ("task_runs", phantom), "unknown")
        assert pending.discard_replica_writes(replica, hub, fixtures.BOARD) == 2
        assert replica.execute("SELECT COUNT(*) FROM task_runs WHERE uid = ?", (phantom,)).fetchone()[0] == 0
        assert _current_run_uid(replica, tid) == run_uid
        assert replica.execute("SELECT COUNT(*) FROM kanban_sync_outbox").fetchone()[0] == 0
    finally:
        replica.close()


def test_a_stray_update_of_a_held_run_is_restored(live, kanban_home, svc):
    """A child row the Board holds: the pending op and the run's ``sync_version`` fence must not keep the stray."""
    _tid, run_uid = _board_card_with_run(live, kanban_home, svc)
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    try:
        board_row = replica.execute("SELECT status, summary FROM task_runs WHERE uid = ?", (run_uid,)).fetchone()
        replica.execute(f"DROP TRIGGER {pending.REPLICA_GUARD}")
        replica.execute("UPDATE task_runs SET status = 'crashed', summary = 'STRAY' WHERE uid = ?", (run_uid,))
        replica.commit()
        assert [tuple(r) for r in replica.execute("SELECT table_name, row_uid FROM kanban_sync_outbox")] == [
            ("task_runs", run_uid)]
        pending.guard_replica(replica)
        assert pending.discard_replica_writes(replica, svc.hub(fixtures.ME), fixtures.BOARD) == 1
        assert tuple(replica.execute("SELECT status, summary FROM task_runs WHERE uid = ?",
                                     (run_uid,)).fetchone()) == tuple(board_row)
        assert replica.execute("SELECT COUNT(*) FROM kanban_sync_outbox").fetchone()[0] == 0
    finally:
        replica.close()


def test_a_stray_delete_on_a_guarded_replica_leaves_no_tombstone(live, kanban_home, svc):
    """Without its tombstone the card comes back with the Board's next version, like any pulled row."""
    tid, _run_uid = _board_card_with_run(live, kanban_home, svc)
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    try:
        replica.execute("DELETE FROM tasks WHERE id = ?", (tid,))
        replica.commit()
        assert replica.execute("SELECT COUNT(*) FROM kanban_sync_tombstones WHERE row_uid = ?", (tid,)).fetchone()[0] == 0
        assert replica.execute("SELECT COUNT(*) FROM kanban_sync_outbox").fetchone()[0] == 0
        live.execute("UPDATE tasks SET priority = 4 WHERE id = ?", (tid,))
        live.commit()
        client.sync_board(fixtures.BOARD, kanban_home / "kanban.db", svc.hub(fixtures.ME), fixtures.ME)
        client.pull(replica, svc.hub(fixtures.ME), fixtures.BOARD, observers=False)
        assert replica.execute("SELECT priority FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == 4
    finally:
        replica.close()


def test_the_pull_keeps_its_own_tombstones_on_a_guarded_replica(live, kanban_home, svc):
    """The tombstone guard skips only writes outside ``applying``: a Board delete still tombstones the replica."""
    tid, _run_uid = _board_card_with_run(live, kanban_home, svc)
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    try:
        kb.delete_task(live, tid)
        client.sync_board(fixtures.BOARD, kanban_home / "kanban.db", svc.hub(fixtures.ME), fixtures.ME)
        client.pull(replica, svc.hub(fixtures.ME), fixtures.BOARD, observers=False)
        assert replica.execute("SELECT COUNT(*) FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == 0
        assert replica.execute("SELECT COUNT(*) FROM kanban_sync_tombstones WHERE row_uid = ?", (tid,)).fetchone()[0] == 1
    finally:
        replica.close()


class _FailingAfter:
    """The fixture Board, failing every read after the first ``n``."""

    def __init__(self, hub, n):
        self.hub, self.left = hub, n

    def row_evidence(self, board, table, uid):
        if self.left <= 0:
            raise OSError("board unreachable")
        self.left -= 1
        return self.hub.row_evidence(board, table, uid)

    def __getattr__(self, name):
        return getattr(self.hub, name)


def test_a_partial_board_read_resolves_the_ops_it_covered(live, kanban_home, svc, monkeypatch):
    """Reads stop at a failure or the per-round cap; ops whose rows were all read resolve now, the rest later."""
    tid, run_uid = _board_card_with_run(live, kanban_home, svc)
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    try:
        _contaminate(replica, tid)  # keys in order: the phantom run, then the card both ops name
        ops = [tuple(r) for r in replica.execute("SELECT table_name FROM kanban_sync_outbox ORDER BY rowid")]
        assert ops == [("task_runs",), ("tasks",)]
        assert pending.discard_replica_writes(replica, _FailingAfter(svc.hub(fixtures.ME), 1), fixtures.BOARD) == 0
        assert replica.execute("SELECT COUNT(*) FROM kanban_sync_outbox").fetchone()[0] == 2
        monkeypatch.setattr(pending, "_EVIDENCE_PER_ROUND", 1)
        assert pending.discard_replica_writes(replica, svc.hub(fixtures.ME), fixtures.BOARD) == 0
        monkeypatch.setattr(pending, "_EVIDENCE_PER_ROUND", 2)
        assert pending.discard_replica_writes(replica, svc.hub(fixtures.ME), fixtures.BOARD) == 2
        assert _current_run_uid(replica, tid) == run_uid
    finally:
        replica.close()


def test_a_malformed_op_still_resolves_by_its_own_row(live, kanban_home, svc):
    tid, run_uid = _board_card_with_run(live, kanban_home, svc)
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    try:
        phantom = _contaminate(replica, tid)
        replica.execute("UPDATE kanban_sync_outbox SET fields = '[not json' WHERE table_name = 'task_runs'")
        replica.commit()
        assert pending.discard_replica_writes(replica, svc.hub(fixtures.ME), fixtures.BOARD) == 2
        assert replica.execute("SELECT COUNT(*) FROM task_runs WHERE uid = ?", (phantom,)).fetchone()[0] == 0
        assert _current_run_uid(replica, tid) == run_uid
    finally:
        replica.close()


class _Unanswered:
    """The fixture Board, failing every read of one row."""

    def __init__(self, hub, key):
        self.hub, self.key = hub, key

    def row_evidence(self, board, table, uid):
        if (table, uid) == self.key:
            raise OSError("413 payload too large")
        return self.hub.row_evidence(board, table, uid)

    def __getattr__(self, name):
        return getattr(self.hub, name)


def test_a_row_the_board_never_answers_for_holds_back_only_its_own_op(live, kanban_home, svc):
    """A read that always fails is skipped, so the ops after it still resolve round after round."""
    tid, _run_uid = _board_card_with_run(live, kanban_home, svc)
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    try:
        phantom = _contaminate(replica, tid)  # the phantom run's key is read first
        hub = _Unanswered(svc.hub(fixtures.ME), ("task_runs", phantom))
        assert pending.discard_replica_writes(replica, hub, fixtures.BOARD) == 1
        assert [tuple(r) for r in replica.execute("SELECT table_name, row_uid FROM kanban_sync_outbox")] == [
            ("task_runs", phantom)]
        assert replica.execute("SELECT COUNT(*) FROM task_runs WHERE uid = ?", (phantom,)).fetchone()[0] == 1
    finally:
        replica.close()


def test_a_restore_the_quarantine_withholds_keeps_its_op_and_rows(live, kanban_home, svc, monkeypatch):
    """Undone while its restore is withheld, a row would be neither the stray nor the Board's: the op waits."""
    from hermes_cli import kanban_sync_quarantine as quarantine
    tid, run_uid = _board_card_with_run(live, kanban_home, svc)
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    try:
        phantom = _contaminate(replica, tid)
        real = quarantine.certified_page
        monkeypatch.setattr(quarantine, "certified_page", lambda *_a: [])  # a quarantined card: nothing passes
        assert pending.discard_replica_writes(replica, svc.hub(fixtures.ME), fixtures.BOARD) == 0
        assert replica.execute("SELECT COUNT(*) FROM kanban_sync_outbox").fetchone()[0] == 2
        assert [r[0] for r in replica.execute("SELECT state FROM kanban_sync_outbox")] == ["pending", "pending"]
        assert _current_run_uid(replica, tid) == phantom  # untouched: no side effect for an op that waits
        monkeypatch.setattr(quarantine, "certified_page", real)
        assert pending.discard_replica_writes(replica, svc.hub(fixtures.ME), fixtures.BOARD) == 2
        assert _current_run_uid(replica, tid) == run_uid
    finally:
        replica.close()


def test_the_guard_installs_once_and_survives_a_sync_trigger_drop(live, kanban_home, svc):
    _tid, _run_uid = _board_card_with_run(live, kanban_home, svc)
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    try:
        schema = replica.execute("PRAGMA schema_version").fetchone()[0]
        pending.guard_replica(replica)
        assert replica.execute("PRAGMA schema_version").fetchone()[0] == schema  # already there: no rewrite
        with kbc.write_txn(replica):
            sync.drop_triggers(replica)  # a sync-off open drops ``kanban_sync_%`` triggers only
        assert _guarded(replica)
        assert replica.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                               (pending.REPLICA_TOMBSTONE_GUARD,)).fetchone()[0] == 1
    finally:
        replica.close()


def test_a_live_file_never_reads_the_board(live, kanban_home, svc):
    """On a live file the discard is a path check: its outbox is real work, and no Board read is made."""
    kb.create_task(live, title="unsent", assignee="default")

    class NoBoard:
        def row_evidence(self, *_a):
            raise AssertionError("a live file read the Board")

    assert pending.discard_replica_writes(live, NoBoard(), fixtures.BOARD) == 0


def test_a_withheld_op_and_a_resolved_one_in_the_same_round(live, kanban_home, svc, monkeypatch):
    """Only the resolved op's rows are undone: the withheld card keeps its stray run, its pointer and its ops."""
    from hermes_cli import kanban_sync_quarantine as quarantine
    held, _held_run = _board_card_with_run(live, kanban_home, svc)
    freed, freed_run = _board_card_with_run(live, kanban_home, svc)
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    try:
        client.pull(replica, svc.hub(fixtures.ME), fixtures.BOARD, observers=False)
        held_phantom = _contaminate(replica, held)
        freed_phantom = _contaminate(replica, freed)
        real = quarantine.certified_page
        # The held card is quarantined: its own row is withheld, while its phantom run's delete passes. That delete
        # must still wait, since the run's op also names the withheld card.
        monkeypatch.setattr(quarantine, "certified_page", lambda *a: [ch for ch in real(*a) if ch.uid != held])
        assert pending.discard_replica_writes(replica, svc.hub(fixtures.ME), fixtures.BOARD) == 2
        assert _current_run_uid(replica, freed) == freed_run
        assert replica.execute("SELECT COUNT(*) FROM task_runs WHERE uid = ?", (freed_phantom,)).fetchone()[0] == 0
        assert _current_run_uid(replica, held) == held_phantom
        assert replica.execute("SELECT COUNT(*) FROM task_runs WHERE uid = ?", (held_phantom,)).fetchone()[0] == 1
        assert {(r[0], r[1]) for r in replica.execute("SELECT table_name, row_uid FROM kanban_sync_outbox")} == {
            ("task_runs", held_phantom), ("tasks", held)}
        assert {r[0] for r in replica.execute("SELECT state FROM kanban_sync_outbox")} == {"pending"}
    finally:
        replica.close()


class _Counting:
    """A Board that never answers, counting the reads it was asked for."""

    def __init__(self):
        self.reads = 0

    def row_evidence(self, *_a):
        self.reads += 1
        raise OSError("board unreachable")


def test_three_failed_reads_end_the_round(live, kanban_home, svc):
    tid, _run_uid = _board_card_with_run(live, kanban_home, svc)
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    try:
        _contaminate(replica, tid)
        for _ in range(3):
            _contaminate(replica, tid)  # more rows than the failure limit
        hub = _Counting()
        assert pending.discard_replica_writes(replica, hub, fixtures.BOARD) == 0
        assert hub.reads == pending._FAILED_READS_PER_ROUND
    finally:
        replica.close()


class _NeverFor:
    """The fixture Board, failing every read of the given rows."""

    def __init__(self, hub, keys):
        self.hub, self.keys = hub, set(keys)

    def row_evidence(self, board, table, uid):
        if (table, uid) in self.keys:
            raise OSError("400 bad row")
        return self.hub.row_evidence(board, table, uid)

    def __getattr__(self, name):
        return getattr(self.hub, name)


def test_rows_that_keep_failing_do_not_starve_the_ops_behind_them(live, kanban_home, svc):
    """Three rows at the front that always fail would end every round at the same place without the rotation."""
    tid, _run_uid = _board_card_with_run(live, kanban_home, svc)
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    try:
        stuck = [_contaminate(replica, tid) for _ in range(3)]
        last = _contaminate(replica, tid)
        hub = _NeverFor(svc.hub(fixtures.ME), [("task_runs", uid) for uid in stuck])
        for _ in range(4):
            pending.discard_replica_writes(replica, hub, fixtures.BOARD)
        assert replica.execute("SELECT COUNT(*) FROM task_runs WHERE uid = ?", (last,)).fetchone()[0] == 0
        assert {r[0] for r in replica.execute("SELECT row_uid FROM kanban_sync_outbox WHERE table_name = 'task_runs'")
                } == set(stuck)
    finally:
        replica.close()


def test_one_row_that_always_fails_to_apply_holds_back_only_its_own_op(live, kanban_home, svc, monkeypatch):
    """The page fails, so the ops are retried one at a time: the healthy card is restored, the bad one waits."""
    bad, _bad_run = _board_card_with_run(live, kanban_home, svc)
    good, good_run = _board_card_with_run(live, kanban_home, svc)
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    try:
        client.pull(replica, svc.hub(fixtures.ME), fixtures.BOARD, observers=False)
        bad_phantom = _contaminate(replica, bad)
        _contaminate(replica, good)
        real = sync.apply_remote_changes

        def refusing(conn, changes, **kwargs):
            if any(ch.uid == bad for ch in changes):
                raise sqlite3.IntegrityError("CHECK constraint failed")
            return real(conn, changes, **kwargs)

        monkeypatch.setattr(sync, "apply_remote_changes", refusing)
        assert pending.discard_replica_writes(replica, svc.hub(fixtures.ME), fixtures.BOARD) == 2
        assert _current_run_uid(replica, good) == good_run
        assert {r[0] for r in replica.execute("SELECT row_uid FROM kanban_sync_outbox")} == {bad, bad_phantom}
    finally:
        replica.close()


def test_the_feed_pull_runs_after_a_failed_undo(live, kanban_home, svc, monkeypatch):
    """The discard runs first in every pull: its failure must not stop the feed page after it."""
    tid, _run_uid = _board_card_with_run(live, kanban_home, svc)
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    try:
        _contaminate(replica, tid)

        def broken(*_a, **_k):
            raise ValueError("evidence reply is not a row")

        monkeypatch.setattr(pending, "_undo", broken)
        live.execute("UPDATE tasks SET title = 'after the undo failed' WHERE id = ?", (tid,))
        live.commit()
        client.sync_board(fixtures.BOARD, kanban_home / "kanban.db", svc.hub(fixtures.ME), fixtures.ME)
        client.pull(replica, svc.hub(fixtures.ME), fixtures.BOARD, observers=False)
        assert replica.execute("SELECT title FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "after the undo failed"
        assert replica.execute("SELECT COUNT(*) FROM kanban_sync_outbox").fetchone()[0] == 2  # kept for later
    finally:
        replica.close()


def test_a_malformed_board_row_leaves_the_feed_and_the_other_ops_alone(live, kanban_home, svc):
    """Building the restore page is part of the guarded undo: a Board row it cannot read fails only its own op."""
    bad, _bad_run = _board_card_with_run(live, kanban_home, svc)
    good, good_run = _board_card_with_run(live, kanban_home, svc)
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    try:
        client.pull(replica, svc.hub(fixtures.ME), fixtures.BOARD, observers=False)
        _contaminate(replica, bad)
        _contaminate(replica, good)

        class Malformed:
            def __init__(self, hub):
                self.hub = hub

            def row_evidence(self, board, table, uid):
                if (table, uid) == ("tasks", bad):
                    return {"state": "live", "row": {"title": "no uid"}}  # _row_change needs row["uid"]
                return self.hub.row_evidence(board, table, uid)

            def __getattr__(self, name):
                return getattr(self.hub, name)

        assert pending.discard_replica_writes(replica, Malformed(svc.hub(fixtures.ME)), fixtures.BOARD) == 2
        assert _current_run_uid(replica, good) == good_run
        bad_phantom = replica.execute("SELECT row_uid FROM kanban_sync_outbox WHERE table_name = 'task_runs'"
                                      ).fetchone()[0]
        assert {r[0] for r in replica.execute("SELECT row_uid FROM kanban_sync_outbox")} == {bad, bad_phantom}
    finally:
        replica.close()


def test_a_card_whose_undo_keeps_failing_stays_on_its_stray_run_unmasked(live, kanban_home, svc, monkeypatch):
    """A failed undo leaves its ops ``discarding`` and the card exactly as stray as before: still on its phantom run
    (never on no run at all) and no longer masked, so the Board's next version or divergence repair can restore it,
    the step the 2026-11-23 mask stopped. The next successful round heals it."""
    tid, run_uid = _board_card_with_run(live, kanban_home, svc)
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    try:
        phantom = _contaminate(replica, tid)

        def failing(*_a, **_k):
            raise sqlite3.OperationalError("disk I/O error")

        real = sync.apply_remote_changes
        monkeypatch.setattr(sync, "apply_remote_changes", failing)
        for _ in range(2):
            assert pending.discard_replica_writes(replica, svc.hub(fixtures.ME), fixtures.BOARD) == 0
        assert {r[0] for r in replica.execute("SELECT state FROM kanban_sync_outbox")} == {"discarding"}
        assert pending.pending_task_fields(replica) == {}
        assert _current_run_uid(replica, tid) == phantom
        monkeypatch.setattr(sync, "apply_remote_changes", real)
        assert pending.discard_replica_writes(replica, svc.hub(fixtures.ME), fixtures.BOARD) == 2
        assert _current_run_uid(replica, tid) == run_uid
    finally:
        replica.close()


def test_a_board_reply_that_is_not_an_object_is_a_failed_read(live, kanban_home, svc):
    """It holds back only the ops naming that row, like any failed read; the rest resolve."""
    bad, _bad_run = _board_card_with_run(live, kanban_home, svc)
    good, good_run = _board_card_with_run(live, kanban_home, svc)
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    try:
        client.pull(replica, svc.hub(fixtures.ME), fixtures.BOARD, observers=False)
        bad_phantom = _contaminate(replica, bad)
        _contaminate(replica, good)
        hub = svc.hub(fixtures.ME)

        class NotAnObject:
            def row_evidence(self, board, table, uid):
                return None if (table, uid) == ("tasks", bad) else hub.row_evidence(board, table, uid)

            def __getattr__(self, name):
                return getattr(hub, name)

        assert pending.discard_replica_writes(replica, NotAnObject(), fixtures.BOARD) == 2
        assert _current_run_uid(replica, good) == good_run
        assert {r[0] for r in replica.execute("SELECT row_uid FROM kanban_sync_outbox")} == {bad, bad_phantom}
        assert [r[0] for r in replica.execute("SELECT state FROM kanban_sync_outbox")] == ["pending", "pending"]
    finally:
        replica.close()


def test_a_withheld_op_a_failed_page_marked_goes_back_to_pending(live, kanban_home, svc, monkeypatch):
    """Withheld, it waits as it was: masking as before, not unmasked by a page attempt that failed earlier."""
    from hermes_cli import kanban_sync_quarantine as quarantine
    tid, _run_uid = _board_card_with_run(live, kanban_home, svc)
    replica = client.open_shadow(fixtures.BOARD, fixtures.ME)
    try:
        _contaminate(replica, tid)
        replica.execute("UPDATE kanban_sync_outbox SET state = 'discarding'")
        replica.commit()
        monkeypatch.setattr(quarantine, "certified_page", lambda *_a: [])
        assert pending.discard_replica_writes(replica, svc.hub(fixtures.ME), fixtures.BOARD) == 0
        assert [r[0] for r in replica.execute("SELECT state FROM kanban_sync_outbox")] == ["pending", "pending"]
    finally:
        replica.close()
