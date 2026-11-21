"""``schedule_task`` parks (split from ``test_kanban_db``): a park waits on time with its wake reason. [daveey]"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # No external writers here; real scans can fail closed. See test_kanban_workspace_jobs.
    from hermes_cli import kanban_workspace_jobs
    monkeypatch.setattr(kanban_workspace_jobs.psutil, "process_iter", lambda: iter(()))
    kb.init_db()
    return home


def test_schedule_task_parks_time_delay_without_dispatching(kanban_home):
    with kbc.connect() as conn:
        t = kb.create_task(conn, title="delayed recheck", assignee="ops")
        assert kb.schedule_task(conn, t, reason="run next week", clock_only=True,
                                awaiting={"kind": "clock", "why": "next week's run"}) is True
        task = kb.get_task(conn, t)
        assert task.status == "scheduled"
        assert kb.claim_task(conn, t) is None

        events = kb.list_events(conn, t)
        # Every park carries a wake (default wait without a SCHEDULED_UNTIL marker). [daveey]
        assert any(e.kind == "scheduled" and e.payload["reason"] == "run next week"
                   and isinstance(e.payload.get("wake_at"), int) for e in events)
