"""``hermes kanban claim`` (moved out of ``hermes_cli/kanban.py``, which re-exports it as ``_cmd_claim``).

A card with workspace_source custody or a manifest is refused (``source_cli_claim_refused``): a CLI claim sends no
``source_protocol`` and would run outside custody (workspace_source v1, v4.1 R3-a).
"""

from __future__ import annotations

import argparse

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli.kanban_output import _err
from hermes_cli.kanban_source_cli import claim_refusal


def cmd_claim(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        if refusal := claim_refusal(conn, args.task_id):
            return _err(refusal)
        task = kb.claim_task(conn, args.task_id, ttl_seconds=args.ttl)
        if task is None:
            existing = kb.get_task(conn, args.task_id)
            if existing is None:
                return _err(f"no such task: {args.task_id}")
            return _err(f"cannot claim {args.task_id}: status={existing.status} "
                        f"lock={existing.claim_lock or '(none)'}")
        workspace = kbw.resolve_workspace(task)
        kbw.set_workspace_path(conn, task.id, str(workspace))
    print(f"Claimed {task.id}\nWorkspace: {workspace}")
    return 0
