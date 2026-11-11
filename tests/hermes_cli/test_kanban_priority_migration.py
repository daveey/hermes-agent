"""One-shot normalization preserves rank/ties, records undo, and refuses stale writes."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_priority_migration as migration
from tests.hermes_cli import test_kanban_parent_first_dispatch as helpers

conn = helpers.conn


def legacy(conn, values):
    ids = [helpers._ready(conn, f"card {i}") for i in range(len(values))]
    for task, value in zip(ids, values):
        conn.execute("UPDATE tasks SET priority=? WHERE id=?", (value, task))
    conn.commit()
    return ids


def priorities(conn, ids):
    return [kb.get_task(conn, task).priority for task in ids]


def test_roundtrip_ranks_ties_headroom_and_idempotence(conn, tmp_path):
    values = [-10, 0, 10, 900, 900, 10000, 10250, 10300]
    ids = legacy(conn, values)
    terminal = helpers._ready(conn, "terminal")
    conn.execute("UPDATE tasks SET status='done', priority=99999 WHERE id=?", (terminal,))
    conn.commit()
    manifest = migration.plan(conn)
    path = tmp_path / "priorities.json"
    migration.save_manifest(path, manifest)
    with pytest.raises(FileExistsError):
        migration.save_manifest(path, manifest)
    assert migration.apply(conn, path) == 6
    assert priorities(conn, ids) == [-10, 0, 180, 360, 360, 540, 720, 900]
    assert kb.get_task(conn, terminal).priority == 99999
    assert migration.apply(conn, path) == 0
    with pytest.raises(ValueError, match="explicit legacy"):
        migration.apply(conn, path, undo=True)
    assert migration.apply(conn, path, undo=True, allow_legacy_rollback=True) == 6
    assert priorities(conn, ids) == values
    assert migration.apply(conn, path, undo=True, allow_legacy_rollback=True) == 0


def test_concurrent_priority_edit_refuses_whole_plan(conn, tmp_path):
    ids = legacy(conn, [10000, 10250])
    path = tmp_path / "priorities.json"
    migration.save_manifest(path, migration.plan(conn))
    kb.edit_task(conn, ids[1], priority=700)
    with pytest.raises(ValueError, match="changed since snapshot"):
        migration.apply(conn, path)
    assert priorities(conn, ids) == [10000, 700]


def test_empty_and_all_nonpositive(conn, tmp_path):
    path = tmp_path / "empty.json"
    migration.save_manifest(path, migration.plan(conn))
    assert migration.apply(conn, path) == 0
    ids = legacy(conn, [-10, 0])
    assert [r["new"] for r in migration.plan(conn)["rows"]] == priorities(conn, sorted(ids))
