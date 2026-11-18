"""Worker context caps done-parent handoffs at the newest ``_CTX_MAX_PARENT_RESULTS``. [daveey]

Regression for t_01a1a1dea9c64d6a / t_01a194b9de51e620 (2026-11-18): long-lived owner cards had 30 and 22
finished prerequisite cards linked as parents, and their results were 42 KB and 51 KB of a ~100 KB context.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_context as ctx


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _child_with_done_parents(conn, n: int) -> tuple[str, list[str]]:
    child = kb.create_task(conn, title="owner", assignee="worker")
    parents = []
    for i in range(n):
        pid = kb.create_task(conn, title=f"prereq {i}", assignee="worker")
        kb.link_tasks(conn, pid, child)
        assert kb.complete_task(conn, pid, result=f"RESULT-{i:02d} " + "x" * 200)
        with kb.write_txn(conn):   # distinct, increasing completion times
            conn.execute("UPDATE tasks SET completed_at=? WHERE id=?", (1_700_000_000 + i * 60, pid))
            conn.execute("UPDATE task_runs SET ended_at=? WHERE task_id=?", (1_700_000_000 + i * 60, pid))
        parents.append(pid)
    return child, parents


def test_few_parents_are_all_shown(kanban_home) -> None:
    with kbc.connect_closing() as conn:
        child, parents = _child_with_done_parents(conn, ctx._CTX_MAX_PARENT_RESULTS)
        text = kb.build_worker_context(conn, child)
    assert all(f"### {p}" in text for p in parents)
    assert "earlier parent result" not in text


def test_many_parents_keep_the_newest_and_name_the_rest(kanban_home) -> None:
    cap = ctx._CTX_MAX_PARENT_RESULTS
    with kbc.connect_closing() as conn:
        child, parents = _child_with_done_parents(conn, cap + 5)
        text = kb.build_worker_context(conn, child)
    old, new = parents[:5], parents[5:]
    assert all(f"### {p}" in text for p in new)
    assert all(f"### {p}" not in text for p in old)
    assert "RESULT-00" not in text and f"RESULT-{cap + 4:02d}" in text
    note = next(line for line in text.splitlines() if "earlier parent result" in line)
    assert note.startswith("_(5 earlier parent results omitted")
    assert all(p in note for p in old)     # every omitted id stays reachable by name


def test_cap_leaves_small_cards_unchanged_and_orders_by_parent_id(kanban_home) -> None:
    """At or under the cap nothing is dropped or reordered; a parent with no completion time is omitted first."""
    cap = ctx._CTX_MAX_PARENT_RESULTS
    with kbc.connect_closing() as conn:
        child, parents = _child_with_done_parents(conn, cap + 1)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET completed_at=NULL WHERE id=?", (parents[-1],))
            conn.execute("UPDATE task_runs SET ended_at=NULL WHERE task_id=?", (parents[-1],))
        text = kb.build_worker_context(conn, child)
    shown = [line[4:].split(" ")[0] for line in text.splitlines() if line.startswith("### t_")]
    assert shown == sorted(shown) and len(shown) == cap
    assert parents[-1] not in shown
    assert "_(1 earlier parent result omitted" in text

