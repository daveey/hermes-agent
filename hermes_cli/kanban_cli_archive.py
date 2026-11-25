"""``hermes kanban archive``: archive cards, purge archived ones, or (``--reason``) a human operator archive of a
contained card (:mod:`hermes_cli.kanban_containment_human`). Moved out of the ``kanban`` CLI facade. [daveey]"""
from __future__ import annotations

import argparse
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_foreign_card as _foreign_card
from hermes_cli.kanban_archive_scope import worker_archive_scope
from hermes_cli.kanban_output import _bulk_apply, _err


def cmd_archive(args: argparse.Namespace) -> int:
    ids = list(args.task_ids or [])
    purge_ids = list(getattr(args, "purge_ids", None) or [])
    if ids and purge_ids:
        return _err("choose either task_ids to archive or --rm archived task_ids")
    if not ids and not purge_ids:
        return _err("at least one task_id is required")
    with kbc.connect_closing() as conn:
        if purge_ids:
            return _bulk_apply(purge_ids, lambda tid: kb.delete_archived_task(conn, tid), lambda tid: f"Deleted {tid}",
                               lambda tid: f"cannot delete {tid} (must already be archived)")
        vias: dict[str, Optional[str]] = {}
        for tid in ids:
            foreign = _foreign_card.refusal(conn, tid)  # [daveey] only a replica copy here: name the owner
            if foreign:
                return _err(f"cannot archive {tid}: {_foreign_card.REJECTION}: {foreign}")
            refusal, vias[tid] = worker_archive_scope(conn, tid)
            if refusal:
                return _err(refusal)
        human = None
        if getattr(args, "reason", None) is not None:  # [daveey] a human operator closes contained cards
            from hermes_cli.kanban_containment_connections import held
            from hermes_cli.kanban_containment_human import for_caller
            from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
            if loose := [tid for tid in ids if not held(conn, tid)]:
                return _err(f"--reason (a human operator archive) applies only to a contained card; not contained: "
                            f"{', '.join(loose)}")
            try:
                human = for_caller(args.reason)
            except ContainmentRefused as exc:
                return _err(f"cannot archive {', '.join(ids)}: {exc}")
            from hermes_cli.kanban_containment_human import dependents
            waiting = {tid: dependents(conn, tid) for tid in ids}
            for tid, deps in waiting.items():
                if deps:
                    print(f"{tid} has {len(deps)} dependent card(s) that will stay waiting, and cannot be unlinked "
                          "while its containment hold stands:")
                    for dep, status, title in deps:
                        print(f"  {dep}  {status:8s}  {title}")
            if any(waiting.values()) and not getattr(args, "yes", False):
                return _err("refusing the human operator archive without --yes: the dependent cards above stay "
                            "waiting on a parent that never succeeds; rerun with --yes to archive anyway")
        # A card another machine runs under its live Board lease gets a stop request, not a local status write
        # the Board would reject (kanban_remote_stop). [daveey]
        from hermes_cli.kanban_remote_stop import RemoteArchiveRefused, archive as _archive
        outcomes: dict[str, dict] = {}
        refusals: dict[str, str] = {}

        from hermes_cli.kanban_containment_lifecycle import ContainmentRefused as _Contained

        def _one(tid: str) -> bool:
            try:
                out = _archive(conn, tid, via=vias.get(tid), **({"human": human} if human else {}))
            except RemoteArchiveRefused as exc:
                refusals[tid] = str(exc)
                return False
            except _Contained as exc:  # [daveey] say why, and who can still archive it
                refusals[tid] = f"cannot archive {tid}: {exc}"
                return False
            if out:
                outcomes[tid] = out
            return bool(out)

        def _ok_line(tid: str) -> str:
            out = outcomes[tid]
            if out.get("status") == "archive_pending":
                return (f"Archive pending {tid}: running on {out['holder']} under its live lease; a stop request "
                        f"was sent and {out['holder']} archives it at its next dispatcher tick")
            return f"Archived {tid}"

        return _bulk_apply(ids, _one, _ok_line, lambda tid: refusals.get(tid) or f"cannot archive {tid}", conn=conn)
