"""Structured and CLI adapters for the same admitted containment service."""
from __future__ import annotations

import argparse
import json

from hermes_cli import kanban_containment_service as service


def add_parsers(actions) -> None:
    stop = actions.add_parser("contain", help="Contain non-human work without satisfying dependencies")
    stop.add_argument("task_id", nargs="?", help="Own task or unclaimed descendant; defaults to admitted task")
    stop.add_argument("--authority", required=True, help="Independent same-board task authorized to resume")
    stop.add_argument("--producer", help="Optional independent producer; requires a genuinely new completed run")
    stop.add_argument("--summary", required=True, help="Bounded handoff, not a human request")
    release = actions.add_parser("resume-contained", help="Explicit scoped authority or new producer event")
    release.add_argument("task_id")
    release.add_argument("--episode", required=True, help="Exact UUIDv7 containment episode")
    release.add_argument("--kind", choices=("authority", "producer"), default="authority")


def command(args: argparse.Namespace) -> int:
    from hermes_cli.kanban_containment_canonical import DeliveryUnknown
    from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
    from hermes_cli.kanban_containment_policy import ContainmentPolicyError
    try:
        result = _execute(args)
    except DeliveryUnknown as exc:
        print(json.dumps({"ok": False, "code": exc.code, "error": str(exc),
                          "request_uid": exc.request_uid, "episode": exc.episode}))
        return 1
    except (ContainmentRefused, ContainmentPolicyError) as exc:
        print(json.dumps({"ok": False, "code": exc.code, "error": str(exc)}))
        return 1
    print(json.dumps({"ok": True, **result}, sort_keys=True))
    return 0


def _execute(args: argparse.Namespace) -> dict:
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        if args.kanban_action == "contain":
            return service.contain(conn, task_id=args.task_id, authority=args.authority,
                                   producer=args.producer, summary=args.summary)
        return service.resume(conn, task_id=args.task_id, episode=args.episode, kind=args.kind)

