"""Bounded isolated baseline: four cards, fake spawn, no provider or host load."""
from contextlib import closing
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch


@pytest.fixture
def capacity_board(tmp_path, monkeypatch):
    home = tmp_path / "capacity-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    path = kb.kanban_db_path()
    assert path.is_relative_to(home)
    from hermes_cli.kanban_model_assignment import _assignee_config
    from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml
    assert _assignee_config("alice") is None
    profile = home / "profiles" / "alice"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(_managed_yaml())
    assert _assignee_config("alice")["model_routing"]["enabled"] is True
    # No synthetic PID may resolve to an unrelated host process.
    monkeypatch.setattr(dispatch, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(dispatch, "_worker_alive", lambda *args: False)
    monkeypatch.setattr(dispatch, "_process_fingerprint", lambda pid: "fixture|1")
    monkeypatch.setattr(dispatch, "count_running_tasks_other_boards", lambda board=None: 0)
    with closing(kbc.connect(path)) as conn:
        ids = [kb.create_task(conn, title=f"capacity fixture {i}", assignee="alice") for i in range(3)]
        child = kb.create_task(conn, title="dependency control", assignee="alice", parents=[ids[0]])
        yield conn, ids, child


@pytest.mark.parametrize("pressure,max_spawn,expected", [
    ("critical", 3, 0), ("elevated", 3, 1), ("ok", 3, 3), ("unknown", 3, 3), ("ok", 0, 0),
])
def test_bounded_fake_spawn_baseline(capacity_board, all_assignees_spawnable, monkeypatch,
                                     pressure, max_spawn, expected):
    start = time.monotonic()
    conn, ids, child = capacity_board
    monkeypatch.setattr(dispatch, "_memory_pressure_level", lambda sample=None: pressure)
    seen = []

    def fake_spawn(task, workspace, board=None):
        seen.append(task.id)
        return 424242

    result = dispatch.dispatch_once(conn, spawn_fn=fake_spawn, max_spawn=max_spawn,
                                    max_in_progress=4, reconcile_orphans=False)
    assert len(seen) == expected, repr(result)
    assert set(seen) <= set(ids)
    assert child not in seen
    assert kb.get_task(conn, child).status == "todo"
    assert len(result.spawned) == expected
    assert time.monotonic() - start < 120
