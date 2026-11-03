"""A fenced connect() of a board with no database names the fence. [daveey]

A spawned descendant whose HERMES_DELEGATED_CHILD_CONTEXT root covers the board
path opens it ``?mode=ro``. When the file does not exist SQLite only said
``unable to open database file`` (raised from kanban_lockguard), which a worker
misread as a lockguard bug (t_01a19efdde6364d6; real cause: the inherited fence
covered a pytest tmp_path under <root>/cache/scratch, t_01a19f60a4e241d2).
"""
from __future__ import annotations

import pytest

from agent.delegation_context import (
    DELEGATED_CHILD_ENV_MARKER,
    DELEGATED_CHILD_OWNER_ENV,
    kanban_path_is_fenced,
)
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def fenced_missing_board(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    home = root / "cache" / "scratch" / "kanban-home"  # missing: never created
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_WORKSPACES_ROOT", DELEGATED_CHILD_OWNER_ENV):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv(DELEGATED_CHILD_ENV_MARKER, str(root))
    kb._INITIALIZED_PATHS.clear()
    path = kb.kanban_db_path()
    assert not path.exists()
    assert kanban_path_is_fenced(path)
    return root, home, path


def test_fenced_connect_of_missing_board_names_the_fence(fenced_missing_board):
    root, home, path = fenced_missing_board
    with pytest.raises(PermissionError) as exc:
        kbc.connect()
    msg = str(exc.value)
    assert str(path) in msg
    assert "fenced" in msg
    assert f"{DELEGATED_CHILD_ENV_MARKER}={root}" in msg
    assert "has no database" in msg
    # Nothing was created on the descendant's behalf.
    assert not home.exists()
