"""Canonical raw capture and immutable attachment staging (never cap-import)."""
import hashlib
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_priority_migration as migration
from tests.hermes_cli import test_kanban_priority_executor as helpers
from tests.hermes_cli import test_kanban_sync_client as sync_helpers

kanban_home = sync_helpers.kanban_home
conn = sync_helpers.live


class Pages:
    def __init__(self, changed=False, stalled=False):
        self.changed = changed
        self.stalled = stalled

    def snapshot_page(self, board, table, after="", limit=1000):
        rows = [{"id": "a", "priority": 10000, "created_on": "one", "status": "ready"},
                {"id": "b", "priority": 50000, "created_on": "two", "status": "done"}]
        return {"table": "tasks", "head": 2 if after and self.changed else 1,
                "rows": rows[1:] if after else rows[:1], "more": not bool(after),
                "next": after if self.stalled else "b" if after else "a"}


def test_raw_capture_resolves_terminal_exception_without_clipping():
    from hermes_cli import kanban_priority_prepare as prepare
    plan = prepare.capture(Pages(), board_id="board", exceptions=["b"])
    assert plan["rows"] == [{"id": "a", "origin": "one", "old": 10000, "new": 900}]
    assert plan["exceptions"] == [{"id": "b", "origin": "two", "old": 50000, "status": "done"}]


@pytest.mark.parametrize("hub, message", [(Pages(changed=True), "changed"), (Pages(stalled=True), "cursor")])
def test_capture_refuses_nonatomic_or_stalled_inventory(hub, message):
    from hermes_cli import kanban_priority_prepare as prepare
    with pytest.raises(ValueError, match=message):
        prepare.capture(hub, board_id="board", exceptions=[])


def test_stage_requires_local_owner_and_preserves_original(conn, tmp_path):
    from hermes_cli import kanban_priority_prepare as prepare
    ids, _, _ = helpers.setup_plan(conn)
    rows = [dict(r) for r in conn.execute("SELECT id,priority,created_on,status FROM tasks")]
    plan = migration.plan_global(rows, board_id=sync_helpers.BOARD, exceptions=[ids[2]])
    path = tmp_path / "original.json"
    result = prepare.stage(conn, plan, path=path, task_id=ids[0], writer=sync_helpers.ME)
    original = path.read_bytes()
    assert result["manifest_sha256"] == hashlib.sha256(original).hexdigest()
    attachment = kb.get_attachment(conn, result["attachment_id"])
    assert attachment is not None
    from pathlib import Path
    assert Path(attachment.stored_path).read_bytes() == original
    assert json.loads(original) == plan
    with pytest.raises(FileExistsError):
        prepare.stage(conn, plan, path=path, task_id=ids[0], writer=sync_helpers.ME)
    assert path.read_bytes() == original
    with pytest.raises(ValueError, match="attachment owner"):
        prepare.stage(conn, plan, path=tmp_path / "foreign.json", task_id=ids[1], writer=sync_helpers.ME)
    assert not (tmp_path / "foreign.json").exists()


def test_stage_refuses_worker_before_writing_file(conn, monkeypatch, tmp_path):
    from hermes_cli import kanban_priority_prepare as prepare
    ids, _, _ = helpers.setup_plan(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", ids[0])
    path = tmp_path / "blocked.json"
    with pytest.raises(PermissionError):
        prepare.stage(conn, {}, path=path, task_id=ids[0], writer=sync_helpers.ME)
    assert not path.exists()


@pytest.mark.parametrize('patch', [
    {'table': 'other'}, {'head': True}, {'rows': None}, {'more': 1},
    {'rows': [None]}, {'rows': [{'id': 'a', 'status': 'unknown'}]},
    {'rows': [{'id': 1, 'status': 'ready'}]},
    {'rows': [{'id': '', 'status': 'ready'}]},
    {'rows': [], 'more': True},
])
def test_capture_rejects_malformed_pages(patch):
    from hermes_cli import kanban_priority_prepare as prepare
    class Malformed:
        def snapshot_page(self, *args, **kwargs):
            return {'table': 'tasks', 'head': 1, 'rows': [], 'more': False, **patch}
    with pytest.raises(ValueError):
        prepare.capture(Malformed(), board_id='board', exceptions=[])


def test_capture_rechecks_final_head():
    from hermes_cli import kanban_priority_prepare as prepare
    class Changed(Pages):
        def snapshot_page(self, *args, **kwargs):
            page = super().snapshot_page(*args, **kwargs)
            if kwargs.get('limit') == 1:
                page['head'] = 5
            return page
    with pytest.raises(ValueError, match='changed'):
        prepare.capture(Changed(), board_id='board', exceptions=[])


@pytest.mark.parametrize('case', ['shadow', 'writer', 'board', 'missing_owner', 'bytes', 'readback'])
def test_stage_guard_failures_preserve_priorities(conn, tmp_path, monkeypatch, case):
    from hermes_cli import kanban_priority_prepare as prepare
    ids, _, _ = helpers.setup_plan(conn)
    rows = [dict(r) for r in conn.execute('SELECT id,priority,created_on,status FROM tasks')]
    plan = migration.plan_global(rows, board_id=sync_helpers.BOARD, exceptions=[])
    writer, owner = sync_helpers.ME, ids[0]
    data = None
    if case == 'shadow':
        conn.execute("UPDATE kanban_sync_state SET mode='shadow'")
    if case == 'writer':
        writer = 'foreign'
    if case == 'board':
        conn.execute("UPDATE kanban_sync_state SET board_id='foreign'")
    if case == 'missing_owner':
        owner = 'missing'
    if case == 'bytes':
        changed = dict(plan, board_id='different')
        data = json.dumps(changed).encode()
    if case == 'readback':
        monkeypatch.setattr(kb, 'get_attachment', lambda *args: None)
    conn.commit()
    path = tmp_path / 'guarded.json'
    with pytest.raises(ValueError):
        prepare.stage(conn, plan, path=path, task_id=owner, writer=writer, original_bytes=data)
    assert path.exists() == (case == 'readback')
    assert helpers.helpers.priorities(conn, ids) == [10000, 10000, 50000]
