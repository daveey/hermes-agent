"""Fixture for guard tests that model a card whose worker already ran (and so may have opened a PR)."""
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch_pr_state as prs


def worker_comment(conn, task_id, author, body):
    """Model an owning worker, including the provenance the real tools stamp."""
    row = conn.execute("SELECT id FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if row is None:
        run_id = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?, ?, 'running', ?)",
            (task_id, author, int(time.time())),
        ).lastrowid
    else:
        run_id = row[0]
    with pytest.MonkeyPatch.context() as env:
        env.setenv("HERMES_KANBAN_TASK", task_id)
        env.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
        return _add_comment(conn, task_id, author, body)


_add_comment = kb.add_comment


@pytest.fixture
def worker_ran(monkeypatch):
    monkeypatch.setattr(kb, "add_comment", worker_comment)
    monkeypatch.setattr(prs, "_pr_comments_predate_first_run", lambda *a, **k: False)
