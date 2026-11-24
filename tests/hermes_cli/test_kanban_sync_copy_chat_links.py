"""A picked-up foreign card retains this gateway's authoritative chat links."""

import pytest

from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from hermes_cli.kanban_db_connect import write_txn
from hermes_cli.kanban_sync_partition_repair import repair_partition_chat_links
from tests.hermes_cli.test_kanban_cross_gateway_parents import (
    BOARD, ME, _other, _round, kanban_home, live, svc,
)
from tests.hermes_cli.test_kanban_sync_foreign_chat_links import _diff, _foreign_copy

__all__ = ["kanban_home", "live", "svc"]


def test_round_recovers_own_chat_links_consumed_before_foreign_pickup(live, svc, kanban_home):
    _foreign_copy(live, svc, kanban_home)
    uid = f"t_foreign:{ME}:default:old-chat:shepherd"
    _other(svc, "own-chat", "task_chat_links", uid,
           {"task_id": "t_foreign", "chat_gateway": ME, "chat_profile": "default",
            "chat_session": "old-chat", "kind": "shepherd", "released": 1, "created_at": 2})
    # Consume the feed while the foreign card is absent, then pick it up again.
    from hermes_cli import kanban_worker_node as wn
    with write_txn(live):
        live.execute("UPDATE kanban_sync_state SET applying = 1")
        live.execute("UPDATE tasks SET status = 'done' WHERE id = 't_foreign'")
        live.execute("UPDATE kanban_sync_state SET applying = 0")
    assert wn.evict(live) == ["t_foreign"]
    _round(kanban_home, svc)
    replica = ksc.open_shadow(BOARD, ME)
    try:
        assert wn.import_cards(live, replica, ["t_foreign"]) == ["t_foreign"]
    finally:
        replica.close()
    assert _diff(live)["child_mismatch"] == {"task_chat_links": 1}
    outbox_before = [tuple(r) for r in live.execute("SELECT * FROM kanban_sync_outbox")]
    _round(kanban_home, svc)
    row = live.execute("SELECT released, chat_profile FROM task_chat_links WHERE uid=?", (uid,)).fetchone()
    assert row is not None
    assert tuple(row) == (1, "default")
    assert _diff(live)["child_mismatch"] == {}
    assert [tuple(r) for r in live.execute("SELECT * FROM kanban_sync_outbox")] == outbox_before
    _round(kanban_home, svc)
    assert live.execute("SELECT COUNT(*) FROM task_chat_links WHERE uid=?", (uid,)).fetchone()[0] == 1


@pytest.mark.parametrize("barrier", ["pending-link", "rejected-link", "pending-task", "rejected-task",
                                     "live-link-tombstone", "shadow-link-tombstone", "live-task-tombstone",
                                     "shadow-task-tombstone", "existing", "placeholder", "shadow-mode",
                                     "absent-task", "own-task", "other-chat"])
def test_repair_preserves_scope_edits_deletions_and_audits(live, svc, kanban_home, barrier):
    _foreign_copy(live, svc, kanban_home)
    uid = f"t_foreign:{ME}:default:old-chat:shepherd"
    replica = ksc.open_shadow(BOARD, ME)
    try:
        session = "kanban-placement:test" if barrier == "placeholder" else "old-chat"
        gateway = "elsewhere" if barrier == "other-chat" else ME
        with write_txn(replica):
            replica.execute(
                "INSERT INTO task_chat_links (uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at) "
                "VALUES (?, 't_foreign', ?, 'default', ?, 'shepherd', 1, 2)", (uid, gateway, session))
        if barrier.startswith(("pending-", "rejected-")):
            state, target = barrier.split("-")
            table, row_uid = ("tasks", "t_foreign") if target == "task" else ("task_chat_links", uid)
            with write_txn(live):
                live.execute("INSERT INTO kanban_sync_outbox "
                             "(op_id,table_name,row_uid,op,fields,hlc,created_at,state,last_error) "
                             "VALUES ('held',?,?, 'delete','{}','',1,?,'preserve audit')", (table,row_uid,state))
        elif barrier.endswith("tombstone"):
            side, target, _ = barrier.split("-")
            conn = live if side == "live" else replica
            table, row_uid = ("tasks", "t_foreign") if target == "task" else ("task_chat_links", uid)
            with write_txn(conn):
                conn.execute("UPDATE kanban_sync_state SET applying = 1")  # a replica tombstone is a pulled one
                conn.execute("INSERT INTO kanban_sync_tombstones (table_name,row_uid,deleted_at) VALUES (?,?,1)",
                             (table,row_uid))
                conn.execute("UPDATE kanban_sync_state SET applying = 0")
        elif barrier == "existing":
            with write_txn(live):
                live.execute("INSERT INTO task_chat_links (uid,task_id,chat_gateway,chat_session,kind,released,created_at) "
                             "VALUES (?, 't_foreign', ?, 'old-chat', 'shepherd', 0, 3)", (uid,ME))
        elif barrier == "shadow-mode":
            sync.set_mode(live, "shadow")
        elif barrier in ("absent-task", "own-task"):
            with write_txn(live):
                live.execute("UPDATE kanban_sync_state SET applying=1")
                if barrier == "absent-task":
                    live.execute("DELETE FROM tasks WHERE id='t_foreign'")
                else:
                    live.execute("UPDATE tasks SET created_on=? WHERE id='t_foreign'", (ME,))
                live.execute("UPDATE kanban_sync_state SET applying=0")
        tables = ("tasks", "task_chat_links", "kanban_sync_outbox", "kanban_sync_tombstones", "kanban_sync_state")
        before = {t: [tuple(r) for r in live.execute(f"SELECT * FROM {t}")] for t in tables}
        assert repair_partition_chat_links(live, replica, ME) == []
        assert {t: [tuple(r) for r in live.execute(f"SELECT * FROM {t}")] for t in tables} == before
    finally:
        replica.close()


def test_delete_between_replica_and_live_pulls_does_not_resurrect_link(live, svc, kanban_home, monkeypatch):
    _foreign_copy(live, svc, kanban_home)
    uid = f"t_foreign:{ME}:default:racing-chat:shepherd"
    _other(svc, "racing-chat", "task_chat_links", uid,
           {"task_id": "t_foreign", "chat_gateway": ME, "chat_profile": "default",
            "chat_session": "racing-chat", "kind": "shepherd", "released": 1, "created_at": 2})
    # The live cursor already consumed this link while the card was absent.
    ksc._pull_replica(BOARD, svc.hub(ME), ME)
    replica = ksc.open_shadow(BOARD, ME)
    try:
        cursor = sync.sync_state(replica)["cursor"]
    finally:
        replica.close()
    with write_txn(live):
        live.execute("UPDATE kanban_sync_state SET cursor=?", (cursor,))
    original = ksc.pull
    deleted = False

    def racing_pull(conn, hub, board, **kwargs):
        nonlocal deleted
        if kwargs.get("partition") == ME and not deleted:
            result = hub.ops(board, [{"client_op_id": "racing-delete", "op": "delete",
                                     "table": "task_chat_links", "uid": uid, "fields": {}}])
            assert result["results"][0]["status"] == "applied"
            deleted = True
        return original(conn, hub, board, **kwargs)

    monkeypatch.setattr(ksc, "pull", racing_pull)
    for _ in range(3):
        _round(kanban_home, svc)
        assert live.execute("SELECT 1 FROM task_chat_links WHERE uid=?", (uid,)).fetchone() is None
    assert deleted
    assert _diff(live)["clean"]
