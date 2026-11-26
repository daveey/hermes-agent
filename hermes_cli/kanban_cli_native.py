"""Untrusted CLI request mapping for a parent-owned native creation bridge.

An offered descriptor is a locator, never a grant. Only the parent dispatch can
admit creation; a failed offer must never fall through to standalone creation.
"""
import argparse
import json
import os
from pathlib import Path

from tools.origin_cli_bridge import ENV_KEY

# Deliberately explicit: new CLI options must acquire a mapping or a refusal.
_DIRECT = frozenset({
    "title", "assignee", "tenant", "priority", "admission_class", "admission_frees",
    "triage", "idempotency_key", "distinct_from", "distinct_reason", "skills",
    "max_retries", "pin_mandatory", "completion_contract", "review_required",
    "goal_mode", "goal_max_turns", "initial_status", "human_request", "project", "board",
    "requires",
})
_RENAMED = {"parent": "parents", "model_override": "model", "provider_override": "provider"}
_REFUSED = frozenset({"branch", "reasoning_effort", "origin_session", "hub_reason", "force_hub", "runs_on"})
_LOCAL = frozenset({"command", "kanban_action", "json", "body", "body_file", "workspace", "max_runtime",
                    "created_by", "self_work", "_kanban_parser", "no_requires"})
# Top-level chat/launcher options are not creation fields or authority. In
# particular a child's --toolsets/--yolo cannot change the parent's dispatch.
_GLOBAL = frozenset({
    "accept_hooks", "checkpoints", "cli", "continue_last", "cpu_profile", "func",
    "ignore_rules", "ignore_user_config", "in_dir", "model", "no_restore_cwd", "oneshot",
    "pass_session_id", "provider", "reasoning", "resume", "safe_mode", "toolsets", "tui",
    "tui_dev", "tui_native", "usage_file", "version", "worktree", "yolo",
})


def offered(args):
    """Dispatch only opted-in native requests before child board initialization."""
    handler = {"create": offered_create, "release": offered_release}.get(getattr(args, "kanban_action", None))
    return handler(args) if handler is not None else None


def offered_create(args):
    """Return None only when no bridge was offered, otherwise a final exit code."""
    if ENV_KEY not in os.environ:
        return None
    from hermes_cli import kanban as cli
    from hermes_cli.kanban_cli_body import read_body
    from tools.origin_cli_bridge import request

    def refused(message):
        return cli._err("kanban: native CLI bridge: " + message, 2)

    values = vars(args)
    unknown = set(values) - _DIRECT - _RENAMED.keys() - _REFUSED - _LOCAL - _GLOBAL
    unsupported = [key for key in _REFUSED if values.get(key) is not None and values[key] is not False]
    if unknown or unsupported:
        return refused("unsupported options: " + ", ".join(sorted(unknown | set(unsupported))))
    if values.get("self_work") is not None or values.get("created_by", "user") != "user":
        return refused("self-work and author overrides are unsupported")
    if os.environ.get("HERMES_ORIGIN_SESSION"):
        return refused("explicit foreign origin is unsupported")
    if values.get("requires"):
        return refused("gateway capability routing is unsupported")
    body, error = read_body(args)
    if error:
        return refused(error)
    try:
        kind, path = cli._parse_workspace_flag(args.workspace)
        runtime = cli._parse_duration(values.get("max_runtime"))
    except (ValueError, argparse.ArgumentTypeError) as exc:
        return refused(str(exc))
    payload = {key: value for key, value in values.items() if key in _DIRECT and value is not None}
    payload.update({target: values[source] for source, target in _RENAMED.items() if values.get(source) is not None})
    # CLI creation is local. Do not silently substitute the tool's automatic
    # remote placement; that needs a separately authenticated forwarding path.
    payload.update(placement="local", body=body, workspace_kind=kind,
                   workspace_path=str(Path(path).expanduser().absolute()) if path else None,
                   max_runtime_seconds=runtime)
    result = request(os.environ[ENV_KEY], payload)
    if values.get("json"):
        print(json.dumps(result, ensure_ascii=False))
    elif result.get("ok"):
        print(result["task_id"])
    else:
        cli._err("kanban: native CLI bridge refused or outcome unknown; do not retry automatically", 2)
    return 0 if result.get("ok") else 2


def offered_release(args):
    """An explicit episode opts into foreign native release; legacy local stays unchanged."""
    if getattr(args, "episode", None) is None:
        if getattr(args, "fresh_after_expiry", None) is not None:
            from hermes_cli import kanban as cli
            return cli._err("kanban: --fresh-after-expiry requires the exact --episode native route", 2)
        return None
    from hermes_cli import kanban as cli
    from tools.origin_cli_bridge import request

    values = vars(args)
    allowed = _GLOBAL | {"command", "kanban_action", "_kanban_parser", "json", "board", "task_id", "ids",
                         "runs", "note", "episode", "capability_episodes", "seen_comment_id", "skills", "fresh_after_expiry"}
    if set(values) - allowed:
        return cli._err("kanban: native release has unsupported options: " + ", ".join(sorted(set(values) - allowed)), 2)
    if values.get("ids") or values.get("seen_comment_id") is not None:
        return cli._err("kanban: native release is exact single-card; foreign comments need HOME's read challenge", 2)
    if ENV_KEY not in os.environ:
        return cli._err("kanban: --episode requires the live owning-chat native terminal bridge; no standalone fallback", 2)
    payload = {k: values.get(k) for k in ("board", "task_id", "episode", "capability_episodes", "runs", "note")}
    if values.get("fresh_after_expiry") is not None:
        payload["fresh_after_expiry"] = values["fresh_after_expiry"]
    result = request(os.environ[ENV_KEY], payload, operation="release")
    if values.get("json"):
        print(json.dumps(result, ensure_ascii=False))
    elif result.get("ok"):
        print(f"{result['task_id']}: {result.get('release_stage', 'outcome_unknown')}")
    else:
        cli._err("kanban: native release refused or outcome unknown; do not retry automatically", 2)
    return 0 if result.get("ok") else 2
