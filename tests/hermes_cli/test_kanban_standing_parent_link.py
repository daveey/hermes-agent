"""kanban_link refuses a never-finishing standing card as a parent. [daveey]

Defect agent-0fddefc2d0: the Ask Eve rollout card t_01a18e12cdcab970 sat in ``todo`` for 8 days because an
agent linked it under t_01a184cee94603eb ("Standing: support Eve"), a permanent listener that re-parks
forever and never completes, as a "dependency hold". A link to such a card is a silent forever-hold; the
right tools are a wake_check park (an event) or a human_request block (a person). ``link_tasks`` now refuses a
parent that is standing: its title starts with "Standing:", or it re-parked more than 5 times in the last
72 h without completing. Both the db entry point and the ``kanban_link`` tool are covered.
"""
import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _parks(conn, task_id, n, *, age_s):
    now = int(time.time())
    with kbc.write_txn(conn):
        for i in range(n):
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome) "
                "VALUES (?, 'default', 'scheduled', ?, ?, 'scheduled')",
                (task_id, now - age_s - 60 - i, now - age_s - i))


def _pair(conn, parent_title):
    parent = kb.create_task(conn, title=parent_title, assignee="default")
    child = kb.create_task(conn, title="rollout: wait for Eve's evidence", assignee="default")
    return parent, child


def test_standing_title_parent_refused(kanban_home):
    with kbc.connect() as conn:
        parent, child = _pair(conn, "Standing: support Eve on Ask Eve")
        with pytest.raises(ValueError) as err:
            kb.link_tasks(conn, parent, child)
        msg = str(err.value)
        assert "standing" in msg.lower() and "wake_check" in msg and "human_request" in msg
        assert kb.parent_ids(conn, child) == []


def test_frequently_reparked_parent_refused(kanban_home):
    with kbc.connect() as conn:
        parent, child = _pair(conn, "hub side of Alpha hand-offs")
        _parks(conn, parent, 6, age_s=3600)
        with pytest.raises(ValueError, match="wake_check"):
            kb.link_tasks(conn, parent, child)
        assert kb.parent_ids(conn, child) == []


def test_normal_parent_still_links(kanban_home):
    with kbc.connect() as conn:
        parent, child = _pair(conn, "Ask Eve PR6: admin settings page")
        _parks(conn, parent, 5, age_s=3600)          # 5 recent parks is not "more than 5"
        kb.link_tasks(conn, parent, child)
        assert kb.parent_ids(conn, child) == [parent]


def test_old_parks_do_not_count(kanban_home):
    with kbc.connect() as conn:
        parent, child = _pair(conn, "rollout card that waited on Eve last week")
        _parks(conn, parent, 9, age_s=4 * 86400)     # all outside the 72 h window
        kb.link_tasks(conn, parent, child)
        assert kb.parent_ids(conn, child) == [parent]


def test_kanban_link_tool_refuses_standing_parent(kanban_home):
    from tools.registry import registry
    import tools.kanban_tools  # registers the tools
    assert tools.kanban_tools

    with kbc.connect() as conn:
        parent, child = _pair(conn, "Standing: support Eve on Ask Eve")
        normal, child2 = _pair(conn, "ordinary parent")
    tool = registry.get_entry("kanban_link")
    assert tool is not None
    out = json.loads(tool.handler({"parent_id": parent, "child_id": child}))
    assert out.get("error") and "wake_check" in out["error"], out
    ok = json.loads(tool.handler({"parent_id": normal, "child_id": child2}))
    assert not ok.get("error"), ok
    with kbc.connect() as conn:
        assert kb.parent_ids(conn, child) == []
        assert kb.parent_ids(conn, child2) == [normal]
