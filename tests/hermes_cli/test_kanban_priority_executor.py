"""Only a non-worker supervisor may apply an attached global plan on its origin."""
import hashlib
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_priority_migration as migration
from tests.hermes_cli import test_kanban_priority_migration as helpers
from tests.hermes_cli import test_kanban_sync_client as sync_helpers

kanban_home = sync_helpers.kanban_home
conn = sync_helpers.live


def setup_plan(conn):
    conn.execute("UPDATE kanban_sync_state SET mode='primary'")
    conn.commit()
    ids = helpers.legacy(conn, [10000, 10000, 50000])
    for task, origin in zip(ids, [sync_helpers.ME, "two", sync_helpers.ME]):
        conn.execute("UPDATE tasks SET created_on=? WHERE id=?", (origin, task))
    conn.commit()
    rows = [dict(r) for r in conn.execute("SELECT id,priority,created_on,status FROM tasks")]
    manifest = migration.plan_global(rows, board_id=sync_helpers.BOARD, exceptions=[])
    data = json.dumps(manifest).encode()
    attachment = kb.store_attachment_bytes(conn, ids[0], "global.json", data, content_type="application/json")
    conn.execute("DELETE FROM kanban_sync_outbox")
    conn.commit()
    return ids, attachment, hashlib.sha256(data).hexdigest()


def test_executor_writes_only_origin_rows_and_captures_normal_outbox(conn):
    ids, attachment, digest = setup_plan(conn)
    from hermes_cli import kanban_priority_executor as executor
    result = executor.apply_attached(conn, attachment, digest, writer=sync_helpers.ME)
    assert result["changed"] == 2
    assert helpers.priorities(conn, ids) == [450, 10000, 900]
    assert executor.apply_attached(conn, attachment, digest, writer=sync_helpers.ME)["changed"] == 0
    outbox = [dict(r) for r in conn.execute("SELECT * FROM kanban_sync_outbox")]
    assert outbox
    assert ids[1] not in json.dumps(outbox)


@pytest.mark.parametrize("case, reason", [
    ("missing", "attached manifest required"), ("digest", "digest mismatch"),
    ("writer", "different writer"), ("shadow", "primary writer"),
    ("board", "different board"), ("priority", "priorities changed"),
    ("ownership", "ownership or membership changed"),
    ("terminal", "ownership or membership changed"),
    ("new", "ownership or membership changed"),
])
def test_executor_refuses_whole_origin_atomically(conn, case, reason):
    from hermes_cli import kanban_priority_executor as executor
    ids, attachment, digest = setup_plan(conn)
    writer = sync_helpers.ME
    if case == "missing":
        attachment = -1
    elif case == "digest":
        digest = "0" * 64
    elif case == "writer":
        writer = "two"
    elif case == "shadow":
        conn.execute("UPDATE kanban_sync_state SET mode='shadow'")
    elif case == "board":
        conn.execute("UPDATE kanban_sync_state SET board_id='another'")
    elif case == "priority":
        conn.execute("UPDATE tasks SET priority=777 WHERE id=?", (ids[2],))
    elif case == "ownership":
        conn.execute("UPDATE tasks SET created_on='two' WHERE id=?", (ids[2],))
    elif case == "terminal":
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (ids[2],))
    elif case == "new":
        extra = helpers.legacy(conn, [10000])[0]
        conn.execute("UPDATE tasks SET created_on=? WHERE id=?", (writer, extra))
    conn.commit()
    before = helpers.priorities(conn, ids)
    outbox_before = [tuple(r) for r in conn.execute("SELECT * FROM kanban_sync_outbox")]
    with pytest.raises(ValueError, match=reason):
        executor.apply_attached(conn, attachment, digest, writer=writer)
    assert helpers.priorities(conn, ids) == before
    assert [tuple(r) for r in conn.execute("SELECT * FROM kanban_sync_outbox")] == outbox_before


@pytest.mark.parametrize("context", ["worker", "descendant"])
def test_worker_boundary_is_not_an_executor(conn, monkeypatch, tmp_path, context):
    from pathlib import Path
    from hermes_cli import kanban_priority_executor as executor
    from agent import delegation_context
    ids, attachment, digest = setup_plan(conn)
    old_path = tmp_path / "legacy.json"
    migration.save_manifest(old_path, migration.plan(conn))
    before = helpers.priorities(conn, ids)
    if context == "worker":
        monkeypatch.setenv("HERMES_KANBAN_TASK", ids[0])
    else:
        monkeypatch.setenv(delegation_context.DELEGATED_CHILD_ENV_MARKER, "1")
        monkeypatch.setenv(delegation_context.DELEGATED_CHILD_OWNER_ENV, delegation_context._owner_token())
        # The original low-level API remains fenced too. No mocked write fence.
        with pytest.raises(PermissionError, match="cannot mutate"):
            migration.apply(conn, Path(old_path))
    with pytest.raises(PermissionError, match="cannot execute"):
        executor.apply_attached(conn, attachment, digest, writer=sync_helpers.ME)
    assert helpers.priorities(conn, ids) == before
