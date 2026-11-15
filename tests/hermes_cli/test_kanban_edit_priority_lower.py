"""``hermes kanban edit --priority``: lowering a card's priority needs ``--reason``.

A chat once ran ``kanban edit <id> --priority 10`` on a stale read and silently undid
another actor's raise to 150. The CLI now refuses a lowering without a reason (exit 1,
naming the current value); raising or re-setting the same value is unchanged. [daveey]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_parser import build_parser


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb.init_db()
    return home


def _main(argv: list[str]) -> int:
    """The real argparse + dispatch path (rc and refusals included)."""
    root = argparse.ArgumentParser(prog="hermes")
    build_parser(root.add_subparsers(dest="command"))
    return kc.kanban_command(root.parse_args(["kanban", *argv]))


def _card(priority: int) -> str:
    with kbc.connect_closing() as conn:
        return kb.create_task(conn, title="card", priority=priority)


def _priority(task_id: str) -> int:
    with kbc.connect_closing() as conn:
        return int(conn.execute("SELECT priority FROM tasks WHERE id = ?", (task_id,)).fetchone()[0])


def _reprioritized(task_id: str) -> list[dict]:
    with kbc.connect_closing() as conn:
        return [e.payload or {} for e in kb.list_events(conn, task_id) if e.kind == "reprioritized"]


def test_lowering_without_reason_is_refused_naming_current_value(kanban_home, capsys):
    tid = _card(150)
    rc = _main(["edit", tid, "--priority", "10"])
    err = capsys.readouterr().err
    print(f"rc={rc} priority={_priority(tid)} stderr={err.strip()!r}")
    assert rc == 1
    assert "currently 150" in err and "--reason" in err
    assert _priority(tid) == 150
    assert _reprioritized(tid) == []


def test_lowering_with_reason_applies_and_records_reason(kanban_home, capsys):
    tid = _card(150)
    rc = _main(["edit", tid, "--priority", "10", "--reason", "superseded by t_x"])
    assert rc == 0, capsys.readouterr().err
    assert _priority(tid) == 10
    assert _reprioritized(tid) == [{"priority": 10, "previous_priority": 150, "reason": "superseded by t_x"}]


def test_blank_reason_does_not_count(kanban_home, capsys):
    tid = _card(150)
    assert _main(["edit", tid, "--priority", "10", "--reason", "  "]) == 1
    assert _priority(tid) == 150


def test_raising_needs_no_reason(kanban_home, capsys):
    tid = _card(10)
    assert _main(["edit", tid, "--priority", "150"]) == 0
    assert _priority(tid) == 150
    assert _reprioritized(tid) == [{"priority": 150, "previous_priority": 10}]


def test_same_value_needs_no_reason(kanban_home, capsys):
    tid = _card(150)
    assert _main(["edit", tid, "--priority", "150"]) == 0
    assert _priority(tid) == 150
    assert _reprioritized(tid) == [{"priority": 150, "previous_priority": 150}]


def test_refused_lowering_leaves_operator_fields_unapplied(kanban_home, capsys):
    tid = _card(150)
    assert _main(["edit", tid, "--priority", "10", "--max-retries", "5"]) == 1
    with kbc.connect_closing() as conn:
        row = conn.execute("SELECT priority, max_retries FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert (int(row[0]), row[1]) == (150, None)


def test_reason_without_priority_or_contract_still_refused(kanban_home, capsys):
    tid = _card(0)
    assert _main(["edit", tid, "--title", "x", "--reason", "why"]) == 2
    assert "--priority" in capsys.readouterr().err


def test_library_default_unchanged_for_dashboard_and_rest(kanban_home):
    """edit_task without require_lower_reason (dashboard/REST callers) still lowers silently."""
    tid = _card(150)
    with kbc.connect_closing() as conn:
        assert kb.edit_task(conn, tid, priority=10)
    assert _priority(tid) == 10
