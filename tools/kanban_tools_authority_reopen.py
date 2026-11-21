"""Exact owner-wake authority reopen through the authenticated tool, never a shell."""
from __future__ import annotations

import os
from contextlib import nullcontext


def handle(args: dict) -> str:
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db_connect as kbc, kanban_db_operator as operator
    from hermes_cli import kanban_foreign_card as foreign
    from hermes_cli.kanban_requeue import owner_wake_authority_reopen_grant, owner_wake_listed_refs

    kt._check(kt._is_dispatcher_owned_worker(), "authority reopen requires a live owner-wake worker")
    owner = os.environ.get("HERMES_KANBAN_TASK")
    target, authority = args.get("task_id"), args.get("reopen_authority")
    kt._check(owner and target and authority, "task_id and reopen_authority are required")
    owner, target, authority = str(owner), str(target), str(authority)
    with kt._board(None) as (kb, own):
        worker = kb.get_task(own, owner)
        refs = [(slug, tid) for slug, tid in owner_wake_listed_refs(worker)
                if tid == target and (args.get("board") is None or args["board"] == slug)]
        kt._check(len(refs) == 1, "authority reopen requires one exact listed contained target")
        slug = refs[0][0]
        same = slug == kb.get_current_board()
        path = kb.board_own_db_path(slug)
        kt._check(same or path.is_file(), "listed board is not available locally")
        scope = nullcontext(own) if same else kbc.connect_closing(db_path=path)
        with scope as conn:
            for tid in (target, authority):
                kt._check(not foreign.refusal(conn, tid), "authority reopen requires local target custody")
            def precondition(current):
                kt._check(owner_wake_authority_reopen_grant(
                    current, owner, authority, board=slug, expected_run_id=kt._worker_run_id(owner),
                    worker_conn=own, target_id=target) == owner,
                    "owner-wake authority grant or live run does not match")
            # Lock the owner board across the cross-board write; the same-board
            # precondition is already inside reopen's write transaction.
            with (nullcontext() if same else kbc.write_txn(own)):
                status = operator.reopen_done_task(
                    conn, authority, reason=f"Owner-wake {owner}: reopen nominated authority of {slug}/{target}",
                    actor=owner, precondition=precondition)
            kt._check(status is not None, "nominated authority is no longer done")
    return kt._ok(task_id=authority, status=status, via="owner-wake-authority-reopen", contained_target=target)
