"""A snapshot rebuild keeps a finished card finished after replaying its containment history. [daveey]

Fleet incident 2026-11-23 (see ``hermes_cli/kanban_sync_snapshot_tasks.py``): the ``task_events`` pass of a rebuild
replayed a card's old ``contained`` / ``containment_released`` pair and left the done card ``ready`` at the Board's
version, so new workers kept retrying a card the Board refused as ``not_claimable``.
"""
from __future__ import annotations

import copy

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_snapshot_tasks as snapshot_tasks
from tests.hermes_cli import test_kanban_containment_replica as fixture

kanban_home = fixture.kanban_home
synced_conn = fixture.synced_conn

DONE_AT = 1_795_196_446


class SnapshotHub:
    """The containment proofs of ``fixture.scenario`` plus a Board snapshot served one row per page, as a real
    board's history is: a card's ``contained`` and its ``containment_released`` sit pages apart, so the first page
    holds the card and a later one releases it (on one page they cancel out and nothing is reprojected)."""

    def __init__(self, proofs, tables):
        self.proofs, self.tables, self.reads = proofs, tables, []

    def row_evidence(self, board, table, uid):
        return self.proofs.row_evidence(board, table, uid)

    def snapshot_page(self, board, table, after="", limit=ksc.SNAPSHOT_PAGE):
        assert board == "fixture"
        self.reads.append(table)
        rows, index = self.tables.get(table, []), int(after or 0)
        more = index + 1 < len(rows)
        return {"head": 50, "rows": copy.deepcopy(rows[index:index + 1]), "more": more,
                **({"next": index + 1} if more else {})}


def _board(conn, target, stop, release, hub, *, version=40):
    """The Board's copy: the card finished after its containment was released."""
    task = {"uid": target, "id": target, "status": "done", "completed_at": DONE_AT, "containment_episode": None,
            "version": version}
    events = [dict(hub.events[op["event_uid"]]["row"], version=i + 1) for i, op in enumerate((stop, release))]
    return {"tasks": [task], "task_events": events}


def _rebuild(conn, hub):
    """``resync`` over the snapshot (the card row exists already: a snapshot task row is an upsert either way)."""
    return ksc.resync(conn, hub, "fixture", observers=False)


def _reopen(conn, tid):
    """What the history replay leaves behind, written the way the replay writes it (nothing queued to send)."""
    conn.execute("UPDATE tasks SET status = 'ready', completed_at = NULL WHERE id = ?", (tid,))
    conn.commit()
    fixture.capture._clear_outbox(conn)


def _card(conn, tid):
    row = conn.execute("SELECT status, completed_at, sync_version FROM tasks WHERE id = ?", (tid,)).fetchone()
    return tuple(row) if row else None


def test_rebuild_keeps_a_released_card_done(synced_conn, monkeypatch):
    target, _, stop, release, proofs = fixture.scenario(synced_conn)
    hub = SnapshotHub(proofs, _board(synced_conn, target, stop, release, proofs))
    _rebuild(synced_conn, hub)
    assert _card(synced_conn, target) == ("done", DONE_AT, 40)
    assert hub.reads[-1] == "tasks"  # the re-read comes after every child table


def test_without_the_reassert_the_history_replay_reopens_the_card(synced_conn, monkeypatch):
    """The failure the reassert removes: the history pass alone leaves the done card ``ready`` at its version."""
    target, _, stop, release, proofs = fixture.scenario(synced_conn)
    hub = SnapshotHub(proofs, _board(synced_conn, target, stop, release, proofs))
    monkeypatch.setattr(snapshot_tasks, "reassert", lambda *a, **k: 0)
    _rebuild(synced_conn, hub)
    assert _card(synced_conn, target) == ("ready", None, 40)


def test_reassert_skips_a_row_the_board_moved_past(synced_conn):
    """A card at another version than the re-read snapshot row is left for the feed."""
    target, _, stop, release, proofs = fixture.scenario(synced_conn)
    hub = SnapshotHub(proofs, _board(synced_conn, target, stop, release, proofs))
    _rebuild(synced_conn, hub)
    _reopen(synced_conn, target)
    hub.tables["tasks"][0]["version"] = 41  # the Board wrote the card again after the first pass
    assert snapshot_tasks.reassert(synced_conn, hub, "fixture") == 0
    assert _card(synced_conn, target) == ("ready", None, 40)
    hub.tables["tasks"][0]["version"] = 40
    assert snapshot_tasks.reassert(synced_conn, hub, "fixture") == 1
    assert _card(synced_conn, target) == ("done", DONE_AT, 40)


def test_reassert_does_not_move_the_cursor(synced_conn):
    target, _, stop, release, proofs = fixture.scenario(synced_conn)
    hub = SnapshotHub(proofs, _board(synced_conn, target, stop, release, proofs))
    _rebuild(synced_conn, hub)
    synced_conn.execute("UPDATE kanban_sync_state SET cursor = 77")
    _reopen(synced_conn, target)
    assert snapshot_tasks.reassert(synced_conn, hub, "fixture") == 1
    assert synced_conn.execute("SELECT cursor FROM kanban_sync_state").fetchone()[0] == 77
    assert kb.get_task(synced_conn, target).status == "done"


def test_reassert_never_adds_a_card_the_rebuild_did_not_write(synced_conn):
    """A snapshot card missing here (another gateway's, outside a live file's partition) stays missing."""
    target, _, stop, release, proofs = fixture.scenario(synced_conn)
    hub = SnapshotHub(proofs, _board(synced_conn, target, stop, release, proofs))
    _rebuild(synced_conn, hub)
    _reopen(synced_conn, target)
    hub.tables["tasks"].append({"uid": "t_foreign", "id": "t_foreign", "status": "done", "created_on": "elsewhere",
                                "version": 9})
    assert snapshot_tasks.reassert(synced_conn, hub, "fixture", partition="worker") == 1
    assert _card(synced_conn, "t_foreign") is None
    assert _card(synced_conn, target) == ("done", DONE_AT, 40)


def test_reassert_skips_rows_that_already_match(synced_conn):
    """An untouched card at the Board version is not written again (most of a real board)."""
    target, _, stop, release, proofs = fixture.scenario(synced_conn)
    hub = SnapshotHub(proofs, _board(synced_conn, target, stop, release, proofs))
    _rebuild(synced_conn, hub)
    assert _card(synced_conn, target) == ("done", DONE_AT, 40)
    assert snapshot_tasks.reassert(synced_conn, hub, "fixture") == 0
