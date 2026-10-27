"""Fixture for guard tests that model a card whose worker already ran (and so may have opened a PR)."""
import pytest

from hermes_cli import kanban_db_dispatch_pr_state as prs


@pytest.fixture
def worker_ran(monkeypatch):
    monkeypatch.setattr(prs, "_pr_comments_predate_first_run", lambda *a, **k: False)
