"""Global rank plans preserve cross-origin ties and explicit operator exceptions."""
import pytest

from hermes_cli import kanban_priority_migration as migration


@pytest.mark.parametrize("case, reason", [
    ("missing", "unresolved exception"), ("duplicates", "duplicate"),
    ("origin", "origin"), ("priority", "integer"), ("ranks", "distinct"),
    ("exception_duplicates", "duplicate exception"), ("board", "board identity"),
])
def test_global_plan_rejects_ambiguous_inventory(case, reason):
    row = {"id": "a", "priority": 10000, "created_on": "one", "status": "ready"}
    cases = {
        "missing": ([row], ["missing"], "board"),
        "duplicates": ([row, row], [], "board"),
        "origin": ([dict(row, created_on=None)], [], "board"),
        "priority": ([dict(row, priority=True)], [], "board"),
        "ranks": ([dict(row, id=str(i), priority=i + 1) for i in range(901)], [], "board"),
        "exception_duplicates": ([row], ["a", "a"], "board"),
        "board": ([row], [], ""),
    }
    rows, exceptions, board = cases[case]
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


def test_both_open_exceptions_keep_requested_order_above_ordinary():
    rows = [
        {"id": "ordinary", "priority": 90000, "created_on": "one", "status": "ready"},
        {"id": "earlier", "priority": 50000, "created_on": "two", "status": "blocked"},
        {"id": "operator", "priority": 20002, "created_on": "one", "status": "running"},
    ]
    plan = migration.plan_global(rows, board_id="board", exceptions=["earlier", "operator"])
    by_id = {row["id"]: row for row in plan["rows"]}
    assert 0 < by_id["ordinary"]["new"] < by_id["earlier"]["new"] < by_id["operator"]["new"] == 900
    assert [row["old"] for row in plan["exceptions"]] == [50000, 20002]
