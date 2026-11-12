"""Canonical verification is read-only and checks the complete global inventory."""
import copy

import pytest

from hermes_cli import kanban_priority_migration as migration
from hermes_cli import kanban_priority_prepare as prepare


class Canonical:
    def __init__(self, rows):
        self.rows = rows

    def snapshot_page(self, board, table, **kwargs):
        return {"table": "tasks", "head": 7, "rows": self.rows, "more": False}


def inventory():
    return [{"id": "a", "priority": 10000, "created_on": "one", "status": "ready"},
            {"id": "b", "priority": 50000, "created_on": "two", "status": "running"},
            {"id": "c", "priority": 90000, "created_on": "two", "status": "done"}]


def test_verify_global_normalization_and_restore():
    rows = inventory()
    manifest = migration.plan_global(rows, board_id="board", exceptions=["b", "c"])
    normalized = copy.deepcopy(rows)
    for row, planned in zip(normalized, manifest["rows"]):
        row["priority"] = planned["new"]
    assert prepare.verify(Canonical(normalized), manifest) == {"verified": 2, "board_id": "board", "direction": "apply"}
    assert prepare.verify(Canonical(rows), manifest, undo=True)["direction"] == "restore"
    assert rows == inventory()


@pytest.mark.parametrize("change", ["missing", "new", "owner", "value", "terminal", "exception-reopened"])
def test_verify_refuses_stale_global_inventory(change):
    rows = inventory()
    manifest = migration.plan_global(rows, board_id="board", exceptions=["b", "c"])
    for row, planned in zip(rows, manifest["rows"]):
        row["priority"] = planned["new"]
    actions = {
        "missing": lambda: rows.pop(0),
        "new": lambda: rows.append({"id": "d", "priority": 0, "created_on": "one", "status": "ready"}),
        "owner": lambda: rows[0].update(created_on="two"),
        "value": lambda: rows[0].update(priority=12),
        "terminal": lambda: rows[0].update(status="done"),
        "exception-reopened": lambda: rows[2].update(status="ready"),
    }
    actions[change]()
    with pytest.raises(ValueError, match="canonical"):
        prepare.verify(Canonical(rows), manifest)
