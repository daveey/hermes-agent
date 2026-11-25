"""Tests for the kanban `promote` verb (issue #28822).

The realistic bug scenario from #28822 is: a child task ends up in
``todo`` with all its parents already ``done`` (because the
auto-promote daemon hasn't run, or a manual close raced it).
Direct-SQL setup is used to construct that state deterministically.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def _stuck_todo(conn, *, parents_done=True, n_parents=1):
    """Build the #28822 scenario: child in 'todo' whose parents may
    have closed as 'done' without the auto-promote logic firing.
    """
    parent_ids = [
        kb.create_task(conn, title=f"parent{i}", assignee="setup")
        for i in range(n_parents)
    ]
    child_id = kb.create_task(
        conn, title="child", parents=parent_ids, assignee="setup"
    )
    assert kb.get_task(conn, child_id).status == "todo"
    if parents_done:
        for pid in parent_ids:
            conn.execute(
                "UPDATE tasks SET status='done' WHERE id=?", (pid,)
            )
    return child_id, parent_ids


def test_promote_stuck_todo_succeeds(conn):
    child, _ = _stuck_todo(conn, parents_done=True)
    ok, err = kb.promote_task(conn, child, actor="tester")
    assert ok and err is None
    assert kb.get_task(conn, child).status == "ready"


def test_promote_refuses_undone_parent_and_names_the_real_remedy(conn):
    # #106195: promotion must never report a 'ready' that the first claim reverts.
    child, (parent,) = _stuck_todo(conn, parents_done=False)
    ok, err = kb.promote_task(conn, child, actor="tester", reason="recovery")
    assert not ok
    assert err and parent in err
    assert kb.get_task(conn, child).status == "todo"
    assert kb.claim_task(conn, child) is None  # still gated; nothing pretended




# ---------------------------------------------------------------------------
# CLI `_cmd_promote` — bulk via `--ids` (the issue's anti-respawn use case:
# promote all children of a closed parent in one command).
# ---------------------------------------------------------------------------


def _promote_ns(task_id, *, ids=None, reason=None, force=False,
                dry_run=False, as_json=False):
    return argparse.Namespace(
        task_id=task_id,
        reason=list(reason or []),
        ids=list(ids or []) or None,
        force=force,
        dry_run=dry_run,
        json=as_json,
    )


def test_cli_promote_bulk_ids_promotes_all(kanban_home, capsys):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        children = [
            kb.create_task(conn, title=f"c{i}", parents=[parent])
            for i in range(3)
        ]
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
    rc = kb_cli._cmd_promote(_promote_ns(children[0], ids=children[1:]))
    assert rc == 0
    out = capsys.readouterr().out
    for c in children:
        assert c in out
    with kbc.connect() as conn:
        for c in children:
            assert kb.get_task(conn, c).status == "ready"


def test_cli_decides_ready_stop_after_plain_promotions(kanban_home, capsys):
    from hermes_cli import kanban_spend_decision as decision
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Ready with an undecided capability stop")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, decision.EVENT, {"cause": "capability_unavailable"})
            for _ in range(3):
                kb._append_event(conn, tid, "promoted", {})
        assert decision.pending(conn, tid)
    args = _promote_ns(tid, reason=["Prerequisite now done; original bounded scope"], as_json=True)
    assert kb_cli._cmd_promote(args) == 0
    assert json.loads(capsys.readouterr().out)["promoted"] is True
    with kbc.connect_closing() as conn:
        assert not decision.pending(conn, tid)
        events = kb.list_events(conn, tid)
        manual = [e for e in events if e.kind == "promoted_manual"]
        assert len(manual) == 1
        assert manual[0].payload["from_status"] == "ready"
    # An ordinary ready card cannot be re-promoted to mint another spend decision.
    assert kb_cli._cmd_promote(args) == 1


@pytest.mark.parametrize("cause", ["safety_refusal", "scope_stopped"])
def test_ready_decision_does_not_clear_execution_restriction(conn, cause):
    from hermes_cli import kanban_spend_decision as decision
    tid = kb.create_task(conn, title="Restricted ready copy")
    with kb.write_txn(conn):
        kb._append_event(conn, tid, decision.EVENT, {"cause": cause})
    for dry in (True, False):
        assert not kb.promote_task(conn, tid, actor="operator", dry_run=dry)[0]
    assert decision.pending(conn, tid)
    assert not any(e.kind == "promoted_manual" for e in kb.list_events(conn, tid))


def test_ready_decision_cannot_bypass_open_parent(conn):
    from hermes_cli import kanban_spend_decision as decision
    tid, (parent,) = _stuck_todo(conn, parents_done=False)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        kb._append_event(conn, tid, decision.EVENT, {"cause": "capability_unavailable"})
    for dry in (True, False):
        ok, why = kb.promote_task(conn, tid, actor="operator", dry_run=dry)
        assert not ok and parent in why
    assert decision.pending(conn, tid)


