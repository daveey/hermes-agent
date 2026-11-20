"""A refused uid must not block unrelated rows on the same board.

Uses real receipt certification, real pull/apply and the sync_all_once catch boundary;
only the enrolled Hub and per-board file selection are fixture transports.
"""
from __future__ import annotations

import copy

from hermes_cli import kanban_containment_replica as replica
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_refusal as refusal
from tests.hermes_cli import test_kanban_containment_replica as fixtures

kanban_home = fixtures.kanban_home
synced_conn = fixtures.synced_conn


def test_changed_receipt_does_not_block_same_board_rows(synced_conn, monkeypatch, tmp_path):
    conn = synced_conn
    target, _, stop, _, hub = fixtures.scenario(conn)
    other = kb.create_task(conn, title="Unrelated original", assignee="default")
    conn.execute("DELETE FROM kanban_sync_outbox")
    uid = stop["event_uid"]
    proof = hub.events[uid]
    replica.remember(conn, proof, board="fixture", task=target, uid=uid)
    original = tuple(conn.execute("SELECT * FROM containment_canonical_receipts WHERE uid=?", (uid,)).fetchone())
    altered = copy.deepcopy(proof)
    altered["row"]["created_at"] += 1
    hub.events[uid] = altered
    refused = fixtures.event(stop, hub, 1)
    healthy = sync.RemoteChange(2, "tasks", other, "upsert", {"title": "Unrelated synced"}, 200, "", "worker")
    changes = [refused, healthy]
    page = {"changes": changes, "next": 2, "more": False}
    monkeypatch.setattr(ksc.acw, "changes_page", lambda *args: page)
    monkeypatch.setattr(ksc, "adapt", lambda change: change)
    monkeypatch.setattr(ksc, "publish_pickup_if_changed", lambda hub: None)
    from hermes_cli import kanban_source_clones
    monkeypatch.setattr(kanban_source_clones, "attest_if_due", lambda hub: None)

    def round_for_fixture(slug, path, transport, gateway):
        pulled = ksc.pull(conn, transport, slug, observers=False)
        return ksc.RoundResult(board=slug, mode="primary", pulled=pulled)

    monkeypatch.setattr(ksc, "sync_board", round_for_fixture)
    refusal.reset()
    try:
        # Before quarantine, this returns a board error without applying the healthy
        # row: #3759 proves loop survival, but not same-board forward progress.
        results = ksc.sync_all_once(hub, boards=[("fixture", tmp_path / "fixture.db")])
        assert len(results) == 1
        assert kb.get_task(conn, other).title == "Unrelated synced"
        assert sync.sync_state(conn)["cursor"] == 2
        assert tuple(conn.execute("SELECT * FROM containment_canonical_receipts WHERE uid=?", (uid,)).fetchone()) == original
        assert conn.execute("SELECT 1 FROM task_events WHERE uid=?", (uid,)).fetchone() is None
    finally:
        refusal.reset()
