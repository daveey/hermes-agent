"""Read-only producer interface: hermes --run-module hermes_cli.kanban_effective_priority TASK."""

import argparse
import contextlib
import json
import sqlite3


class MissingRequester(ValueError):
    """The board is readable but has no urgency observation for this requester."""


def snapshot(task_id: str, *, board=None) -> dict:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_admission_score import effective_priorities
    from hermes_cli.kanban_host_counts import board_of
    from hermes_cli.sqlite_safe_read import connect_tracked

    path = kb.kanban_db_path(board).resolve()
    with contextlib.closing(connect_tracked(path.as_uri() + "?mode=ro", uri=True,
                                           tracking_path=path, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        value = effective_priorities(conn, [task_id]).get(task_id)
        if value is None:
            raise MissingRequester("requester is absent from the board and replica")
        return {"task_id": task_id, "board": board_of(conn), "priority": value}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id")
    parser.add_argument("--board")
    parser.add_argument("--fallback", type=int,
                        help="use this priority only when the requester is absent (reported in JSON)")
    args = parser.parse_args(argv)
    try:
        result = snapshot(args.task_id, board=args.board)
    except MissingRequester as exc:
        if args.fallback is None:
            parser.exit(1, f"effective priority unavailable: {exc}\n")
        result = {"task_id": args.task_id, "board": args.board, "priority": args.fallback,
                  "fallback_reason": str(exc)}
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(1, f"effective priority unavailable: {exc}\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
