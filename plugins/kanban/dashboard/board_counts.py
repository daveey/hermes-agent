"""Switcher counts need no schema reconciliation or writer initialization."""
from contextlib import closing
import sqlite3

from hermes_cli import kanban_db
from hermes_cli.kanban_generation import open_ro


def board_counts(slug: str) -> dict[str, int]:
    """Read existing tasks.status, including legacy boards; never create a store.

    Use the generation-guarded query-only connection, including its missing WAL
    sidecar fallback. Writer connect still owns integrity checks and migrations.
    """
    try:
        with closing(open_ro(kanban_db.kanban_db_path(board=slug))) as conn:
            return {status: int(count) for status, count in conn.execute(
                "SELECT status, COUNT(*) FROM tasks GROUP BY status")}
    except (OSError, ValueError, sqlite3.Error):
        return {}
