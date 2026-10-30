"""Kanban tool error bodies: the safe next step when the board's database fails. [daveey]

A bare ``kanban_show: disk I/O error`` once left an agent with no route, and it wrote the live board with raw
``sqlite3`` (INSERT/UPDATE) and deleted its ``-shm`` and ``.corrupt.*.bak`` files (self-heal agent-6eb3a51e94).
A SQLite failure now carries a ``hint``: retry once, then the same change through ``hermes kanban <verb>`` in a
fresh process, never raw writes or deleting the board's files, ``hermes kanban recover`` when the integrity
check fails, block when the CLI fails too.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from tools.registry import tool_error

# Tool -> CLI verb with its arguments, for the hint. Unlisted tools use their own name minus ``kanban_``.
_CLI_ARGS = {
    "kanban_show": "show {task_id}",
    "kanban_comment": 'comment {task_id} "<body>"',
    "kanban_unblock": "unblock {task_id}",
    "kanban_block": 'block {task_id} "<reason>"',
    "kanban_complete": 'complete {task_id} --summary "<summary>"',
    "kanban_create": 'create "<title>" --assignee <profile>',
    "kanban_list": "list",
    "kanban_link": "link {parent_id} {child_id}",
    "kanban_unlink": "unlink {parent_id} {child_id}",
    "kanban_supervisor_request": "supervisor-requests --task {task_id}",
    "kanban_heartbeat": "heartbeat {task_id}",
    "kanban_request_review": 'request-review {task_id} --summary "<summary>"',
}


def _is_db_failure(exc: BaseException) -> bool:
    if isinstance(exc, sqlite3.DatabaseError):  # OperationalError ("disk I/O error"), "malformed", ...
        return True
    from hermes_cli.kanban_db_connect import KanbanDbCorruptError
    return isinstance(exc, KanbanDbCorruptError)


def db_error_hint(tool_name: str, args: dict, exc: BaseException) -> Optional[str]:
    """The fallback procedure for a board whose database failed, or None for any other error."""
    if not _is_db_failure(exc):
        return None
    args = args if isinstance(args, dict) else {}
    board = str(args.get("board") or "").strip()
    cli = f"hermes kanban --board {board}" if board else "hermes kanban"
    template = _CLI_ARGS.get(tool_name, tool_name.removeprefix("kanban_").replace("_", "-") + " ...")
    fields = {"task_id": args.get("task_id") or "<id>", "parent_id": args.get("parent_id") or "<parent>",
              "child_id": args.get("child_id") or "<child>"}
    verb = template.format(**fields)
    return (
        f"The board's database failed in this process. 1) Retry this call once. 2) If it fails again, make the "
        f"same change with the CLI in a fresh process: `{cli} {verb}` (the terminal tool). 3) Never write "
        "kanban.db with sqlite3 (INSERT/UPDATE/DELETE) and never delete its -wal, -shm or .corrupt.*.bak files: "
        "that bypasses the board's transitions and corrupts it under open connections. 4) If "
        "`sqlite3 -readonly <board>/kanban.db 'pragma integrity_check'` does not print ok, run "
        f"`{cli} recover` (one owner; a second run names the holder). 5) If the CLI fails too, stop and "
        "kanban_block with the error text."
    )


def generic_tool_error(tool_name: str, args: dict, exc: BaseException, classify_rejections: bool) -> str:
    """The error body for an unclassified handler failure: ``<tool>: <exc>``, a ``rejection`` class when the
    tool classifies rejections, and the database-fallback ``hint`` when the board's database failed."""
    extra: dict = {"rejection": "error"} if classify_rejections else {}
    hint = db_error_hint(tool_name, args, exc)
    if hint:
        extra["hint"] = hint
    return tool_error(f"{tool_name}: {exc}", **extra)
