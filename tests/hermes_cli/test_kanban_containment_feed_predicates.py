"""Real feed/snapshot entry points certify before exposing a containment row."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_containment_replica as replica
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_sync_client as client
from hermes_cli.kanban_db_sync import uuid7
from tests.hermes_cli import test_kanban_sync_capture as fixtures

kanban_home = fixtures.kanban_home
synced_conn = fixtures.synced_conn


def test_gateway_capture_never_publishes_server_owned_containment_marker(synced_conn):
    from hermes_cli import kanban_db_sync as sync
    conn = synced_conn
    sync.install_triggers(conn)
    assert "containment_episode" in sync.table_columns(conn, "tasks")
    assert "containment_episode" not in sync.captured_columns(conn, "tasks")
    task = kb.create_task(conn, title="Fixture capture boundary", assignee="default")
    fixtures._clear_outbox(conn)
    conn.execute("UPDATE tasks SET title='Fixture changed' WHERE id=?", (task,))
    ops = fixtures._ops(conn)
    assert len(ops) == 1 and ops[0]["fields"]["title"] == "Fixture changed"
    assert "containment_episode" not in ops[0]["fields"]


@pytest.mark.parametrize("snapshot", [False, True])
def test_real_feed_entrypoint_certifies_before_apply(synced_conn, monkeypatch, snapshot):
    conn = synced_conn
    task = kb.create_task(conn, title="Fixture certification boundary", assignee="default")
    fixtures._clear_outbox(conn)
    row = {"uid": task, "id": task, "status": "triage", "containment_episode": uuid7(), "version": 1}
    changes = [{"seq": 1, "table": "tasks", "uid": task, "op": "upsert", "fields": row,
                "origin_gateway": "fixture-home"}]
    calls = []

    class Hub:
        def changes(self, board, cursor):
            return {"changes": changes, "next": 1, "more": False}

        def snapshot_page(self, board, table, after):
            return {"rows": [row], "head": 1, "more": False}

    hub = Hub()

    def certify(actual_conn, actual_hub, board, page):
        assert actual_conn is conn and actual_hub is hub and board == "default"
        assert len(page) == 1 and page[0].fields["containment_episode"] == row["containment_episode"]
        calls.append("certify")

    def apply(*_a, **_k):
        assert calls == ["certify"]
        calls.append("apply")

    monkeypatch.setattr(replica, "certify_page", certify)
    monkeypatch.setattr(client.sync, "apply_remote_changes", apply)
    monkeypatch.setattr(client, "IMPORT_ORDER", ["tasks"])
    if snapshot:
        client.resync(conn, hub, "default", observers=False)
    else:
        client.pull(conn, hub, "default", observers=False)
    assert calls == ["certify", "apply"]
