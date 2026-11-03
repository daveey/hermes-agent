"""Main-health owner annotations are best-effort, read-only, and non-authoritative."""
import sqlite3
from copy import deepcopy

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from hermes_cli.kanban_pr_merge_time import annotate_main_health_owner

SHA = "a" * 40


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    kb.init_db()
    with connect() as conn:
        yield conn


def receipt(branch="main", ok=True):
    label = "acme/repo" if branch == "main" else f"acme/repo@{branch}"
    return {"ok": ok, "classification": "success" if ok else "failure", "main_health": {
        "idempotency_key": f"ci-red:{label}:{SHA}", "detail": "CI red; ci-main-watch."}}


def owner(conn, key, status="running", creator="ci-main-watch"):
    tid = kb.create_task(conn, title="Repair main", idempotency_key=key, created_by=creator)
    conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
    conn.commit()
    return tid


@pytest.mark.parametrize("status", ["ready", "running", "scheduled", "done", "archived", None])
def test_only_active_owner_is_annotated_without_writes(board, status):
    expected = None
    if status:
        tid = owner(board, f"ci-red:acme/repo:{SHA}", status)
        if status not in {"done", "archived"}:
            expected = tid
    value = receipt()
    before = board.total_changes
    annotate_main_health_owner(board, value)
    assert value["main_health"].get("owner_card") == expected
    assert value["ok"] is True and value["classification"] == "success"
    assert board.total_changes == before


@pytest.mark.parametrize("branch", ["main", "release/test"])
@pytest.mark.parametrize("other_board", [False, True])
@pytest.mark.parametrize("ok", [False, True])
def test_first_red_streak_owner_across_boards(board, branch, other_board, ok):
    label = "acme/repo" if branch == "main" else f"acme/repo@{branch}"
    key = f"ci-red:{label}:" + "b" * 40
    if other_board:
        kb.create_board("ci-owners")
        with connect(board="ci-owners") as other:
            tid = owner(other, key)
    else:
        tid = owner(board, key)
    # A newer closed exact-SHA card cannot eclipse the active streak owner.
    owner(board, f"ci-red:{label}:{SHA}", "done")
    owner(board, f"ci-red:{label}:" + "c" * 40, creator="someone-else")
    owner(board, "ci-red:acme/repository:" + "d" * 40)
    value = receipt(branch, ok)
    before = board.total_changes
    annotate_main_health_owner(board, value)
    assert value["main_health"].get("owner_card") == tid
    assert value["ok"] is ok
    assert value["classification"] == ("success" if ok else "failure")
    assert board.total_changes == before


def test_owner_lookup_failure_does_not_change_acceptance(board):
    value = receipt()
    before = deepcopy(value)
    class Unreadable:
        def execute(self, *args):
            raise sqlite3.OperationalError("unreadable diagnostic board")

    annotate_main_health_owner(Unreadable(), value)
    assert value == before


def test_sibling_open_is_read_only_and_missing_db_not_created(board, monkeypatch, tmp_path):
    from hermes_cli import kanban_db_workspace as workspace
    from hermes_cli import kanban_lockguard

    path = tmp_path / "absent.db"
    monkeypatch.setattr(workspace, "_sibling_board_db_files", lambda conn: [path])
    real_connect = kanban_lockguard.connect
    calls = []

    def observed(database, **kwargs):
        calls.append(database)
        assert database.endswith("?mode=ro") and kwargs["uri"] is True
        return real_connect(database, **kwargs)

    monkeypatch.setattr(kanban_lockguard, "connect", observed)
    value = receipt()
    annotate_main_health_owner(board, value)
    assert calls and not path.exists()
    assert value["ok"] and "owner_card" not in value["main_health"]
