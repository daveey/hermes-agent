"""Agent-owned non-human containment; identity is never supplied by tool args."""
from __future__ import annotations

import json

from hermes_cli import kanban_containment_service as service
from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
from hermes_cli.kanban_containment_policy import ContainmentPolicyError
from hermes_cli.kanban_containment_canonical import DeliveryUnknown


def _schema(name, description, properties, required):
    return {"name": name, "description": description, "parameters": {
        "type": "object", "properties": {**properties, "board": {"type": "string"}},
        "required": required, "additionalProperties": False}}


CONTAIN_SCHEMA = _schema("kanban_contain",
    "Contain an admitted run without satisfying dependencies. Protected NON-HUMAN triage preserves all open edges "
    "without dispatching review or asking a human. Can also contain an unclaimed descendant. Resume requires the exact "
    "episode and a live scoped authority or genuinely new producer completion; generic unblock cannot release it. "
    "Synced boards refuse until canonical containment capability is installed. After terminal_for_run=true, stop.",
    {"task_id": {"type": "string"}, "authority": {"type": "string"},
     "producer": {"type": "string"}, "summary": {"type": "string", "minLength": 1,
        "description": "Bounded handoff summary; runtime rejects more than 2000 characters."}},
    ["authority", "summary"])
RESUME_SCHEMA = _schema("kanban_resume_contained",
    "Resume an exact contained episode with authenticated evidence. Requires a live scoped authority "
    "or genuinely new completed producer run. "
    "Preserves independent spend/human holds and every dependency edge; does not complete the task.",
    {"task_id": {"type": "string"}, "episode": {"type": "string"},
     "kind": {"type": "string", "enum": ["authority", "producer"]}}, ["task_id", "episode"])


def _handle(args: dict, *, release: bool) -> str:
    from tools import kanban_tools as kt
    from tools.registry import tool_error
    try:
        with kt._board(args.get("board")) as (_, conn):
            if release:
                result = service.resume(conn, task_id=args["task_id"], episode=args["episode"],
                                         kind=args.get("kind", "authority"))
            else:
                result = service.contain(conn, task_id=args.get("task_id"), authority=args["authority"],
                                          producer=args.get("producer"), summary=args["summary"])
        return json.dumps({"ok": True, **result})
    except DeliveryUnknown as exc:
        return json.dumps({"ok": False, "code": exc.code, "error": str(exc),
                           "request_uid": exc.request_uid, "episode": exc.episode})
    except (ContainmentRefused, ContainmentPolicyError) as exc:
        return json.dumps({"ok": False, "code": exc.code, "error": str(exc)})
    except ValueError as exc:
        return tool_error(str(exc), code=getattr(exc, "code", "containment_refused"))


def handle_contain(args: dict, **kwargs) -> str:
    return _handle(args, release=False)


def handle_resume(args: dict, **kwargs) -> str:
    return _handle(args, release=True)
