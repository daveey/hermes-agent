"""Global rank plans preserve cross-origin ties and explicit operator exceptions."""
import pytest

from hermes_cli import kanban_priority_migration as migration


@pytest.mark.parametrize("case, reason", [
    ("missing", "unresolved exception"), ("duplicates", "duplicate"),
    ("origin", "origin"), ("priority", "integer"), ("ranks", "distinct"),
    ("exception_duplicates", "duplicate exception"), ("board", "board identity"),
])
def test_global_plan_rejects_ambiguous_inventory(case, reason):
    rows = [{"id": "a", "priority": 10000, "created_on": "one", "status": "ready"}]
    exceptions = []
    board = "board"
    if case == "missing":
        exceptions = ["missing"]
    elif case == "duplicates":
        rows *= 2
    elif case == "origin":
        rows[0]["created_on"] = None
    elif case == "priority":
        rows[0]["priority"] = True
    elif case == "ranks":
        rows = [{"id": str(i), "priority": i + 1, "created_on": "one", "status": "ready"}
                for i in range(901)]
    elif case == "exception_duplicates":
        exceptions = ["a", "a"]
    elif case == "board":
        board = ""
    with pytest.raises(ValueError, match=reason):
        migration.plan_global(rows, board_id=board, exceptions=exceptions)


def test_global_plan_reserves_exception_bands_and_records_terminal_resolution():
    rows = [
        {"id": "a", "priority": 50000, "created_on": "one", "status": "ready"},
        {"id": "b", "priority": 50000, "created_on": "two", "status": "ready"},
        {"id": "c", "priority": 70000, "created_on": "two", "status": "ready"},
        {"id": "operator", "priority": 20002, "created_on": "one", "status": "ready"},
        {"id": "earlier", "priority": 10300, "created_on": "two", "status": "done"},
    ]
    plan = migration.plan_global(rows, board_id="board", exceptions=["earlier", "operator"])
    by_id = {r["id"]: r for r in plan["rows"]}
    assert by_id["a"]["new"] == by_id["b"]["new"] < by_id["c"]["new"] < 900
    assert by_id["operator"] == {"id": "operator", "origin": "one", "old": 20002, "new": 900}
    assert plan["exceptions"] == [
        {"id": "earlier", "status": "done", "origin": "two", "old": 10300},
        {"id": "operator", "status": "ready", "origin": "one", "old": 20002},
    ]
    assert "earlier" not in by_id
