"""Manual release CLI handlers with per-card unread-thread refusals. [daveey]"""
from __future__ import annotations

import os
import sys

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_release_comments import UNREAD_EXIT, capture_refusal, cli_guard


def _seen_bulk_error(args, ids):
    return len(ids) > 1 and getattr(args, "seen_comment_id", None) is not None


def cmd_unblock(args):
    from hermes_cli.kanban_cli_actor import cli_actor
    from hermes_cli.kanban_shared_blockers import unblock_verdict
    if os.environ.get("HERMES_KANBAN_TASK"):
        return cli._err("kanban unblock is orchestrator-only; workers must hand off their assigned task")
    ids, rc = cli._require_ids(args, cli._REASON_FLAG_HINT)
    if rc:
        return rc
    if _seen_bulk_error(args, ids):
        return cli._err("--seen belongs to one card; release each card separately with its own newest read comment id")
    reason = cli._stripped_or_none(getattr(args, "reason", None))
    author = cli._profile_author() if reason else None
    suffix = f": {reason}" if reason else ""
    refused = {}
    with kbc.connect_closing() as conn:
        rc = cli._ids_error(ids, cli._REASON_FLAG_HINT, conn)
        if rc:
            return rc
        op = cli._commented(reason, author, "UNBLOCK", lambda tid, comment: kb.unblock_task(
            conn, tid, comment=comment, event_extra={"actor": cli_actor()},
            precondition=cli_guard(args, tid, "unblock")))
        rc = cli._bulk_apply(
            ids, lambda tid: capture_refusal(lambda: op(tid), tid, refused),
            lambda tid: f"Unblocked {tid}{suffix}; {unblock_verdict(conn, tid)}",
            lambda tid: refused[tid]["error"] if tid in refused else (
                kb.unblock_refusal(conn, tid) or f"cannot unblock {tid} (not blocked/scheduled?)"), conn=conn)
        return UNREAD_EXIT if refused else rc


def cmd_promote(args):
    reason = cli._joined_words(args.reason)
    author = cli._profile_author()
    ids = list(dict.fromkeys(cli._bulk_ids(args)))
    if _seen_bulk_error(args, ids):
        return cli._err("--seen belongs to one card; release each card separately with its own newest read comment id")
    dry_run = bool(args.dry_run)
    results, refused = [], {}
    with kbc.connect_closing() as conn:
        for tid in ids:
            outcome = capture_refusal(lambda: kb.promote_task(
                conn, tid, actor=author, reason=reason, dry_run=dry_run,
                precondition=cli_guard(args, tid, "promote")), tid, refused)
            ok, err = outcome if outcome is not False else (False, refused[tid]["error"])
            results.append({"task_id": tid, "promoted": ok, "dry_run": dry_run,
                            "reason": reason, "error": err, **refused.get(tid, {})})
    failed = any(not r["promoted"] for r in results)
    rc = UNREAD_EXIT if refused else int(failed)
    if getattr(args, "json", False):
        cli._print_json(results[0] if len(results) == 1 else results)
        return rc
    tag = " (dry)" if dry_run else ""
    label = "Would promote" if dry_run else "Promoted"
    suffix = f": {reason}" if reason else ""
    for r in results:
        if r["promoted"]:
            print(f"{label} {r['task_id']} -> ready{tag}{suffix}")
        else:
            print(f"cannot promote {r['task_id']}: {r['error']}", file=sys.stderr)
    return rc


from hermes_cli import kanban as cli
