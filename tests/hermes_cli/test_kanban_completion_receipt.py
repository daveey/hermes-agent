"""Canonical completion receipt admission, independent of tool wrappers."""
from copy import deepcopy
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_origin
from tests.hermes_state.test_completion_receipt import receipt


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / "board-home"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: ("here", {"here", "away"}))
    path = kb.kanban_db_path(board="default")
    assert path.is_relative_to(home)
    with kbc.connect_closing(path) as connection:
        yield connection


def task(conn, **kwargs):
    return kb.create_task(conn, title="Bounded assessment", assignee="coder", **kwargs)


def pending(owner):
    value = receipt("contained")
    value["items"][0]["remaining"] = [{"scope": "cause investigation", "state": "pending",
                                       "owner": owner, "reason": "Separate scoped owner"}]
    return value


def snapshot(conn):
    return {table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")]
            for table in ("tasks", "task_runs", "task_events")}


def test_malformed_receipt_rejects_before_side_effecting_gates(conn):
    tid = task(conn)
    before = snapshot(conn)
    with pytest.raises(ValueError, match="completion_receipt"):
        kb.complete_task(conn, tid, summary="Assessment complete", created_cards=["t_missing"],
                         metadata={"completion_receipt": {"version": 1, "items": []}})
    assert snapshot(conn) == before


@pytest.mark.parametrize("disposition", ["delivered", "superseded", "contained", "rejected"])
def test_receipt_stored_on_canonical_completed_run(conn, disposition):
    tid = task(conn)
    value = receipt(disposition)
    before = deepcopy(value)
    assert kb.complete_task(conn, tid, summary="Assessment complete", metadata={"completion_receipt": value})
    event = conn.execute("SELECT run_id FROM task_events WHERE task_id=? AND kind='completed'", (tid,)).fetchone()
    row = conn.execute("SELECT metadata FROM task_runs WHERE id=?", (event[0],)).fetchone()
    assert json.loads(row[0])["completion_receipt"] == before
    assert value == before


@pytest.mark.parametrize("which", ["missing", "unrelated", "closed", "wrong_gateway", "wrong_board", "self"])
def test_pending_owner_must_be_open_related_and_fully_qualified(conn, which):
    tid = task(conn)
    owner = task(conn, parents=[tid]) if which not in ("unrelated", "self") else task(conn)
    if which == "missing":
        owner = "t_missing"
    if which == "self":
        owner = tid
    if which == "closed":
        # Fixture state only: cannot complete a child before its parent through normal admission.
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (owner,))
        conn.commit()
    gateway = "away" if which == "wrong_gateway" else "here"
    board = "other" if which == "wrong_board" else "default"
    before = snapshot(conn)
    with pytest.raises(ValueError, match="completion_receipt"):
        kb.complete_task(conn, tid, summary="Containment complete", metadata={
            "completion_receipt": pending(f"@task:{board}@{gateway}/{owner}")})
    assert snapshot(conn) == before


def test_open_linked_owner_admitted_without_changing_receipt(conn):
    tid = task(conn)
    owner = task(conn, parents=[tid])
    value = pending(f"@task:default@here/{owner}")
    assert kb.complete_task(conn, tid, summary="Containment complete", metadata={"completion_receipt": value})
    assert kb.get_task(conn, owner).status == "ready"


def test_deferred_scope_needs_no_owner_and_grants_no_authority(conn):
    tid = task(conn)
    value = receipt()
    value["items"][0]["remaining"] = [{"scope": "enforcement", "state": "deferred", "owner": None,
                                       "reason": "User deferred; execution not authorized"}]
    assert kb.complete_task(conn, tid, summary="Report delivered", metadata={"completion_receipt": value})
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
