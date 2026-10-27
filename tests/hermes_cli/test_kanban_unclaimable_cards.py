"""Cards nobody can claim: unassigned ready and stale triage."""
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kd


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing() as c:
        yield c


def _row(conn, tid):
    return conn.execute("SELECT status, assignee, body FROM tasks WHERE id=?", (tid,)).fetchone()


def test_stale_triage_swept_once(conn):
    tid = kb.create_task(conn, title="held", triage=True)
    assert kd.sweep_stale_triage(conn) == []  # too fresh
    future = int(time.time()) + 7200
    assert kd.sweep_stale_triage(conn, default_assignee="default", now=future) == [tid]
    row = _row(conn, tid)
    assert row["status"] == "ready" and row["assignee"] == "default" and "triage sweep" in row["body"]
    conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
    conn.commit()
    assert kd.sweep_stale_triage(conn, now=future + 7200) == []


def _args(**kw):
    import argparse
    return argparse.Namespace(**kw)


def test_cli_defaults_assignee_but_not_triage_or_blocked(conn):
    from hermes_cli import kanban
    assert kanban._cli_default_assignee(_args(triage=False, initial_status="running")) == "default"
    assert kanban._cli_default_assignee(_args(triage=True)) is None
    assert kanban._cli_default_assignee(_args(triage=False, initial_status="blocked")) is None
