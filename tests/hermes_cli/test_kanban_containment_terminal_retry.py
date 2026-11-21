"""An authenticated projection is not proof that the caller settled its delivery."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_containment_replica as replica
from tests.hermes_cli import test_kanban_containment_terminal_remote as tr

kanban_home = tr.kanban_home
synced_conn = tr.synced_conn


@pytest.mark.parametrize("page", ["events-and-task", "events-only"])
def test_lost_reply_retry_still_settles_after_ordinary_sync_projected_the_archive(synced_conn, monkeypatch, page):
    conn = synced_conn
    hub = tr.setup(conn, monkeypatch)
    hub.fail = True
    with pytest.raises(TimeoutError):
        kb.archive_task(conn, hub.target, comment=("me", "why"))
    hub.fail = False
    # An ordinary pull lands the Board's archive before the caller retries.
    rows = [hub.source.events[hub.close_event["event_uid"]]["row"], hub.audit]
    changes = [client._row_change("task_events", dict(r)) for r in rows]
    if page == "events-and-task":
        changes.append(client._row_change("tasks", {**hub.task, "uid": hub.target}))
    replica.certify_page(conn, hub, "fixture", changes)
    sync.apply_remote_changes(conn, changes, new_cursor=sync.sync_state(conn)["cursor"], board="fixture")
    assert kb.get_task(conn, hub.target).status == "archived"
    # The request this caller staged is still unresolved: the retry must finish it, once.
    assert kb.archive_task(conn, hub.target, comment=("me", "why")) is True
    assert [r[0] for r in conn.execute("SELECT confirmed FROM containment_delivery")] == [1]
    assert conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id=?", (hub.target,)).fetchone()[0] == 1
    # And only then is a further repeat the settled no-op.
    assert kb.archive_task(conn, hub.target, comment=("me", "why")) is False
    assert conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id=?", (hub.target,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='archived'", (hub.target,)).fetchone()[0] == 1
    assert len(hub.posts) == 2
    assert hub.posts[0] == hub.posts[1]


def test_unknown_stop_prevents_settled_noop(synced_conn, monkeypatch):
    from hermes_cli.kanban_containment_terminal import closed
    from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
    hub = tr.setup(synced_conn, monkeypatch)
    assert kb.archive_task(synced_conn, hub.target)
    state = sync.sync_state(synced_conn)
    unknown = sync.uuid7()
    synced_conn.execute(
        "INSERT INTO containment_delivery(request_uid,board,gateway,op_json,confirmed) VALUES (?,?,?,?,0)",
        (unknown, state["board_id"], state["gateway_id"],
         json.dumps({"op": "contain", "task": hub.target, "event_uid": unknown})),
    )
    synced_conn.commit()
    assert not closed(synced_conn, hub.target)
    assert kb.get_task(synced_conn, hub.target).status == "archived"
    with pytest.raises(ContainmentRefused, match="was not projected locally"):
        kb.archive_task(synced_conn, hub.target, comment=("me", "must not write"))
    assert not synced_conn.execute("SELECT 1 FROM task_comments WHERE task_id=?", (hub.target,)).fetchone()


@pytest.mark.parametrize("other", ["board", "gateway", "task"])
def test_unrelated_pending_archive_does_not_replay_settled_request(synced_conn, monkeypatch, other):
    hub = tr.setup(synced_conn, monkeypatch)
    assert kb.archive_task(synced_conn, hub.target, comment=("me", "why"))
    state = sync.sync_state(synced_conn)
    uid = sync.uuid7()
    synced_conn.execute(
        "INSERT INTO containment_delivery(request_uid,board,gateway,op_json,confirmed) VALUES (?,?,?,?,0)",
        (uid, "other" if other == "board" else state["board_id"],
         "other" if other == "gateway" else state["gateway_id"],
         json.dumps({"op": "archive", "task": hub.child if other == "task" else hub.target, "client_op_id": uid})),
    )
    synced_conn.commit()
    before = synced_conn.total_changes
    assert kb.archive_task(synced_conn, hub.target, comment=("me", "why"), via="different") is False
    assert synced_conn.total_changes == before
    assert len(hub.posts) == 1
    assert synced_conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id=?", (hub.target,)).fetchone()[0] == 1
