"""Public CLI and sync boundaries for bounded priorities."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_priority_migration as migration
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli import test_kanban_edit_priority_lower as cli

kanban_home = cli.kanban_home


@pytest.fixture
def synced(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    yield from support.synced_conn(home)


def test_cli_relative_bump_and_audit(kanban_home):
    task = cli._card(990)
    assert cli._main(["edit", task, "--priority-bump", "15", "--reason", "below lane median"]) == 0
    assert cli._priority(task) == 1000
    assert cli._reprioritized(task)[-1] == {
        "priority": 1000, "previous_priority": 990, "relative_bump": 15,
        "applied_delta": 10, "reason": "below lane median",
    }
    assert cli._main(["edit", task, "--priority-bump", "0", "--title", "invalid"]) == 2
    assert cli._main(["edit", task, "--priority-bump", "10", "--priority", "900"]) == 2


def test_sync_import_clamps_without_echo_loop(synced):
    sync.set_identity(synced, "board", "local")
    change = sync.RemoteChange(seq=1, table="tasks", uid="t_remote", op="upsert", fields={
        "title": "legacy", "status": "ready", "assignee": "default", "priority": 10300,
        "created_at": 10, "created_on": "remote",
    }, version=1, hlc="1", origin_gateway="remote")
    before = synced.execute("SELECT count(*) FROM kanban_sync_outbox").fetchone()[0]
    for _ in range(2):
        sync.apply_remote_changes(synced, [change], new_cursor=1, observers=False)
        assert kb.get_task(synced, "t_remote").priority == 1000
        assert synced.execute("SELECT count(*) FROM kanban_sync_outbox").fetchone()[0] == before


def test_migration_refuses_other_writer_shadow_and_foreign(synced, tmp_path):
    sync.set_identity(synced, "board", "local")
    task = kb.create_task(synced, title="local", priority=500)
    synced.execute("UPDATE tasks SET created_on='local' WHERE id=?", (task,))
    synced.commit()
    path = tmp_path / "manifest.json"
    migration.save_manifest(path, migration.plan(synced))
    sync.set_identity(synced, "board", "different")
    with pytest.raises(ValueError, match="different writer"):
        migration.apply(synced, path)
    sync.set_identity(synced, "board", "local")
    sync.set_mode(synced, "primary")
    path2 = tmp_path / "primary.json"
    migration.save_manifest(path2, migration.plan(synced))
    synced.execute("UPDATE tasks SET created_on='foreign' WHERE id=?", (task,))
    synced.commit()
    with pytest.raises(ValueError, match="foreign-owned"):
        migration.apply(synced, path2)
    assert kb.get_task(synced, task).priority == 500
