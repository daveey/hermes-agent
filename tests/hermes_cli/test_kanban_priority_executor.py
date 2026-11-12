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
    ("foreign_attachment", "attachment owner"),
    ("writer", "different writer"), ("shadow", "primary writer"),
    ("board", "different board"), ("priority", "priorities changed"),
    ("ownership", "ownership or membership changed"),
    ("terminal", "ownership or membership changed"),
    ("new", "ownership or membership changed"),
])
@pytest.mark.parametrize("undo", [False, True])
def test_executor_refuses_whole_origin_atomically(conn, case, reason, undo):
    from hermes_cli import kanban_priority_executor as executor
    ids, attachment, digest = setup_plan(conn)
    if undo:
        executor.apply_attached(conn, attachment, digest, writer=sync_helpers.ME)
    writer = sync_helpers.ME
    sql_mutations = {
        "shadow": ("UPDATE kanban_sync_state SET mode='shadow'", ()),
        "board": ("UPDATE kanban_sync_state SET board_id='another'", ()),
        "priority": ("UPDATE tasks SET priority=777 WHERE id=?", (ids[2],)),
        "ownership": ("UPDATE tasks SET created_on='two' WHERE id=?", (ids[2],)),
        "terminal": ("UPDATE tasks SET status='done' WHERE id=?", (ids[2],)),
    }
    if case in sql_mutations:
        conn.execute(*sql_mutations[case])
    if case == "missing":
        attachment = -1
    if case == "foreign_attachment":
        from pathlib import Path
        stored = kb.get_attachment(conn, attachment)
        assert stored is not None
        attachment = kb.store_attachment_bytes(conn, ids[1], "foreign.json", Path(stored.stored_path).read_bytes())
    if case == "digest":
        digest = "0" * 64
    if case == "writer":
        writer = "two"
    if case == "new":
        extra = helpers.legacy(conn, [10000])[0]
        conn.execute("UPDATE tasks SET created_on=? WHERE id=?", (writer, extra))
    conn.commit()
    before = helpers.priorities(conn, ids)
    outbox_before = [tuple(r) for r in conn.execute("SELECT * FROM kanban_sync_outbox")]
    with pytest.raises(ValueError, match=reason):
        executor.apply_attached(conn, attachment, digest, writer=writer, undo=undo, allow_legacy_rollback=True)
    assert helpers.priorities(conn, ids) == before
    assert [tuple(r) for r in conn.execute("SELECT * FROM kanban_sync_outbox")] == outbox_before


@pytest.mark.parametrize("mutation", ["rank", "version", "boolean", "duplicate", "exception", "origin", "ceiling", "extra"])
def test_attached_manifest_is_validated_not_trusted(conn, mutation):
    from pathlib import Path
    from hermes_cli import kanban_priority_executor as executor
    ids, attachment, _ = setup_plan(conn)
    stored = kb.get_attachment(conn, attachment)
    assert stored is not None
    manifest = json.loads(Path(stored.stored_path).read_bytes())
    mutations = {
        "rank": lambda: manifest["rows"][0].update(new=123),
        "version": lambda: manifest.update(version=1),
        "boolean": lambda: manifest["rows"][0].update(old=True),
        "duplicate": lambda: manifest["rows"].append(manifest["rows"][0]),
        "exception": lambda: manifest.update(exceptions=[{"id": "unknown", "status": "running", "origin": "two", "old": 1}]),
        "origin": lambda: manifest["rows"][0].update(origin=None),
        "ceiling": lambda: manifest.update(ceiling=999),
        "extra": lambda: manifest.update(arbitrary="ignored?"),
    }
    mutations[mutation]()
    data = json.dumps(manifest).encode()
    attachment = kb.store_attachment_bytes(conn, ids[0], "invalid.json", data, content_type="application/json")
    before = helpers.priorities(conn, ids)
    with pytest.raises(ValueError, match="manifest"):
        executor.apply_attached(conn, attachment, hashlib.sha256(data).hexdigest(), writer=sync_helpers.ME)
    assert helpers.priorities(conn, ids) == before


def test_restore_requires_explicit_policy_rollback_and_cas(conn):
    from hermes_cli import kanban_priority_executor as executor
    ids, attachment, digest = setup_plan(conn)
    executor.apply_attached(conn, attachment, digest, writer=sync_helpers.ME)
    with pytest.raises(ValueError, match="explicit legacy policy rollback"):
        executor.apply_attached(conn, attachment, digest, writer=sync_helpers.ME, undo=True)
    assert helpers.priorities(conn, ids) == [450, 10000, 900]
    result = executor.apply_attached(conn, attachment, digest, writer=sync_helpers.ME,
                                     undo=True, allow_legacy_rollback=True)
    assert result["changed"] == 2
    assert helpers.priorities(conn, ids) == [10000, 10000, 50000]
    assert executor.apply_attached(conn, attachment, digest, writer=sync_helpers.ME,
                                   undo=True, allow_legacy_rollback=True)["changed"] == 0
    conn.execute("UPDATE tasks SET priority=123 WHERE id=?", (ids[0],))
    conn.commit()
    with pytest.raises(ValueError, match="priorities changed"):
        executor.apply_attached(conn, attachment, digest, writer=sync_helpers.ME,
                                undo=True, allow_legacy_rollback=True)
    assert helpers.priorities(conn, ids) == [123, 10000, 50000]


def test_unknown_live_origin_refuses_normalization(conn):
    from hermes_cli import kanban_priority_executor as executor
    ids, attachment, digest = setup_plan(conn)
    conn.execute("UPDATE tasks SET created_on=NULL WHERE id=?", (ids[1],))
    conn.commit()
    with pytest.raises(ValueError, match="missing origin"):
        executor.apply_attached(conn, attachment, digest, writer=sync_helpers.ME)
    assert helpers.priorities(conn, ids) == [10000, 10000, 50000]


def test_two_origins_share_ranks_and_publish_only_their_own_rows(conn, tmp_path):
    from hermes_cli import kanban_priority_executor as executor
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_sync_client as ksc
    from tests.hermes_cli.kanban_sync_fake_board import FakeService
    ids, attachment, digest = setup_plan(conn)
    svc = FakeService()
    for task, origin, priority in zip(ids, [sync_helpers.ME, "two", sync_helpers.ME], [10000, 10000, 50000]):
        svc.hub(origin).ops(sync_helpers.BOARD, [{
            "client_op_id": f"seed-{task}", "op": "upsert", "table": "tasks", "uid": task,
            "hlc": f"0000000000001-0000-{origin}",
            "fields": {"title": task, "status": "ready", "priority": priority, "created_at": 1},
        }])
    other = kbc.connect(tmp_path / "other-origin.db")
    try:
        conn.backup(other)
        other.execute("UPDATE kanban_sync_state SET gateway_id='two'")
        other.commit()
        assert executor.apply_attached(conn, attachment, digest, writer=sync_helpers.ME)["changed"] == 2
        from pathlib import Path
        stored = kb.get_attachment(conn, attachment)
        assert stored is not None
        other_attachment = kb.store_attachment_bytes(other, ids[1], "origin-two.json", Path(stored.stored_path).read_bytes())
        assert executor.apply_attached(other, other_attachment, digest, writer="two")["changed"] == 1
        assert helpers.priorities(other, ids) == [10000, 450, 50000]
        for db, origin, own in [(conn, sync_helpers.ME, {ids[0], ids[2]}), (other, "two", {ids[1]})]:
            task_ops = {r[0] for r in db.execute("SELECT row_uid FROM kanban_sync_outbox WHERE table_name='tasks'")}
            assert task_ops == own
            result = ksc.push(db, svc.hub(origin), sync_helpers.BOARD, origin, primary=True)
            assert result.rejected == 0 and result.sent > 0
            assert not sync_helpers._pending(db)
        canonical = svc.hub(sync_helpers.ME).snapshot_page(sync_helpers.BOARD, "tasks")
        values = {r["id"]: r["priority"] for r in canonical["rows"]}
        assert [values[task] for task in ids] == [450, 450, 900]
    finally:
        other.close()


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
