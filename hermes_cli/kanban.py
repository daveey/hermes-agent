"""``hermes kanban …`` — dispatch (``kanban_command``), task-verb handlers, ``run_slash`` for ``/kanban``.
DB work lives in ``kanban_db``; siblings: ``kanban_parser`` (argparse, re-exported ``build_parser``),
``kanban_output`` (text/--json), ``kanban_boards`` (``boards …``), ``kanban_ops`` (dispatch/daemon/
tail/watch/gc/repair).
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json

from hermes_cli.kanban_published_worktree import publication_scope
from hermes_cli.kanban_owner_chat import TERMINAL as TERMINAL_OWNER, declared_producer as _declared_producer
import logging
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_overlap
from hermes_cli import kanban_db_notify as kbn
from hermes_cli import kanban_db_operator as kbo
from hermes_cli import kanban_swarm as ks
from hermes_cli import kanban_workflow
from hermes_cli import kanban_contract_cli as _contract_cli
from hermes_cli import kanban_foreign_card as _foreign_card
from hermes_cli import kanban_shadow_view as _shadow_view
from hermes_cli.kanban_show_print import _print_diagnostics, _print_max_retries, _print_section
from hermes_cli import kanban_remote_stop as _remote_stop
from hermes_cli import kanban_review_contract as _review_contract
from hermes_cli import kanban_spend_budget_cli as _spend_budget_cli
from hermes_cli import kanban_worker_shell as _worker_shell
from hermes_cli.kanban_cli_triage_lines import _decompose_ok_line, _retitled_suffix
from hermes_cli.kanban_cli_flags import (parse_branch_flag as _parse_branch_flag,
                                         parse_workspace_flag as _parse_workspace_flag)
from hermes_cli.kanban_output import (
    _ATTACHMENT_FIELDS, _RUNS_RUN_FIELDS, _SHOW_RUN_FIELDS, _bulk_apply, _err,
    _fmt_counts, _fmt_task_line, _fmt_ts, _json_out, _obj_dict, _print_json,
    _task_to_dict,
)
from hermes_cli.kanban_boards import _dispatch_boards
from hermes_cli.kanban_schema_owner import BoardSchemaNotOwned
from hermes_cli.kanban_notify_identity import _caller_identity_for_target
from hermes_cli.kanban_cli_metadata import ReceiptEditRefused, parse_metadata_flag as _parse_metadata_flag, receipt_refusal
from hermes_cli.kanban_cli_park import (
    _REASON_FLAG_HINT, _REASON_WORDS_HINT, _cli_awaiting, _cli_wake_check, _commented, _guarded_park, _ids_error,
    _parked_msg,
)
from hermes_cli.kanban_ops import (
    _cmd_daemon, _kanban_config, _cmd_dispatch, _cmd_gc, _cmd_recover, _cmd_repair, _cmd_tail, _cmd_watch,
)
from hermes_cli.kanban_parser import build_parser, cmd_resume as _cmd_resume, cmd_quota_release as _cmd_quota_release, cmd_correct_diagnostics as _cmd_correct_diagnostics, cmd_edit_text as _cmd_edit_text
from hermes_cli.kanban_claim_cli import cmd_claim as _cmd_claim  # moved; re-exported for callers
from hermes_cli.kanban_source_cli import cmd_source_release as _cmd_source_release
from hermes_cli.kanban_run_models_cli import command as _cmd_attest_run_models
from hermes_cli.kanban_sync_cli import cmd_sync as _cmd_sync
from hermes_cli.kanban_admission_explain import cmd_queue as _cmd_queue
from hermes_cli.kanban_lanes import cli_creator as _lane_creator
from hermes_cli.kanban_lanes_cli import command as _cmd_lanes
from hermes_cli.kanban_assignees_cli import cmd_assignees as _cmd_assignees  # moved; re-exported for callers
from hermes_cli.kanban_archive_scope import (  # moved here from this module; re-exported for callers
    _capability_owner_waiting_card, _creator_task_id, _is_stranded_runs_on_copy,
    _worker_may_archive_capability_owner, _worker_may_archive_own,
    archive_scope_for, worker_archive_scope, worker_may_archive,
)
from hermes_cli.kanban_requeue import (  # moved here from this module; re-exported for callers
    OWNER_WAKE_REQUEUE_STATUSES, _is_owner_wake_supervisor, _is_self_heal_fix_card, has_requeue_grant,
    named_defect_card, owner_wake_listed_cards, requeue_grant, self_heal_requeue_grant,
)


# --- Flag parsing helpers ---

def _none_profile(value: str) -> Optional[str]:
    """``none`` / ``-`` / ``null`` mean "unassign"."""
    return None if value.lower() in {"none", "-", "null"} else value


def _run_state_kwargs(args: argparse.Namespace, cmd: str) -> tuple[Optional[dict[str, str]], int]:
    """``--state-type``/``--state-name`` must be given together: ``(kwargs, 0)`` or ``(None, 2)``."""
    st = getattr(args, "state_type", None)
    sn = getattr(args, "state_name", None)
    if (st is None) != (sn is None):
        return None, _err(f"kanban {cmd}: pass both --state-type and --state-name, or omit both", 2)
    return ({} if st is None else {"state_type": st, "state_name": sn}), 0


def _check_dispatcher_presence(hermes_home: Optional[Path] = None) -> tuple[bool, str]:
    """``(running, message)`` for the "will anything dispatch this?" warning: True when a gateway is
    alive for this HERMES_HOME with ``kanban.dispatch_in_gateway`` on, else False + human guidance.
    Fails OPEN (probe/config errors -> ``(True, "")``) — a missed warning beats crying wolf.
    ``hermes_home`` scopes the probe to a profile dir (dashboard backend); CLI callers pass None.

    The dashboard plugin API passes it because the dashboard backend process can be running under a
    different HERMES_HOME than the profile the request targets, which otherwise produced a "no gateway is
    running" warning against a perfectly healthy profile gateway (#71211). CLI callers leave it ``None`` and
    keep the existing process-level behavior.
    """
    try:
        # The dispatcher is host-wide: one gateway holds ``<kanban root>/kanban/.dispatcher.lock`` and
        # dispatches every board for every profile. Ask who holds it before judging this home's own
        # gateway/config, or a profile CLI with dispatch_in_gateway=false warns "task will sit in ready"
        # while the root gateway dispatches it fine. [daveey]
        from gateway.kanban_singleton import describe_holder, holder_info
        from hermes_cli.kanban_db import kanban_home

        holder = holder_info(kanban_home() / "kanban" / ".dispatcher.lock")
        if holder:
            return (True, f"host dispatcher: {describe_holder(holder)}")
    except Exception:  # health: allow BLE001 -- fails open like the rest of this probe
        logging.getLogger(__name__).debug("dispatcher presence: host lock probe failed", exc_info=True)
    try:
        from gateway.status import resolve_gateway_liveness  # type: ignore

        # Same ladder as the dashboard status endpoints so PID-file-less / cross-container gateways
        # aren't misreported; use_cache=False because this one-shot probe must see the state now.
        liveness = resolve_gateway_liveness(profile_dir=hermes_home, use_cache=False)
    except Exception:
        return (True, "")  # can't probe — silent
    if liveness.probe_error:  # resolver swallows per-rung failures; "can't tell" != "no gateway"
        return (True, "")
    pid = liveness.pid
    # Even if the gateway is up, dispatch_in_gateway may be off (can't tell -> assume default).
    if pid and bool(_kanban_config().get("dispatch_in_gateway", True)):
        return (True, f"gateway pid={pid}, dispatch enabled")
    if pid:
        return (False, "Gateway is running but kanban.dispatch_in_gateway=false in "
                "config.yaml — the task will sit in 'ready' until you flip it "
                "back on and restart the gateway, OR run the legacy "
                "standalone daemon (`hermes kanban daemon --force`).")
    return (False, "No gateway is running — the task will sit in 'ready' until you "
            "start it. Run:\n    hermes gateway start\n"
            "The gateway hosts an embedded dispatcher (tick interval 60s by "
            "default); your task will be picked up on the next tick after "
            "the gateway comes up.")


# --- Command dispatch ---

_ORIGIN_COMMANDS = {
    "chat-tasks": "hermes_cli.kanban_chat_tasks",
    "origin-forward": "hermes_cli.kanban_origin_forward_adapter",
    "origin-bind": "hermes_cli.kanban_origin_adapter",
    **dict.fromkeys(("origin-create", "origin-adopt", "ownership-snapshot", "selfwork-create", "selfwork-forward", "selfwork-producer"),
                    "hermes_cli.kanban_origin_owner"),
}

def kanban_command(args: argparse.Namespace) -> int:
    """Entry point from ``hermes kanban …``; returns a shell-style exit code."""
    action = getattr(args, "kanban_action", None)
    if not action:
        parser = getattr(args, "_kanban_parser", None)
        if parser is not None:
            parser.print_help()
        else:
            print("usage: hermes kanban <action> [options]\n"
                  "Run 'hermes kanban --help' for the full list of actions.", file=sys.stderr)
        return 0

    # Fast-fail for UX only; the durable trust boundary is in kanban_db, since children can
    # import DB mutators directly.
    if _is_delegated_child_cli_mutation(args):
        from hermes_cli.top_level_env import delegated_child_refusal  # names a leaked worker env [daveey]
        return _err(delegated_child_refusal(
            "kanban: delegate_task child contexts cannot mutate Kanban tasks via the CLI "
            "(this shell descends from a Kanban worker or a delegate_task child, whose terminals "
            "are fenced from board writes by design; make the change with the agent's kanban_* "
            "tools, e.g. kanban_comment, kanban_block, kanban_link, kanban_unlink, kanban_create)"))

    # Native requests execute only in the owning parent, before any child-side
    # board initialization or publication scope can write local state.
    if action == "create":
        from hermes_cli.kanban_cli_native import offered_create
        bridged = offered_create(args)
        if bridged is not None:
            return bridged

    # Origin obligations are profile-local, not current-board state; do not
    # auto-initialize a board or route by a worker's inherited board override.
    if module := _ORIGIN_COMMANDS.get(action):
        return importlib.import_module(module).command(args)

    # `boards …` manages board metadata and the current-board pointer itself, so it must ignore
    # the `--board` routing override (else `--board beta boards show` reports beta).
    if action == "boards":
        return _dispatch_boards(args)

    # `--board <slug>` pins HERMES_KANBAN_BOARD for the duration of this call so it inherits the
    # exact resolution the dispatcher uses for workers.
    board_override = getattr(args, "board", None)
    board_scope = contextlib.nullcontext()
    if board_override:
        try:
            normed = kb._normalize_board_slug(board_override)
        except ValueError as exc:
            return _err(f"kanban: {exc}", 2)
        if not normed:
            return _err("kanban: --board requires a slug", 2)
        # Boards other than 'default' must already exist — typoed slugs would otherwise silently
        # create an empty board.
        if normed != kb.DEFAULT_BOARD and not kb.board_exists(normed):
            return _err(f"kanban: board {normed!r} does not exist. "
                        f"Create it with `hermes kanban boards create {normed}`.")
        board_scope = kb.scoped_current_board(normed)

    with board_scope:
        if module := {"stranded": "hermes_cli.kanban_actionability_cli",
                      "record-actionability": "hermes_cli.kanban_actionability_recovery_cli"}.get(action):
            return importlib.import_module(module).command(args)
        # Maintenance handlers skip init_db; their connections still run schema setup.
        if handler := {"repair": _cmd_repair, "recover": _cmd_recover,
                       "correct-diagnostics": _cmd_correct_diagnostics, "edit-text": _cmd_edit_text}.get(action):
            return handler(args)
        try:
            kb.init_db()
        except BoardSchemaNotOwned as exc:  # one line, its own "kanban:" prefix
            return _err(str(exc))
        except Exception as exc:
            return _err(f"kanban: could not initialize database: {exc}")

        handler = _HANDLERS.get(action)
        if not handler:
            return _err(f"kanban: unknown action {action!r}", 2)
        if refused := _remote_stop.cli_refusal(action, args):  # [daveey] another machine's live Board lease
            return _err(refused)
        try:
            return int(handler(args) or 0)
        except BoardSchemaNotOwned as exc:  # a write a non-installed core may not make here, not a traceback [daveey]
            return _err(str(exc))
        except (ValueError, RuntimeError, PermissionError) as exc:
            return _err(f"kanban: {exc}")


# --- Handlers ---

# Env vars only an agent's tool process carries (terminal session bridge, execute_code
# sandbox RPC). A plain user shell has none of them, so it never gets the warning. [daveey]
_AGENT_ENV_MARKERS = ("HERMES_SESSION_KEY", "HERMES_SESSION_ID", "HERMES_SESSION_SOURCE",
                      "HERMES_RPC_SOCKET", "HERMES_KERNEL_SENTINEL")


def _cli_default_assignee(args) -> Optional[str]:
    """Dispatchable cards need an assignee: an unassigned ``ready``/``todo`` card is
    never spawned. Default it (``kanban.default_assignee`` or ``default``); triage and
    blocked drafts stay unassigned. Trackers for remote work should use
    ``kanban_schedule`` with a ``wake_check`` instead."""
    if getattr(args, "triage", False) or getattr(args, "initial_status", "running") == "blocked":
        return None
    return kb._default_ready_assignee()


def _agent_invoked() -> bool:
    """Whether this CLI call came from an agent tool (terminal / execute_code)."""
    return any(os.environ.get(name) for name in _AGENT_ENV_MARKERS)


def _profile_author() -> str:
    """Best-effort author name for an interactive CLI call."""
    from hermes_cli.profiles import current_profile_name
    return current_profile_name("user") or "user"


_DELEGATED_CHILD_DENIED_ACTIONS: frozenset[str] = frozenset({
    "init", "attest-run-models", "create", "swarm", "assign", "reclaim", "move", "reassign", "link", "unlink",
    "claim", "comment", "attach", "attach-rm", "complete", "edit", "block", "correct-diagnostics", "edit-text",
    "schedule", "unblock", "contain", "resume-contained", "promote", "archive", "dispatch", "daemon", "repair", "recover",
    "heartbeat", "notify-subscribe", "notify-unsubscribe", "specify", "decompose",
    "request-review", "request-changes", "reopen-review", "reopen", "resume", "quota-release", "source-release",
    "gc", "origin-bind", "origin-forward", "origin-create", "origin-adopt", "sync", "supervisor-requests", "spend-budget", "lanes",
    "record-actionability", "release",
})

_DELEGATED_CHILD_DENIED_BOARD_ACTIONS: frozenset[str] = frozenset({
    "create", "new", "rm", "remove", "delete", "switch", "use", "rename",
    "set-default-workdir", "dispatch-owner", "import",
})


def _is_delegated_child_cli_mutation(args: argparse.Namespace) -> bool:
    action = getattr(args, "kanban_action", None)
    if action == "boards":
        if (getattr(args, "boards_action", None) or "list") not in _DELEGATED_CHILD_DENIED_BOARD_ACTIONS:
            return False
    elif action not in _DELEGATED_CHILD_DENIED_ACTIONS:
        return False
    from agent.delegation_context import kanban_path_is_fenced

    if action in {"origin-bind", "origin-forward"}:
        from hermes_constants import get_hermes_home
        ledger = "origin-forward.db" if action == "origin-forward" else "origin-ledger.db"
        return (kanban_path_is_fenced(get_hermes_home() / "kanban" / ledger)
                or (action == "origin-forward" and kanban_path_is_fenced(get_hermes_home() / "state.db")))
    return kanban_path_is_fenced(kb.kanban_home()) or kanban_path_is_fenced(kb.kanban_db_path())


def _joined_words(words) -> Optional[str]:
    """Free-text positional ``nargs="*"`` words -> stripped string, or None when absent."""
    return " ".join(words).strip() if words else None


def _stripped_or_none(value: Optional[str]) -> Optional[str]:
    """``None`` stays ``None``; otherwise strip, and treat the empty string as ``None``."""
    return None if value is None else (value.strip() or None)


def _ok_or_err(ok, fail: str, done: str) -> int:
    """Single-mutation handlers: print ``done`` (rc 0) or ``fail`` to stderr (rc 1)."""
    if not ok:
        return _err(fail)
    print(done)
    return 0


def _bulk_ids(args: argparse.Namespace) -> list[str]:
    """Positional ``task_id`` plus ``--ids`` extras (bulk verbs)."""
    return [args.task_id] + list(getattr(args, "ids", None) or [])


def _require_ids(args: argparse.Namespace, hint: str = 'Free text goes in its flag, e.g. --reason "..."',
                 ) -> tuple[list[str], int]:
    """``args.task_ids`` -> ``(ids, 0)``, ``([], 1)`` when empty, or ``([], 2)`` when a token is not
    task-id shaped (``_ids_error``), after printing the error."""
    ids = list(args.task_ids or [])
    if not ids:
        return ids, _err("at least one task_id is required")
    rc = _ids_error(ids, hint)
    return ([], rc) if rc else (ids, 0)


def _parse_duration(val) -> Optional[int]:
    """``30s`` / ``5m`` / ``2h`` / ``1d`` or a raw integer → seconds; None for empty input;
    ValueError on malformed input."""
    if val is None or val == "":
        return None
    s = str(val).strip().lower()
    try:
        return int(s)  # bare integer → seconds
    except ValueError:
        pass
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if not (s and s[-1] in units):
        raise ValueError(f"malformed duration {val!r} (expected 30s, 5m, 2h, 1d, or a number)")
    try:
        n = float(s[:-1])
    except ValueError as exc:
        raise ValueError(f"malformed duration {val!r}") from exc
    return int(n * units[s[-1]])


def _cmd_init(args: argparse.Namespace) -> int:
    path = kb.init_db()
    print(f"Kanban DB initialized at {path}")
    print()
    # Profiles on disk == assignees already addressable.
    try:
        profiles = kb.list_profiles_on_disk()
    except Exception:
        profiles = []
    if profiles:
        print(f"Discovered {len(profiles)} profile(s) on disk; any of these can be an --assignee:")
        for name in profiles:
            print(f"  {name}")
    else:
        print("No profiles found under ~/.hermes/profiles/.\n"
              "Create one with `hermes -p <name> setup` before assigning tasks.")
    print(
        "\nNext step: start the gateway so ready tasks actually get picked up.\n"
        "  hermes gateway start\n\n"
        "The gateway hosts an embedded dispatcher that ticks every 60 seconds\n"
        "by default (config: kanban.dispatch_interval_seconds). Without a\n"
        "running gateway, tasks stay in 'ready' forever."
    )
    return 0


def _cmd_heartbeat(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        ok = kbd.heartbeat_worker(conn, args.task_id, note=getattr(args, "note", None),
                                 expected_run_id=_worker_run_id_for(args.task_id))
    return _ok_or_err(ok, f"cannot heartbeat {args.task_id} (not running?)",
                      f"Heartbeat recorded for {args.task_id}")


def _print_overlap_refusal(args: argparse.Namespace, refused: str) -> int:
    return kanban_overlap.print_cli(refused, as_json=bool(getattr(args, "json", False)))


@publication_scope
def _cmd_create(args: argparse.Namespace) -> int:
    from agent.delegation_context import is_dispatcher_owned_worker_context
    from hermes_cli.kanban_cli_body import read_body

    body, body_error = read_body(args)
    if body_error:
        return _err(body_error, 2)

    from hermes_cli.kanban_selfwork import SelfWorkError, contract
    from hermes_cli.kanban_model_neutral import validate_selector, validate_text
    try:
        validate_selector(getattr(args, "model_override", None), getattr(args, "provider_override", None),
                          mandatory=getattr(args, "pin_mandatory", False))
        validate_text(args.title, body)
        self_work = contract(getattr(args, "self_work", None))
        ws_kind, ws_path = _parse_workspace_flag(args.workspace)
        branch_name = _parse_branch_flag(getattr(args, "branch", None))
    except (argparse.ArgumentTypeError, ValueError) as exc:
        return _err(f"kanban: {exc}", 2)
    if branch_name and ws_kind != "worktree":
        return _err("kanban: --branch is only valid with --workspace worktree", 2)
    try:
        max_runtime = _parse_duration(getattr(args, "max_runtime", None))
    except ValueError as exc:
        return _err(f"kanban: --max-runtime: {exc}", 2)
    max_retries = getattr(args, "max_retries", None)
    if max_retries is not None and max_retries < 1:
        return _err(f"kanban: --max-retries must be >= 1 (got {max_retries}); "
                    "use 1 to trip on the first failure.", 2)
    in_worker = bool(os.environ.get("HERMES_KANBAN_TASK")) and is_dispatcher_owned_worker_context()
    if self_work is not None:
        from hermes_cli.kanban_selfwork_outbound import cli_command
        return cli_command(args, body=body, workspace_kind=ws_kind, workspace_path=ws_path,
                           max_runtime=max_runtime, branch_name=branch_name)
    # An agent shelling out to the CLI from a chat (terminal / execute_code) owns the
    # card just like the kanban_create tool would: stamp its session so the Desktop
    # session's task list shows it. Workers' cards inherit their creator task's session.
    # The desktop/TUI key (HERMES_SESSION_KEY) is what the session's notify poller
    # listens on, so it wins over HERMES_SESSION_ID; plain shells have neither. [daveey]
    from hermes_cli.kanban_cli_body import session_identity
    session_key, session_id = session_identity(in_worker)
    idem_key = getattr(args, "idempotency_key", None)
    # [daveey] Bind cards made for a chat on another gateway ($HERMES_ORIGIN_SESSION / --origin-session),
    # validated before the card exists. See hermes_cli/kanban_origin.py.
    from hermes_cli import kanban_origin
    try:
        origin = kanban_origin.resolve(getattr(args, "origin_session", None))
    except kanban_origin.OriginError as exc:
        return _err(f"kanban: {exc.code}: {exc}", 2)
    origin_binding = None
    # [daveey] Hub admission (a model call) runs before any connection/write lock.
    from hermes_cli.kanban_hub_admission_placement import cli_create_fields, cli_preflight, cli_report
    assignee = (args.assignee or (_profile_author() if in_worker or _agent_invoked() else None)
                or _cli_default_assignee(args))
    plan, refused_rc = cli_preflight(args, body, ws_kind, ws_path, getattr(args, "board", None)
                                     or kb.get_current_board(), assignee=assignee)
    if refused_rc is not None:
        return refused_rc
    with kbc.connect_closing() as conn:
        # An --idempotency-key hit returns an existing card: never re-stamp/re-subscribe it.
        idem_hit = bool(idem_key) and conn.execute(
            "SELECT 1 FROM tasks WHERE idempotency_key = ? AND status != 'archived' LIMIT 1",
            (idem_key,)).fetchone() is not None
        # [daveey] Refuse a second owner, exactly as the kanban_create tool does (hermes_cli/kanban_overlap.py):
        # open cards on every board/gateway, skipping parents, the creator's own fan-out and its key family.
        from hermes_cli import kanban_overlap
        board = getattr(args, "board", None) or kb.get_current_board()
        refused, after_create = kanban_overlap.check(
            conn, {"body": body, "idempotency_key": idem_key, "assignee": getattr(args, "assignee", None),
                   "board": None if board == kb.DEFAULT_BOARD else board,
                   "distinct_from": getattr(args, "distinct_from", None) or [],
                   "distinct_reason": getattr(args, "distinct_reason", None)},
            courier=False, title=str(args.title).strip(), session_id=session_id,
            self_tid=os.environ.get("HERMES_KANBAN_TASK") if in_worker else None,
            parents=list(args.parent or ()), entry="cli")
        if refused:
            return _print_overlap_refusal(args, refused)
        from hermes_cli.kanban_db_workspace import WorktreeWorkspaceConfigError
        from hermes_cli.kanban_capabilities import CapabilityMismatchError
        from hermes_cli.kanban_pin_provider import PinProviderUnresolvedError
        from hermes_cli.kanban_model_assignment import ModelProviderMismatchError
        from hermes_cli.kanban_skill_check import UnknownSkillError
        from hermes_cli.kanban_capabilities import RequiresDeclarationMissing, declaration_required
        from hermes_cli.kanban_worker_node import MachinePinRefused, check_machine_pins
        from hermes_cli.kanban_ask_resolved import AskFilingRefused, check_create_filing, create_context
        from hermes_cli.kanban_admission_class import AdmissionMetadataError
        from hermes_cli.kanban_create_authority import CreateAuthorityRefused
        try:
            # [daveey] A blocked ask's resolved_check runs once first, as the sweep will run it for this assignee
            # and workspace: already met or broken refuses the create. A key hit returns the card: no probe.
            check_create_filing(getattr(args, "human_request", None),
                                None if idem_hit else getattr(args, "initial_status", None),
                                context=create_context(conn, ws_path, assignee))
            check_machine_pins(conn, getattr(args, "runs_on", None))  # explicit pins need pickup [daveey]
            task_id = kb.create_task(
                conn, title=args.title, body=body,
                assignee=assignee,
                session_id=session_id,
                chat_origin=origin.ref if origin else getattr(args, "origin_session", None),
                # [daveey] Same declaration gate as the kanban_create tool, for agent/worker shells only.
                require_declaration=(in_worker or _agent_invoked()) and declaration_required(),
                created_by=_lane_creator(args, args.created_by or _profile_author()),
                workspace_kind=ws_kind, workspace_path=ws_path, branch_name=branch_name,
                project_id=getattr(args, "project", None), tenant=args.tenant, priority=args.priority,
                admission_class=getattr(args, "admission_class", "normal"),
                admission_frees=getattr(args, "admission_frees", None),
                self_work=self_work,
                parents=tuple(args.parent or ()), triage=bool(getattr(args, "triage", False)),
                idempotency_key=idem_key,
                max_runtime_seconds=max_runtime, skills=getattr(args, "skills", None) or None,
                max_retries=max_retries, model_override=getattr(args, "model_override", None),
                provider_override=getattr(args, "provider_override", None), pin_mandatory=bool(getattr(args, "pin_mandatory", False)),
                goal_mode=bool(getattr(args, "goal_mode", False)), review_required=bool(getattr(args, "review_required", False)),
                goal_max_turns=getattr(args, "goal_max_turns", None),
                reasoning_effort=getattr(args, "reasoning_effort", None),
                completion_contract=getattr(args, "completion_contract", None),
                initial_status=getattr(args, "initial_status", "running"),
                human_request=getattr(args, "human_request", None),
                creator_task_id=(os.environ.get("HERMES_KANBAN_TASK")
                                 if in_worker else None),
                validate_workspace=True, validate_skills=True, placement_checked=True, board=board,
                require_scope_authority=in_worker or _agent_invoked(),  # agents need their own chat
                # a human terminal's card is owned by the standing "terminal" owner chat (or its script's producer)
                owner_producer=None if in_worker or _agent_invoked() else (_declared_producer() or TERMINAL_OWNER),
                # [daveey] no --runs-on: queue for the worker pool; hub admission's placement and receipt
                **cli_create_fields(conn, plan, args, body, ws_kind, ws_path, board, assignee=assignee),
                insert_guard=after_create.guard,  # re-checks overlap under the write lock [daveey]
            )
        except kanban_overlap.OverlapRefused as exc:  # landed under the write lock after the first check
            return _print_overlap_refusal(args, exc.settle())
        except WorktreeWorkspaceConfigError as exc:
            return _err(f"kanban: --workspace: {exc}", 2)
        except (CapabilityMismatchError, UnknownSkillError, RequiresDeclarationMissing, MachinePinRefused,
                AskFilingRefused, CreateAuthorityRefused) as exc:
            return _err(f"kanban: {exc.code}: {exc}", 2)
        except (PinProviderUnresolvedError, ModelProviderMismatchError, AdmissionMetadataError, SelfWorkError) as exc:
            return _err(f"kanban: {exc}", 2)
        if not idem_hit:
            after_create(conn, task_id)  # records an accepted --distinct-from override on the new card
        if session_key and not idem_hit:
            # Same completion subscription the kanban_create tool adds, so the chat
            # that shelled out hears when the card lands (and its row stays busy).
            from tools.kanban_tools import _maybe_auto_subscribe
            _maybe_auto_subscribe(conn, task_id)
        if origin is not None:  # idempotent, so a retried create still (re)binds
            origin_binding = kanban_origin.bind(conn, task_id, origin, board=getattr(args, "board", None))
        placement = None
        if getattr(args, "requires", None) and not idem_hit:
            # Placement preflight: declared gateway needs this gateway lacks are owned first. [daveey]
            from hermes_cli.kanban_capability_routing import preflight
            placement = preflight(conn, task_id, board=getattr(args, "board", None))
        task = kb.get_task(conn, task_id)
    from hermes_cli.kanban_placement_diagnostics import cli_creation, cli_json_fields, print_cli_creation
    creation_placement = cli_report(cli_creation(args, body, ws_kind, ws_path, board, task, skip=idem_hit), plan)
    contract_extra = _contract_cli.create_warning_fields(task, skip=idem_hit, stderr=True)
    unlinked_warning = None
    if not in_worker and not session_id and not idem_hit and _agent_invoked():
        # An agent shelled out but no chat could be stamped (unknown/missing session env:
        # ssh to another host, `env -i`, another profile...). Say so loudly: an unlinked card
        # silently never shows in the chat's task strip or sidebar badge. [daveey]
        unlinked_warning = (
            f"{task_id} is not linked to any chat (no known HERMES_SESSION_KEY/ID in this "
            "environment), so it won't show in the chat's task strip. Create cards with the "
            "kanban_create tool instead, or link it to the chat: "
            f"hermes kanban notify-subscribe {task_id} --platform tui --chat-id <session_id>")
        print(f"⚠  {unlinked_warning}", file=sys.stderr)
    if getattr(args, "json", False):
        out = _task_to_dict(task)
        out.update(cli_json_fields(creation_placement))
        if unlinked_warning:  # also in the payload: run_slash returns only stdout for --json
            out["warning"] = unlinked_warning
        out.update(contract_extra)
        if origin_binding:
            out["origin_binding"] = origin_binding
        if placement:
            out["capability_placement"] = placement
        _print_json(out)
    else:
        print(f"Created {task_id}  ({task.status}, assignee={task.assignee or '-'})")
        print_cli_creation(creation_placement)
        for r in (placement or {}).get("routes") or []:
            print(f"Pre-routed {r['need']}: owner card requested on {r['gateway']} "
                  f"(hermes-hub request {r['request_id']}); {task_id} waits on it")
        if placement and placement.get("warning"):
            print(f"⚠  {placement['warning']}", file=sys.stderr)
        if origin_binding:
            print(f"Bound to {origin_binding['origin']} (readback pending: on the origin gateway run "
                  f"`{origin_binding['verify_visible']}`)")
        # Warn only for ready+assigned tasks that would sit without a dispatcher (triage/todo idle
        # by design, unassigned can't dispatch); skipped under --json so stdout stays parseable.
        if task.status == "ready" and task.assignee:
            running, message = _check_dispatcher_presence()
            if not running and message:
                print(f"\n⚠  {message}", file=sys.stderr)
    return 0


def _cmd_swarm(args: argparse.Namespace) -> int:
    try:
        workers = [ks.parse_worker_arg(raw) for raw in (args.worker or [])]
    except ValueError as exc:
        return _err(f"kanban swarm: {exc}", 2)
    if not workers:
        return _err("kanban swarm: at least one --worker is required", 2)
    with kbc.connect_closing() as conn:
        created = ks.create_swarm(
            conn, goal=args.goal, workers=workers, verifier_assignee=args.verifier,
            synthesizer_assignee=args.synthesizer, tenant=args.tenant,
            created_by=_lane_creator(args, args.created_by or _profile_author()), priority=args.priority,
            idempotency_key=getattr(args, "idempotency_key", None),
        )
    if getattr(args, "json", False):
        _print_json(created.as_dict())
    else:
        print(f"Swarm root: {created.root_id}\n"
              "Workers: " + ", ".join(created.worker_ids) + "\n"
              f"Verifier: {created.verifier_id}\n"
              f"Synthesizer: {created.synthesizer_id}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    assignee = args.assignee
    if args.mine and not assignee:
        assignee = _profile_author()
    with kbc.connect_closing() as conn:
        # Cheap mini-dispatch so list reflects dependencies cleared since the last tick. It is a
        # write, so a fenced worker/delegate shell skips it: listing must not fail there. [daveey]
        with contextlib.suppress(PermissionError):
            kb.recompute_ready(conn)
        filters = dict(
            assignee=assignee, status=args.status, tenant=args.tenant, session_id=args.session,
            include_archived=args.archived, order_by=getattr(args, "sort", None),
            workflow_template_id=args.workflow_template_id, current_step_key=args.current_step_key,
        )
        tasks = kb.list_tasks(conn, **filters)
        remote = _shadow_view.remote_tasks(conn, **filters)  # [daveey] a shadow board's other gateways' cards, read-only
    combined = _shadow_view.ordered([(t, None) for t in tasks] + remote, getattr(args, "sort", None))
    if _json_out(args, [_shadow_view.mark(_task_to_dict(t), home) for t, home in combined]):
        return 0
    # Passive discoverability: only multi-board users see which board this is.
    try:
        all_boards = kb.list_boards(include_archived=False)
    except Exception:
        all_boards = []
    if len(all_boards) > 1:
        other_count = len(all_boards) - 1
        print(f"Board: {kb.get_current_board()} ({other_count} other board{'s' if other_count != 1 else ''} — "
              f"`hermes kanban boards list`)\n")
    if not tasks and not remote:
        print("(no matching tasks)")
        return 0
    for t, home in combined:
        suffix = f"  [remote:{home}, read-only]" if home is not None else ""
        print(f"{_fmt_task_line(t)}{suffix}")
    return 0


def _cmd_wait_check(args: argparse.Namespace) -> int:
    from hermes_cli import kanban_wait_check
    return kanban_wait_check.run(args)


def _cmd_wait_comment(args: argparse.Namespace) -> int:
    from hermes_cli import kanban_comment_origin
    return kanban_comment_origin.run_wait_comment(args)


def _cmd_capabilities(args: argparse.Namespace) -> int:
    from hermes_cli import kanban_capability_registry
    return kanban_capability_registry.run_cli(args)


def _cli_route_needs(op: str, args: argparse.Namespace, reason: str) -> Optional[int]:
    """``--need`` on block/schedule: route to the gateway holding the capability. [daveey]"""
    needs = getattr(args, "needs", None)
    if not needs:
        return None
    from hermes_cli import kanban_capability_routing as kcr
    ids = _bulk_ids(args)
    if len(ids) != 1:
        return _err(f"kanban {op}: --need routes one card at a time", 2)
    if getattr(args, "human_request", None) is not None:
        return _err(f"kanban {op}: --need routes to an agent; --human-request asks David. Pass one.", 2)
    if getattr(args, "wake_check", None) or getattr(args, "clock_only", False):
        return _err(f"kanban {op}: --need parks on the owner card's own probe; drop --wake-check/--clock-only", 2)
    tid = ids[0]
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        if task is not None and task.goal_mode:
            return _err(f"kanban {op}: goal_mode tasks cannot park on an owner card", 2)
        try:
            from agent.redact import redact_sensitive_text
            res = kcr.route(conn, tid, needs, reason=redact_sensitive_text(reason or "", force=True),
                            expected_run_id=_worker_run_id_for(tid))
        except kcr.CapabilityRouteError as exc:
            return _err(f"kanban {op}: {exc}", 2)
        except ValueError as exc:
            return _err(f"kanban {op}: {exc}", 2)
    for r in res["routes"]:
        print(f"{tid} → scheduled: owner card for {r['need']} requested on {r['gateway']} "
              f"(hermes-hub request {r['request_id']})")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    rsk, rc = _run_state_kwargs(args, "show")
    if rc:
        return rc
    graph = None
    want_json = getattr(args, "json", False)
    with kbc.connect_closing() as live, contextlib.ExitStack() as _scope:
        task, conn, remote_home = _shadow_view.open_card(live, _scope, args.task_id)  # [daveey] live, else a shadow replica's
        if not task:
            from hermes_cli.kanban_pickup_stall import not_here_note
            return _err(f"no such task: {args.task_id}{not_here_note(conn, args.task_id)}")
        comments = kb.list_comments(conn, args.task_id)
        events = kb.list_events(conn, args.task_id)
        from hermes_cli.kanban_artifact_receipt import latest_receipt, format_receipt
        custody = latest_receipt(events)
        parents = kb.parent_ids(conn, args.task_id)
        children = kb.child_ids(conn, args.task_id)
        runs = kb.list_runs(conn, args.task_id, **rsk)
        # Workers hand off via task_runs.summary; tasks.result stays NULL unless set.
        latest_summary = kb.latest_summary(conn, args.task_id)
        from hermes_cli import kanban_wake_check as kwc
        wake_check = kwc.get_state(conn, args.task_id)
        from hermes_cli import kanban_wake_summary as kws  # [daveey] what wakes a scheduled card, at latest when
        wakes = kws.wakes(conn, task, wake_check=wake_check, check_loaded=True)
        from hermes_cli import kanban_queue_reason as kqr
        queue_reason = kqr.latest(conn, args.task_id)
        # Same ``queue`` as kanban_show and the REST read (kanban-7d720d4ab8): a ready card says where it waits. [daveey]
        from hermes_cli import kanban_queue_position as kqp, kanban_hub_admission_placement as _hub_admission_show
        details = _shadow_view.show_details(conn, task, getattr(args, "board", None), remote_home)
        queue = details.get("queue")
        if not want_json:
            graph = kb.task_graph_context(conn, task.id)

    if want_json:
        _print_json({
            "task": _shadow_view.mark(_task_to_dict(task), remote_home), "latest_summary": latest_summary, "parents": parents, "children": children,
            **custody,
            "comments": [_obj_dict(c, ("author", "body", "created_at")) for c in comments],
            "events": [_obj_dict(e, ("kind", "payload", "created_at", "run_id")) for e in events],
            "runs": [_obj_dict(r, _SHOW_RUN_FIELDS) for r in runs],
            "wake_check": wake_check, "wakes": wakes,
            "queue_reason": queue_reason,
            **details,
            **_hub_admission_show.show_json(events),
        })
        return 0

    def field(label: str, value) -> None:
        print(f"  {label + ':':<11}{value}")

    print(f"Task {task.id}: {task.title}")
    field("status", kws.status_text(task.status, wakes))
    from hermes_cli.kanban_actionability_evidence import render as render_actionability
    field("actionability", render_actionability(details["actionability"]))
    _shadow_view.print_remote(field, remote_home)
    field("assignee", task.assignee or "-")
    kqr.print_field(queue_reason, field)
    field("capacity", details["capacity_note"])
    kqp.print_field(queue, queue_reason, field)
    if task.tenant:
        field("tenant", task.tenant)
    field("workspace", f"{task.workspace_kind}" + (f" @ {task.workspace_path}" if task.workspace_path else ""))
    if task.branch_name:
        field("branch", task.branch_name)
    if task.skills:
        field("skills", ", ".join(task.skills))
    if task.model_override:
        _prov = f" (provider: {task.provider_override})" if task.provider_override else ""
        field("model", f"{task.model_override}{_prov}")
    _print_max_retries(task)
    field("created", f"{_fmt_ts(task.created_at)} by {task.created_by or '-'}")
    _hub_admission_show.print_show_field(events, field)  # [daveey] why this card is (or is not) on the hub

    # Diagnostics up top so CLI users see distress signals before scrolling.
    from hermes_cli import kanban_diagnostics as kd
    diags = kd.compute_task_diagnostics(task, events, runs, graph=graph, wake_check=wake_check)
    if diags:
        print(f"\n  Diagnostics ({len(diags)}):")
        _print_diagnostics(diags, "    ", with_kind=False)
    if wake_check:
        field("wake check", kwc.describe_state(wake_check))
    if task.started_at:
        field("started", _fmt_ts(task.started_at))
    if task.completed_at:
        field("completed", _fmt_ts(task.completed_at))
    if parents:
        field("parents", ", ".join(parents))
    if children:
        field("children", ", ".join(children))
    if task.body:
        _print_section("Body:", [task.body])
    if task.result:
        _print_section("Result:", [task.result])
    elif latest_summary:
        _print_section("Latest summary:", [latest_summary])
    if comments:
        _print_section(f"Comments ({len(comments)}):",
                       (f"  [{_fmt_ts(c.created_at)}] {c.author}: {c.body}" for c in comments))
    print(format_receipt(custody), end="")
    if events:
        _print_section(f"Events ({len(events)}):", (
            f"  [{_fmt_ts(e.created_at)}]{f' [run {e.run_id}]' if e.run_id else ''} {e.kind}"
            f"{f' {e.payload}' if e.payload else ''}" for e in events[-20:]))
    if runs:
        print()
        print(f"Runs ({len(runs)}):")
        for r in runs:
            # Clamp to 0 so NTP backward-jumps don't print negative seconds.
            elapsed = max(0, r.ended_at - r.started_at) if r.ended_at else None
            el = f"{elapsed}s" if elapsed is not None else "active"
            outcome = r.outcome or r.status or "active"
            print(f"  #{r.id:<3} {outcome:<12} @{r.profile or '-'}  {el}  {_fmt_ts(r.started_at)}")
            if r.summary:
                print(f"        → {r.summary.splitlines()[0][:160]}")
            if r.error:
                print(f"        ! {r.error.splitlines()[0][:160]}")
    return 0


def _cmd_assign(args: argparse.Namespace) -> int:
    profile = _none_profile(args.profile)
    with kbc.connect_closing() as conn:
        ok = kb.assign_task(conn, args.task_id, profile)
    return _ok_or_err(ok, f"no such task: {args.task_id}",
                      f"Assigned {args.task_id} to {profile or '(unassigned)'}")


def _cmd_set_model(args: argparse.Namespace) -> int:
    model = args.model
    if model is not None and model.lower() in {"none", "-", "null", ""}:
        model = None
    provider = getattr(args, "provider", None)
    try:
        with kbc.connect_closing() as conn:
            from hermes_cli.kanban_pin_reroute import set_model_cli
            ok, wrote = set_model_cli(conn, args.task_id, args.model, model, provider, getattr(args, "mandatory", None))
    except (ValueError, RuntimeError) as exc:
        return _err(f"kanban: {exc}", 2)
    if not ok:
        return _err(f"no such task: {args.task_id}")
    if not wrote:
        print(f"{args.task_id}: model pin " + ("mandatory: a quota wall never moves it" if args.mandatory
                                               else "reroutable: a far quota wall may move it within its class"))
    elif model:
        label = f"{provider}:{model}" if provider else model
        print(f"Set model override on {args.task_id}: {label} (applies on next dispatch)")
    else:
        print(f"Cleared model override on {args.task_id} (worker uses its profile default)")
    return 0


def _cmd_reclaim(args: argparse.Namespace) -> int:
    from hermes_cli.kanban_core_update_requeue import reclaim_attribution
    reason = getattr(args, "reason", None)
    attribution = dict(urgency_source=getattr(args, "urgency_source", None),
                       interrupt_target=getattr(args, "interrupt_target", None))
    try:
        reclaim_attribution(reason, **attribution)
    except ValueError as exc:
        print(f"kanban reclaim: refused: {exc}", file=sys.stderr)
        return 2
    with kbc.connect_closing() as conn:
        ok = kb.reclaim_task(conn, args.task_id, reason=reason, **attribution)
    return _ok_or_err(ok, f"cannot reclaim {args.task_id} (not running or unknown id)",
                      f"Reclaimed {args.task_id}")


def _cmd_move(args: argparse.Namespace) -> int:
    """[daveey] ``hermes kanban move``: the card's own worker (HERMES_KANBAN_TASK + run id) moves its running card;
    otherwise a queued card is relabelled, or with --reclaim a card running here is stopped first."""
    from hermes_cli import kanban_move as km, kanban_move_pool as kmp
    labels, host, err = kmp.cli_labels(args)
    if labels is None:
        print(err, file=sys.stderr)
        return 1
    own_run = os.environ.get("HERMES_KANBAN_TASK") == args.task_id and os.environ.get("HERMES_KANBAN_RUN_ID")
    with kbc.connect_closing() as conn:
        status = (conn.execute("SELECT status FROM tasks WHERE id = ?", (args.task_id,)).fetchone() or {"status": None})
        if own_run and not (args.checkpoint or "").strip():
            res = {"ok": False, "reason_code": "checkpoint_required",
                   "error": "--checkpoint is required when a worker moves its own running card"}
        elif own_run:
            res = km.move_task(conn, args.task_id, runs_on=labels, reason=args.reason, checkpoint=args.checkpoint,
                               expected_run_id=int(os.environ["HERMES_KANBAN_RUN_ID"]), force=args.force, **host)
        elif status["status"] == "running":
            if not args.reclaim:
                res = {"ok": False, "reason_code": "running",
                       "error": f"{args.task_id} is running: its worker moves it (kanban_move), or pass --reclaim"}
            else:
                res = km.move_reclaim(conn, args.task_id, runs_on=labels, reason=args.reason, force=args.force, **host)
        else:
            res = km.move_queued(conn, args.task_id, runs_on=labels, reason=args.reason,
                                 checkpoint=args.checkpoint or "", force=args.force, **host)
    if getattr(args, "json", False):
        print(json.dumps(res))
    elif res.get("ok"):
        print(f"Moved {args.task_id} to {res['runs_on']} (eligible: {', '.join(res['matches'])}; "
              f"move {res['move_no']}; now {res['status']})")
        if res.get("note"):
            print(res["note"])
    else:
        print(f"kanban move: {res.get('error')} [{res.get('reason_code')}]", file=sys.stderr)
    return 0 if res.get("ok") else 1

def _cmd_reassign(args: argparse.Namespace) -> int:
    profile = _none_profile(args.profile)
    reclaim = bool(getattr(args, "reclaim", False))
    with kbc.connect_closing() as conn:
        ok = kb.reassign_task(conn, args.task_id, profile, reclaim_first=reclaim, reason=getattr(args, "reason", None))
    return _ok_or_err(
        ok,
        f"cannot reassign {args.task_id} (unknown id, or still running — pass --reclaim to release first)",
        f"Reassigned {args.task_id} to {profile or '(unassigned)'}" + (" (claim reclaimed)" if reclaim else ""),
    )


def _rows_by_task(conn, table: str, ids: list[str]) -> dict[str, list]:
    """``{task_id: [rows ordered by id]}`` for every id (empty list when none)."""
    by = {i: [] for i in ids}
    placeholders = ",".join(["?"] * len(ids))
    for row in conn.execute(f"SELECT * FROM {table} WHERE task_id IN ({placeholders}) ORDER BY id", tuple(ids)):
        by.setdefault(row["task_id"], []).append(row)
    return by


def _cmd_diagnostics(args: argparse.Namespace) -> int:
    """List active diagnostics on the board via the same rule engine the dashboard uses."""
    from hermes_cli import kanban_diagnostics as kd
    # Honour kanban.default_assignee as the fallback for unassigned ready tasks (#27145),
    # kanban.max_in_progress as the global concurrency cap (#33488), kanban.max_in_progress_per_profile as
    # the per-profile cap (#21582), and kanban.max_spawn as the per-tick spawn limit (#28805). Same
    # semantics as the gateway dispatch path so behavior matches whether the user runs the CLI directly or
    # relies on the gateway-embedded dispatcher.
    from hermes_cli.config import load_config

    diag_config = kd.config_from_runtime_config(load_config())

    with kbc.connect_closing() as conn:
        # Either one-task mode or fleet mode.
        if getattr(args, "task", None):
            task = kb.get_task(conn, args.task)
            if task is None:
                return _err(f"no such task: {args.task}")
            from hermes_cli import kanban_wake_check as kwc
            diags_by_task = {args.task: kd.compute_task_diagnostics(
                task, kb.list_events(conn, args.task), kb.list_runs(conn, args.task),
                graph=kb.task_graph_context(conn, args.task), config=diag_config,
                wake_check=kwc.get_state(conn, args.task))}
        else:
            # Fleet mode: pull all non-archived tasks + their events/runs.
            rows = list(conn.execute("SELECT * FROM tasks WHERE status != 'archived'").fetchall())
            ids = [r["id"] for r in rows]
            diags_by_task = {}
            if ids:
                ev_by = _rows_by_task(conn, "task_events", ids)
                run_by = _rows_by_task(conn, "task_runs", ids)
                graph_by = kb.task_graph_contexts(conn, ids)
                from hermes_cli import kanban_wake_check as kwc
                wake_by = kwc.load_states(conn)
                for r in rows:
                    tid = r["id"]
                    dl = kd.compute_task_diagnostics(r, ev_by.get(tid, []), run_by.get(tid, []),
                                                     graph=graph_by.get(tid), config=diag_config,
                                                     wake_check=wake_by.get(tid))
                    if dl:
                        diags_by_task[tid] = dl

        sev = getattr(args, "severity", None)
        if sev:
            floor = kd.SEVERITY_ORDER.index(sev)
            diags_by_task = {tid: kept for tid, dl in diags_by_task.items()
                             if (kept := [d for d in dl if kd.SEVERITY_ORDER.index(d.severity) >= floor])}

        # Map task_id → title/status/assignee for the table output.
        meta: dict[str, dict] = {}
        if diags_by_task:
            placeholders = ",".join(["?"] * len(diags_by_task))
            for r in conn.execute(f"SELECT id, title, status, assignee FROM tasks WHERE id IN ({placeholders})",
                                  tuple(diags_by_task.keys())):
                meta[r["id"]] = {k: r[k] for k in ("title", "status", "assignee")}

    # What this home believes it may claim on a shared board (#113620).
    allowlist = kbd.dispatch_profile_allowlist_summary()

    if getattr(args, "json", False):
        # Per-task rows unchanged; the home-scope allowlist rides as a trailing row
        # (task_id null) so existing `payload[0]["diagnostics"]` consumers keep working.
        _print_json([{"task_id": tid, **meta.get(tid, {}), "diagnostics": [d.to_dict() for d in dl]}
                     for tid, dl in diags_by_task.items()]
                    + [{"task_id": None, "dispatch_profiles": allowlist, "diagnostics": []}])
        return 0

    print(f"kanban.dispatch_profiles: {allowlist}")
    if not diags_by_task:
        print("No active diagnostics on this board.")
        return 0

    total = sum(len(dl) for dl in diags_by_task.values())
    print(f"{total} active diagnostic(s) across {len(diags_by_task)} task(s):\n")
    for tid, dl in diags_by_task.items():
        m = meta.get(tid, {})
        print(f"  {tid}  {m.get('status') or '?':8s}  @{m.get('assignee') or '(unassigned)':18s}  "
              f"{m.get('title') or '(untitled)'}")
        _print_diagnostics(dl, "    ", with_kind=True)
        print()
    return 0


def _cmd_link(args: argparse.Namespace) -> int:
    # A worker linking its own running card (dependency-block handoff) proves
    # ownership with its run id; linking a foreign task never needs one.
    expected_child_run_id = (
        _worker_run_id_for(args.child_id)
        if args.child_id == os.environ.get("HERMES_KANBAN_TASK") else None)
    with kbc.connect_closing() as conn:
        gated = kb.link_tasks(conn, args.parent_id, args.child_id,
                              expected_child_run_id=expected_child_run_id)
        from hermes_cli.kanban_board_delivery import cli_lines
        queued = cli_lines(conn)  # [daveey] the stopped Board sync keeps it here
    print(f"Linked {args.parent_id} -> {args.child_id}", *queued, sep="\n")
    if gated:
        print(
            f"Note: {args.child_id} was ready and is now todo — parent "
            f"{args.parent_id} is not done yet. The ready -> running claim "
            f"re-checks parents, so the child only runs after the parent "
            f"completes; use `hermes kanban unlink {args.parent_id} {args.child_id}` "
            f"to run it now."
        )
    return 0


def _cmd_unlink(args: argparse.Namespace) -> int:
    env_tid = os.environ.get("HERMES_KANBAN_TASK")  # [daveey] same own-card rule as the kanban_unlink tool
    if env_tid and env_tid not in (args.parent_id, args.child_id):
        return _err(f"unlink refused: worker {env_tid} may only unlink its own card's edges; use kanban_supervisor_request")
    with kbc.connect_closing() as conn:
        ok = kb.unlink_tasks(conn, args.parent_id, args.child_id)
        from hermes_cli.kanban_board_delivery import cli_lines
        queued = cli_lines(conn) if ok else []  # [daveey] the stopped Board sync keeps it here
    return _ok_or_err(ok, f"No such link: {args.parent_id} -> {args.child_id}",
                      "\n".join([f"Unlinked {args.parent_id} -> {args.child_id}", *queued]))


def _cmd_supervisor_requests(args: argparse.Namespace) -> int:
    from hermes_cli.kanban_supervisor_requests import cli_command  # [daveey]
    return cli_command(args)


def _cmd_comment(args: argparse.Namespace) -> int:
    body = " ".join(args.text).strip()
    if args.max_len is not None:
        if args.max_len < 1:
            return _err("kanban: --max-len must be positive", 2)
        if len(body) > args.max_len:
            suffix = f"\n\n[trimmed to {args.max_len} chars by --max-len]"
            body = body[: max(0, args.max_len - len(suffix))].rstrip() + suffix
    author = args.author or _profile_author()
    # [daveey] /kanban (run_slash) is typed into a chat composer: human. The CLI is human only when
    # no process in its ancestry started with an agent-harness marker (kanban_comment_writer).
    if getattr(args, "_slash_typed", False):
        writer_kind, writer_session = "human", None
    else:
        from hermes_cli.kanban_comment_writer import cli_comment_writer
        writer_kind, writer_session, _evidence = cli_comment_writer(author=author)
    with kbc.connect_closing() as conn:
        try:
            from hermes_cli.kanban_foreign_comment import append
            uid = append(conn, args.task_id, author, body, writer_kind=writer_kind,
                         writer_session=writer_session, origin_gateway=getattr(args, "origin_gateway", None))
            if uid:
                print(f"Comment added to {args.task_id} (canonical comment {uid}). "
                      "Normal sync delivers it to the home and replica; read show after sync for its local ID.")
                return 0
            kb.add_comment(conn, args.task_id, author, body,
                           origin_gateway=getattr(args, "origin_gateway", None),
                           writer_kind=writer_kind, writer_session=writer_session)
        except ValueError as exc:
            return _err(f"kanban comment: {exc}", 2)
        live = kb.live_run_id(conn, args.task_id)
        from hermes_cli.kanban_comment_origin import comment_wake_note  # [daveey] parked reader, unmatched note
        wake_note = comment_wake_note(conn, args.task_id, author, body,
                                      origin_gateway=getattr(args, "origin_gateway", None))
        from hermes_cli.kanban_comment_liveness import EXITED_NOTE, worker_exited
        exited = live is not None and worker_exited(conn, args.task_id, live)  # [daveey] no reader left
        from hermes_cli.kanban_board_delivery import cli_lines, remote_runner
        queued = cli_lines(conn, args.task_id)  # [daveey] the stopped Board sync keeps it here
        remote = bool(queued) and remote_runner(conn, args.task_id) is not None
    print(f"Comment added to {args.task_id}")
    own_run = os.environ.get("HERMES_KANBAN_TASK") == args.task_id
    if exited and not own_run:
        print(EXITED_NOTE[0].upper() + EXITED_NOTE[1:] + ".")
    elif live is not None and not own_run and not remote:
        print("The card has a live worker run: it gets this note at its next model call.")
    if wake_note and not own_run:
        print(wake_note)
    for line in queued:
        print(line)
    return 0


def _cmd_attach(args: argparse.Namespace) -> int:
    """Attach a local file via the shared ``store_attachment_bytes`` path (same 25 MB cap and name
    sanitisation as the dashboard upload and agent tool)."""
    import mimetypes
    _worker_run_id_for(args.task_id)

    src = Path(args.path).expanduser()
    if not src.is_file():
        return _err(f"kanban: no such file: {src}")
    data = src.read_bytes()
    name = args.name or src.name
    content_type = args.content_type or mimetypes.guess_type(name)[0]
    uploaded_by = args.author or _profile_author()
    try:
        with kbc.connect_closing() as conn:
            att_id = kb.store_attachment_bytes(conn, args.task_id, name, data, content_type=content_type,
                                               uploaded_by=uploaded_by)
    except kb.AttachmentTooLarge as exc:
        return _err(f"kanban: {exc}")
    print(f"Attached {name} to {args.task_id} (attachment {att_id}, {len(data)} bytes)")
    return 0


def _cmd_attachments(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        if kb.get_task(conn, args.task_id) is None:
            return _err(f"no such task: {args.task_id}")
        atts = kb.list_attachments(conn, args.task_id)
    if _json_out(args, [_obj_dict(a, _ATTACHMENT_FIELDS) for a in atts], ascii=True):
        return 0
    if not atts:
        print(f"No attachments on {args.task_id}")
        return 0
    print(f"Attachments on {args.task_id}:")
    for a in atts:
        ct = a.content_type or "-"
        print(f"  [{a.id}] {a.filename}  ({a.size} bytes, {ct}, by {a.uploaded_by or '-'})")
        print(f"        {a.stored_path}")
    return 0


def _cmd_attach_rm(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        removed = kb.delete_attachment(conn, args.attachment_id)
    if removed is None:
        return _err(f"no such attachment: {args.attachment_id}")
    print(f"Deleted attachment {args.attachment_id} ({removed.filename}) from {removed.task_id}")
    return 0


def _worker_run_id_for(task_id: str) -> Optional[int]:
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if env_tid and env_tid != task_id:
        raise ValueError(f"worker is scoped to task {env_tid}; refusing to mutate {task_id}")
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    if os.environ.get("HERMES_KANBAN_TASK") != task_id or not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _goal_mode_handoff_rejection(task: Optional[kb.Task], evidence: str,
                                 handoff_tool: str = "kanban_complete", *, verified=None):
    """Goal judge for every terminal worker handoff (including review).

    Returns ``(verdict, reason_or_None)``: ``"done"`` allows; ``"blocked"`` = judge ruled the goal
    unachievable; ``"continue"``/``"wait"`` reject with the judge's reason. Judge failures allow
    the handoff (logged).

    See #100954.
    ``{"done", None}`` means the judge allows the handoff; anything else is a rejection whose verdict
    disambiguates the guidance the caller gives the worker (``continue`` = not done yet, ``blocked`` =
    judged unachievable — see #100954).
    """
    if task is None or not task.goal_mode:
        return ("done", None)
    try:
        from agent.auxiliary_client import get_text_auxiliary_client

        client, model = get_text_auxiliary_client("goal_judge")
    except Exception:
        client, model = None, None
    if client is None or not model:
        return ("done", None)

    from hermes_cli.goals import handoff_judge_goal, judge_goal

    verdict, reason, transport_failed = "done", "", False
    try:
        # Headless handoff checks run outside any agent turn: bind the per-task relay-affinity
        # scope (mirrors kanban_specify) so the relay does not reject the judge call (#113669).
        from agent.portal_tags import get_affinity_scope, reset_affinity_scope, set_affinity_scope
        affinity_token = None if get_affinity_scope() else set_affinity_scope(f"kanban:{task.id}")
        try:
            verdict, reason, parse_failed, _, transport_failed = judge_goal(
                goal=handoff_judge_goal(task.title, task.body, handoff_tool),
                last_response=evidence.strip())
        finally:
            if affinity_token is not None:
                reset_affinity_scope(affinity_token)
    except Exception as judge_exc:
        import logging as _logging

        _logging.getLogger(__name__).warning("goal judge check failed, allowing lifecycle handoff: %s",
                                             judge_exc, exc_info=True)
        return ("done", None)
    if transport_failed:
        # ``judge_goal`` fails open to ``continue`` on transport errors (relay 400, auth, timeout);
        # an unreachable judge is not a human "not done" and must not reject the handoff (#83610).
        import logging as _logging

        _logging.getLogger(__name__).warning("goal judge unreachable (%s), allowing lifecycle handoff", reason)
        return ("done", None)
    if verdict == 'blocked' and parse_failed:
        # An unparsed answer is not evidence that can authorize a recovery park.
        return ('continue', reason)
    if verdict == 'done' and not parse_failed and verified is not None and handoff_tool == 'kanban_complete':
        from hermes_cli.kanban_goal_receipt import GoalVerification
        verified.append(GoalVerification.for_handoff(task, evidence))
    return (verdict, None if verdict == "done" else reason)


def _goal_gate_error(conn, tid: str, evidence: str, handoff: str, blocked_hint: str,
                     continue_hint: str, *, verified=None) -> Optional[str]:
    """Goal-mode judge gate shared by ``complete`` / ``request-review`` (mirrors tools/kanban_tools.py);
    applied to every terminal handoff so request-review can't bypass it. Returns the error line, or
    None to allow."""
    tool = "kanban_request_review" if handoff.startswith("review") else "kanban_complete"
    task = kb.get_task(conn, tid)
    verdict, rejection = _goal_mode_handoff_rejection(task, evidence, tool, verified=verified)
    if verdict == "blocked":
        from hermes_cli.kanban_goal_recovery import park
        if park(conn, task, rejection, expected_run_id=_worker_run_id_for(tid)):
            return (f"kanban: goal {handoff} of {tid} rejected: {rejection}. "
                    "Parked in triage for bounded supervisor recovery; acceptance unchanged.")
        return (f"kanban: goal {handoff} of {tid} rejected: judge ruled "
                f"the goal unachievable — {rejection}. {blocked_hint}")
    if rejection is not None:
        return f"kanban: goal {handoff} of {tid} rejected by judge: {rejection}. {continue_hint}"
    return None


def _cmd_complete(args: argparse.Namespace) -> int:
    """Mark one or more tasks done. Supports a single id or a list."""
    from hermes_cli.kanban_containment_authority import AuthorityOpen
    ids, rc = _require_ids(args)
    if rc:
        return rc
    summary = getattr(args, "summary", None)
    raw_meta = getattr(args, "metadata", None)
    # Handoff fields are per-run; refuse to copy them across N runs.
    if len(ids) > 1 and (summary or raw_meta):
        return _err("kanban: --summary / --metadata are per-task and can't be used "
                    "with multiple ids (would apply the same handoff to every task). "
                    "Complete tasks one at a time, or drop the flags for the bulk close.", 2)
    metadata, rc = _parse_metadata_flag(raw_meta)
    if rc:
        return rc
    fail_msg: dict[str, str] = {}
    with kbc.connect_closing() as conn:
        def op(tid):
            verified = []
            gate_err = _goal_gate_error(
                conn, tid, (summary or args.result or "").strip(), "completion",
                "Re-scope with kanban edit, or record the block with kanban block instead of completing.",
                "Provide evidence matching the task's acceptance criteria.", verified=verified)
            if gate_err:
                fail_msg[tid] = gate_err
                return False
            fail_msg[tid] = f"cannot complete {tid} (unknown id or terminal state)"
            foreign = _foreign_card.refusal(conn, tid)  # [daveey] only a replica copy here: name the owner
            if foreign:
                fail_msg[tid] = f"cannot complete {tid}: {_foreign_card.REJECTION}: {foreign}"
                return False
            try:
                own_run, shell_why = _worker_run_id_for(tid), None
                if own_run is None:  # a worker's terminal shell: its run, once proven [daveey]
                    own_run, shell_why = _worker_shell.run_for(conn, tid)
                _review_contract.refuse_outside(conn, tid, metadata, own_run, worker_shell=shell_why)
                done = kb.complete_task(conn, tid, result=args.result, summary=summary, metadata=metadata,
                                        goal_verification=verified[0] if verified else None,
                                        expected_run_id=own_run,
                                        force=bool(getattr(args, "force", False)))
            except AuthorityOpen as authority_err:
                fail_msg[tid] = f"cannot complete {tid}: {authority_err}"
                return False
            except _review_contract.IndependenceRefused as outside:  # --force does not override it [daveey]
                fail_msg[tid] = f"cannot complete {tid}: {outside}"
                return False
            except kb.LiveClaimError:
                fail_msg[tid] = (f"cannot complete {tid}: a live worker is running it. Wait for the "
                                 f"worker, `hermes kanban reclaim {tid}` to release it, or re-run with "
                                 f"--force to close its run and complete anyway.")
                return False
            except kb.EmptyCompletionError as empty_err:
                fail_msg[tid] = (f"cannot complete {tid}: {empty_err}. Pass --result/--summary "
                                 f"describing what was done (an empty completion is not evidence).")
                return False
            except (kb.ArtifactPreservationError, ValueError) as receipt_err:  # incl. ReviewRequiredError
                fail_msg[tid] = receipt_refusal(tid, receipt_err)  # nothing written; re-raises other errors
                return False
            if not done:
                # complete_task returns bare False for a dependency refusal too;
                # name the open parents instead of claiming the id is unknown.
                blockers = kb.unsatisfied_parents(conn, tid)
                if blockers:
                    detail = ", ".join(f"{pid} ({status})" for pid, status in blockers)
                    fail_msg[tid] = (f"cannot complete {tid}: unsatisfied parent dependencies: {detail}; "
                                     f"complete the parents first, or `hermes kanban unlink <parent> {tid}`.")
                else:  # a PR-acceptance refusal names its reason, not "unknown id" [daveey]
                    fail_msg[tid] = _contract_cli.complete_refusal(conn, tid) or fail_msg[tid]
            return done

        return _bulk_apply(ids, op, lambda tid: f"Completed {tid}", fail_msg.__getitem__, conn=conn)


_OPERATOR_EDIT_FLAGS = ("max_retries", "goal_mode", "goal_max_turns", "max_runtime_seconds", "session_id", "review_required")


def _cmd_edit(args: argparse.Namespace) -> int:
    from hermes_cli.kanban_admission_class import AdmissionMetadataError
    from hermes_cli.kanban_requires_edit import edit_preflight  # --requires / --completion-contract / --reason
    admission = {name: getattr(args, name, None) for name in ("admission_class", "admission_frees")}
    if (rc := edit_preflight(args)) is not None:
        return rc
    reason = getattr(args, "reason", None)
    result = getattr(args, "result", None)
    raw_metadata = getattr(args, "metadata", None)
    summary = getattr(args, "summary", None)
    title = getattr(args, "title", None)
    body = getattr(args, "body", None)
    priority = getattr(args, "priority", None)
    priority_bump = getattr(args, "priority_bump", None)
    if priority_bump is not None and (priority is not None or priority_bump <= 0):
        return _err("--priority-bump must be positive and cannot be combined with --priority", 2)
    from hermes_cli.kanban_priority import bounded
    if priority is not None:
        priority = bounded(priority)
    operator = {name: getattr(args, name, None) for name in _OPERATOR_EDIT_FLAGS}
    operator.update({k: operator[k] == "on" for k in ("goal_mode", "review_required") if operator[k] is not None})
    has_operator = any(value is not None for value in operator.values())
    has_admission = any(value is not None for value in admission.values())
    if has_operator and has_admission:
        return _err("kanban edit: admission metadata cannot be combined with operator fields", 2)
    if result is None and (summary is not None or raw_metadata is not None):
        return _err("kanban edit: --summary and --metadata require --result", 2)
    if all(value is None for value in (title, body, priority, priority_bump, result)) and not (has_operator or has_admission):
        return _err(
            "kanban edit: provide --title, --body, --priority, --result, --max-retries, --goal-mode, "
            "--goal-max-turns, --max-runtime-seconds, --session-id, --review-required, or --completion-contract", 2,
        )
    if getattr(args, "force", False) and operator["session_id"] is None:
        return _err("kanban edit: --force only applies to --session-id", 2)
    metadata, rc = _parse_metadata_flag(raw_metadata)
    if rc:
        return rc
    with kbc.connect_closing() as conn:
        changed: dict = {}
        if (priority is not None or priority_bump is not None) and has_operator and not (reason or "").strip():
            # Refuse before the operator columns commit, so a refused lowering applies nothing;
            # edit_task re-checks inside its own write transaction. [daveey]
            row = conn.execute("SELECT priority FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
            requested = priority if priority is not None else bounded(int(row[0] or 0) + priority_bump) if row else None
            if row is not None and requested < int(row[0] or 0):
                return _err(f"kanban edit: {kb.PriorityLowerRefused(args.task_id, int(row[0] or 0), requested)}")
            refused = (kb.paced_raise_refusal(conn, args.task_id, int(row[0] or 0), requested)
                       if row is not None and requested is not None else None)
            if refused is not None:
                return _err(f"kanban edit: {refused}")
        if has_operator:
            # Operator columns first: a refusal (e.g. a session_id overwrite) must leave the
            # whole edit unapplied rather than half of it. [daveey]
            try:
                changed = kbo.edit_operator_fields(
                    conn, args.task_id, force=bool(getattr(args, "force", False)), **operator,
                )
            except kbo.KanbanEditRefused as exc:
                return _err(f"kanban edit: {exc}")
            if changed is None:
                return _err(f"cannot edit {args.task_id} (unknown id)")
        if has_admission or any(value is not None for value in (title, body, priority, priority_bump, result)):
            try:
                ok = kb.edit_task(
                    conn, args.task_id, title=title, body=body, priority=priority,
                    result=result, summary=summary, metadata=metadata, **admission,
                    priority_reason=reason, require_lower_reason=True, priority_bump=priority_bump,
                )
            except (kb.PriorityLowerRefused, kb.PriorityRaisePacedRefused, AdmissionMetadataError, ReceiptEditRefused) as exc:
                return _err(f"kanban edit: {exc}")
            if not ok:
                return _err(f"cannot edit {args.task_id} (unknown id, or --result used on a task that is not done)")
    detail = "; ".join(f"{f}: {c['old']!r} -> {c['new']!r}" for f, c in (changed or {}).items())
    print(f"Edited {args.task_id}" + (f" ({detail})" if detail else ""))
    return 0


def _cmd_reopen(args: argparse.Namespace) -> int:
    """``done`` -> ``ready`` (``todo`` while a parent is open) with an audited reason. [daveey]"""
    with kbc.connect_closing() as conn:
        try:
            status = kbo.reopen_done_task(conn, args.task_id, reason=args.reason, actor=_profile_author())
        except kbo.KanbanEditRefused as exc:
            return _err(f"kanban reopen: {exc}", 2)
        if status is None:
            held = kb.unblock_refusal(conn, args.task_id, verb="reopen")  # an archived stale copy: nothing revives it
            if held:
                return _err(held)
            current = kb.get_task(conn, args.task_id)
            what = "unknown id" if current is None else f"status is {current.status!r}, not 'done'"
            return _err(f"cannot reopen {args.task_id} ({what}); use unblock/reopen-review for other states")
        print(f"Reopened {args.task_id} -> {status}")
        _print_queued_local(conn)
    return 0


def _cmd_block(args: argparse.Namespace) -> int:
    if getattr(args, "classification_cas", None) is not None:
        if not getattr(args, "metadata_only", False):
            return _err("classification CAS requires --metadata-only", 2)
        from hermes_cli.kanban_human_request_migration import reclassify_cli_request
        return reclassify_cli_request(args, _joined_words(args.reason))
    from hermes_cli.kanban_cli_actor import cli_actor
    reason = _joined_words(args.reason)
    kind = getattr(args, "kind", None)
    routed = _cli_route_needs("block", args, reason)
    if routed is not None:
        return routed
    from hermes_cli.kanban_human_request import validate_human_request
    request = validate_human_request(getattr(args, "human_request", None), kind=kind)
    from hermes_cli.kanban_ask_resolved import filing_refusal, resolved_check
    refused_ask = None
    if resolved_check(request)[0]:  # [daveey] run per card before any ask is filed, as the sweep will run it
        with kbc.connect_closing() as conn:
            refused_ask = filing_refusal(conn, _bulk_ids(args), request)
    if refused_ask:
        tid, filed = refused_ask
        return _err(f"kanban block: {filed}" + (f" (card {tid}; nothing was blocked)"
                                                 if len(_bulk_ids(args) or ()) > 1 else ""), 2)
    if getattr(args, "metadata_only", False):
        from hermes_cli.kanban_human_request_migration import classify_cli_request
        return classify_cli_request(args, request, reason)
    author = _profile_author()
    ids = _bulk_ids(args)
    suffix = f": {reason}" if reason else ""
    with kbc.connect_closing() as conn:
        rc = _ids_error(ids, _REASON_WORDS_HINT, conn)
        if rc:
            return rc

        def ok_msg(tid):
            # Report where it landed: dependency blocks -> todo, tripped unblock-loop breaker -> triage.
            landed = kb.get_task(conn, tid)
            where = landed.status if landed else "blocked"
            from hermes_cli import kanban_awaiting as kaw
            if kaw.escalated_owner(conn, tid):
                return _parked_msg(conn, "Blocked", suffix)(tid)
            if where == "todo":
                return f"{tid} → todo (dependency wait){suffix}"
            if kind == "dependency" and where == "blocked":
                return f"Blocked {tid} as needs_input (no open parent to wait on){suffix}"
            if where == "triage":
                verdict = ("has an explicit human request" if (landed.human_request if landed else request)
                           else "orchestration attention needed")
                return f"{tid} → triage (unblock loop detected — {verdict}){suffix}"
            return f"Blocked {tid}{suffix}"

        try:
            wake = _cli_wake_check(args, conn=conn, ids=ids)
            awaiting, wake = _cli_awaiting(args, wake)
        except ValueError as exc:
            return _err(f"kanban block: {exc}", 2)
        from hermes_cli.kanban_block_policy import block_policy_error
        # Per card (task=): it checks that card's exhausted budget and offers kind='assignment' only where a
        # different profile can take it. The first refusal stops the whole bulk block, as before. [daveey]
        policy = next(filter(None, (block_policy_error(kind, human_request=request, wake_check=wake[0],
                                                       clock_only=wake[2], reason=reason, task=(conn, t))
                                    for t in ids)), None)
        if policy:
            return _err(f"kanban block: {policy}", 2)
        refused: dict = {}
        op = _guarded_park(conn, "block", wake, refused, _commented(
            reason, author, "BLOCKED", lambda tid, comment: kb.block_task(
                conn, tid, reason=reason, kind=kind, expected_run_id=_worker_run_id_for(tid),
                human_request=request, awaiting=awaiting, comment=comment, actor=cli_actor(),
                wake_check=wake[0], check_every=wake[1], clock_only=wake[2])),
            awaiting=awaiting, reason=reason, kind=kind)
        return _bulk_apply(ids, op, ok_msg, lambda tid: refused.get(tid) or f"cannot block {tid}", conn=conn)


def _cmd_schedule(args: argparse.Namespace) -> int:
    reason = _joined_words(args.reason)
    routed = _cli_route_needs("schedule", args, reason)
    if routed is not None:
        return routed
    author = _profile_author()
    ids = _bulk_ids(args)
    suffix = f": {reason}" if reason else ""
    with kbc.connect_closing() as conn:
        rc = _ids_error(ids, _REASON_WORDS_HINT, conn)
        if rc:
            return rc
        try:
            wake = _cli_wake_check(args, conn=conn, ids=ids)
            awaiting, wake = _cli_awaiting(args, wake)
        except ValueError as exc:
            return _err(f"kanban schedule: {exc}", 2)
        refused: dict = {}
        op = _guarded_park(conn, "schedule", wake, refused, _commented(
            reason, author, "SCHEDULED", lambda tid, comment: kb.schedule_task(
                conn, tid, reason=reason, expected_run_id=_worker_run_id_for(tid), awaiting=awaiting,
                wake_check=wake[0], check_every=wake[1], clock_only=wake[2], comment=comment)),
            awaiting=awaiting, reason=reason)
        from hermes_cli.kanban_reason_comment import human_answer_refusal
        from hermes_cli import kanban_awaiting as kaw
        parked = op

        def verified(tid):
            """A park that reports success but leaves the card where it was is a failure, not "Scheduled"."""
            if not parked(tid):
                return False
            task = kb.get_task(conn, tid)
            if (task is not None and task.status == "scheduled") or kaw.escalated_owner(conn, tid):
                return True
            refused[tid] = (f"cannot schedule {tid}: the park was accepted but the card is still "
                            f"{task.status if task else 'missing'}, so nothing changed")
            return False

        return _bulk_apply(ids, verified, _parked_msg(conn, "Scheduled", suffix),
                           lambda tid: (refused.get(tid) or human_answer_refusal(conn, tid, "schedule")
                                        or f"cannot schedule {tid}"), conn=conn)


from hermes_cli.kanban_cli_release import cmd_unblock as _cmd_unblock


def _cmd_request_review(args: argparse.Namespace) -> int:
    tid = args.task_id
    summary = _stripped_or_none(getattr(args, "summary", None))
    metadata, rc = _parse_metadata_flag(getattr(args, "metadata", None))
    if rc:
        return rc
    with kbc.connect_closing() as conn:
        gate_err = _goal_gate_error(
            conn, tid, summary or "", "review handoff",
            "Record the block with kanban block instead of requesting review.",
            "Provide acceptance evidence matching the task.")
        if gate_err:
            return _err(gate_err)
        ok, reason = kb.request_review(
            conn, tid, summary=summary, metadata=metadata, reviewer=getattr(args, "reviewer", None),
            expected_run_id=_worker_run_id_for(tid), force=bool(getattr(args, "force", False)), with_reason=True)
        if not ok:
            return _err(f"cannot request review for {tid}: {reason or 'not running/ready?'}")
        persisted_run = kb.latest_run(conn, tid)
        display_summary = persisted_run.summary if persisted_run else None
        print(f"Requested review for {tid}" + (f": {display_summary}" if display_summary else ""))
        _print_queued_local(conn)
    return 0


def _cmd_request_changes(args: argparse.Namespace) -> int:
    tid = args.task_id
    reason = " ".join(args.reason).strip()
    with kbc.connect_closing() as conn:
        ok, detail = kb.request_changes(conn, tid, reason=reason, expected_run_id=_worker_run_id_for(tid))
        if not ok:
            return _err(f"cannot request changes for {tid}: {detail or 'invalid review state'}")
        print(f"Requested changes for {tid}" + (f"; routed to {detail}" if detail else ""))
        _print_queued_local(conn)
    return 0


def _print_queued_local(conn) -> None:
    """[daveey] A write the stopped Board sync keeps local says so (``kanban_board_delivery``)."""
    from hermes_cli.kanban_board_delivery import cli_lines
    for line in cli_lines(conn):
        print(line)


def _cmd_reopen_review(args: argparse.Namespace) -> int:
    ids, rc = _require_ids(args)
    if rc:
        return rc
    reason = getattr(args, "reason", None)
    if reason is not None:
        reason = str(kb.redact_review_value(reason.strip())).strip() or None
    author = _profile_author() if reason else None
    suffix = f": {reason}" if reason else ""
    with kbc.connect_closing() as conn:
        def op(tid):
            if not kb.reopen_review_task(conn, tid):
                return False
            if reason:
                kb.add_comment(conn, tid, author or "operator", f"CHANGES REQUESTED: {reason}")
            return True

        return _bulk_apply(ids, op, lambda tid: f"Reopened {tid}{suffix}",
                           lambda tid: f"cannot reopen {tid} (not in review?)", conn=conn)


from hermes_cli.kanban_cli_release import cmd_promote as _cmd_promote


def _cmd_archive(args: argparse.Namespace) -> int:
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
        # A card another machine runs under its live Board lease gets a stop request, not a local status write
        # the Board would reject (kanban_remote_stop). [daveey]
        from hermes_cli.kanban_remote_stop import RemoteArchiveRefused, archive as _archive
        outcomes: dict[str, dict] = {}
        refusals: dict[str, str] = {}

        def _one(tid: str) -> bool:
            try:
                out = _archive(conn, tid, via=vias.get(tid))
            except RemoteArchiveRefused as exc:
                refusals[tid] = str(exc)
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


def _host_lock_holders() -> dict:
    """``{"dispatcher": info|None, "sync": info|None}``: who holds the host-wide singleton locks. [daveey]"""
    out: dict = {}
    try:
        from gateway.kanban_singleton import holder_info
        root = kb.kanban_home() / "kanban"
        for name in ("dispatcher", "sync"):
            out[name] = holder_info(root / f".{name}.lock")
    except Exception:  # health: allow BLE001 -- stats must print even when the lock probe can't run
        logging.getLogger(__name__).debug("kanban stats: host lock probe failed", exc_info=True)
    return out


def _cmd_stats(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        stats = kb.board_stats(conn)
    stats["host_locks"] = _host_lock_holders()
    if _json_out(args, stats):
        return 0
    print("By status:")
    for k in kanban_workflow.DEFAULT_WORKFLOW.keys():
        print(f"  {k:8s}  {stats['by_status'].get(k, 0)}")
    if stats["by_assignee"]:
        print("\nBy assignee:")
        for who, counts in sorted(stats["by_assignee"].items()):
            print(f"  {who:20s}  {_fmt_counts(counts)}")
    if stats.get("waiting_on_user"):
        print(f"\nWaiting on you: {stats['waiting_on_user']} ({', '.join(stats['waiting_on_user_ids'][:10])})")
    age = stats["oldest_ready_age_seconds"]
    if age is not None:
        print(f"\nOldest ready task age: {int(age)}s")
    locks = stats.get("host_locks") or {}
    if locks:
        from gateway.kanban_singleton import describe_holder
        print("\nHost locks:")
        for name, info in locks.items():
            if not info:
                print(f"  {name:10s}  free (nothing on this host {'dispatches' if name == 'dispatcher' else 'syncs'})")
                continue
            since = info.get("since")
            held = f", held {int(time.time() - since)}s" if isinstance(since, (int, float)) else ""
            flag = "" if info.get("root") else "  ⚠ not the root gateway: its config's kanban caps apply"
            print(f"  {name:10s}  {describe_holder(info)}{held}{flag}")
    return 0


def _cmd_notify_subscribe(args: argparse.Namespace) -> int:
    delivery_metadata = {key: value for key, value in (("parent_chat_id", getattr(args, "parent_chat_id", None)),
                                                       ("guild_id", getattr(args, "guild_id", None))) if value}
    if args.platform in ("tui", "hub"):
        # An explicit subscribe is a shepherd: the chat's sidebar row stays busy until the card
        # lands (kanban_session_activity.COUNTED_SUB_REASONS). Upgrades an earlier touch sub, never
        # downgrades an owning create/origin sub (add_notify_sub). 'hub' is hermes-hub's shepherd
        # action: stamped so children do not inherit it (_inherit_notify_subs). [daveey]
        delivery_metadata["reason"] = "shepherd"
    user_id, user_id_alt = _caller_identity_for_target(
        args.platform, args.chat_id, args.thread_id, args.user_id, getattr(args, "user_id_alt", None))
    with kbc.connect_closing() as conn:
        if kb.get_task(conn, args.task_id) is None:
            return _err(f"no such task: {args.task_id}")
        kbn.add_notify_sub(
            conn, task_id=args.task_id, platform=args.platform, chat_id=args.chat_id,
            chat_type=args.chat_type, thread_id=args.thread_id, user_id=user_id,
            user_id_alt=user_id_alt,
            notifier_profile=args.notifier_profile or _profile_author(),
            delivery_mode=getattr(args, "delivery_mode", None),
            delivery_metadata=delivery_metadata or None,
        )
    print(f"Subscribed {args.platform}:{args.chat_id}" + (f":{args.thread_id}" if args.thread_id else "")
          + f" to {args.task_id}")
    if args.platform == "tui":
        from hermes_cli.kanban_notify_target import print_tui_target
        print_tui_target(args.task_id, args.chat_id)
    return 0


def _cmd_notify_list(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        subs = kbn.list_notify_subs(conn, args.task_id)
    if _json_out(args, subs):
        return 0
    if not subs:
        print("(no subscriptions)")
        return 0
    for s in subs:
        thr = f":{s['thread_id']}" if s.get("thread_id") else ""
        dmode, ctype = s.get("delivery_mode") or "notify", s.get("chat_type") or "dm"
        extras = "".join((
            f"  owner={s['notifier_profile']}" if s.get("notifier_profile") else "",
            "" if ctype == "dm" else f"  chat_type={ctype}",
            f"  user_id_alt={s['user_id_alt']}" if s.get("user_id_alt") else "",
            "" if dmode == "notify" else f"  mode={dmode}",
        ))
        print(f"  {s['task_id']:10s}  {s['platform']}:{s['chat_id']}{thr}  (since event {s['last_event_id']}){extras}")
    return 0


def _cmd_notify_unsubscribe(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        ok = kbn.remove_notify_sub(conn, task_id=args.task_id, platform=args.platform, chat_id=args.chat_id,
                                  thread_id=args.thread_id)
    return _ok_or_err(ok, "(no such subscription)", f"Unsubscribed from {args.task_id}")


def _cmd_log(args: argparse.Namespace) -> int:
    content = kb.read_worker_log(args.task_id, tail_bytes=args.tail)
    if content is None:
        return _err(f"(no log for {args.task_id} — task may not have spawned yet)")
    sys.stdout.write(content)
    if not content.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def _cmd_runs(args: argparse.Namespace) -> int:
    """Show attempt history for a task."""
    rsk, rc = _run_state_kwargs(args, "runs")
    if rc:
        return rc
    with kbc.connect_closing() as conn:
        runs = kb.list_runs(conn, args.task_id, **rsk)
    if _json_out(args, [_obj_dict(r, _RUNS_RUN_FIELDS) for r in runs]):
        return 0
    if not runs:
        print(f"(no runs yet for {args.task_id})")
        return 0
    print(f"{'#':3s}  {'OUTCOME':12s}  {'PROFILE':16s}  {'ELAPSED':>8s}  STARTED")
    for i, r in enumerate(runs, 1):
        end = r.ended_at or int(time.time())
        # Clamp to 0 so NTP backward-jumps don't print negative durations.
        elapsed = max(0, end - r.started_at)
        el = f"{elapsed}s" if elapsed < 60 else f"{elapsed // 60}m" if elapsed < 3600 else f"{elapsed / 3600:.1f}h"
        outcome = r.outcome or ("(running)" if not r.ended_at else r.status)
        print(f"{i:3d}  {outcome:12s}  {(r.profile or '-'):16s}  {el:>8s}  {_fmt_ts(r.started_at)}")
        if r.summary:
            print(f"     → {r.summary.splitlines()[0][:100]}")
        if r.error:
            print(f"     ✖ {r.error[:100]}")
    return 0


def _cmd_context(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        text = kb.build_worker_context(conn, args.task_id)
    print(text)
    return 0


def _run_triage_sweep(args: argparse.Namespace, verb: str, mod, run_one, json_key: str,
                      json_fields: tuple[str, ...], human_ok) -> int:
    """Shared driver for ``specify`` / ``decompose``: validate ids (one task id XOR ``--all``), run
    ``run_one(tid, author=...)`` per id, print JSON or human lines, exit code."""
    all_flag = bool(getattr(args, "all_triage", False))
    author = getattr(args, "author", None) or _profile_author()
    want_json = bool(getattr(args, "json", False))
    tenant = getattr(args, "tenant", None)
    if args.task_id and all_flag:
        return _err("kanban: pass either a task id OR --all, not both", 2)
    if all_flag:
        ids = mod.list_triage_ids(tenant=tenant)
        if not ids:
            if want_json:
                print(json.dumps({json_key: 0, "total": 0}))
            else:
                print("No triage tasks" + (f" for tenant {tenant!r}" if tenant else "") + ".")
            return 0
    elif args.task_id:
        ids = [args.task_id]
    else:
        return _err(f"kanban: {verb} requires a task id or --all", 2)

    ok_count = 0
    for tid in ids:
        outcome = run_one(tid, author=author)
        if outcome.ok:
            ok_count += 1
        if want_json:
            print(json.dumps(_obj_dict(outcome, json_fields)))
        elif outcome.ok:
            print(human_ok(outcome))
        else:
            print(f"kanban: {verb} {outcome.task_id}: {outcome.reason}", file=sys.stderr)
    if not all_flag:
        return 0 if ok_count == 1 else 1
    # --all: exit 1 only when every candidate failed (honest signal for scripts).
    return 0 if (ok_count > 0 or not ids) else 1


def _cmd_specify(args: argparse.Namespace) -> int:
    """Spec a triage task (or all) via the auxiliary LLM, promote to todo."""
    from hermes_cli import kanban_specify as spec

    return _run_triage_sweep(args, "specify", spec, spec.specify_task, "specified",
                             ("task_id", "ok", "reason", "new_title"),
                             lambda o: f"Specified {o.task_id} → todo{_retitled_suffix(o)}")


def _cmd_decompose(args: argparse.Namespace) -> int:
    """Fan a triage task (or all) out into child tasks via the auxiliary LLM."""
    from hermes_cli import kanban_decompose as decomp

    force = bool(getattr(args, "force", False))

    def run_one(tid: str, *, author: str):
        return decomp.decompose_task(tid, author=author, force=force)

    return _run_triage_sweep(args, "decompose", decomp, run_one, "decomposed",
                             ("task_id", "ok", "reason", "fanout", "child_ids", "new_title"), _decompose_ok_line)


_HANDLERS = {
    "attest-run-models": _cmd_attest_run_models,
    "init": _cmd_init, "create": _cmd_create, "swarm": _cmd_swarm,
    "list": _cmd_list, "ls": _cmd_list, "show": _cmd_show, "wait-check": _cmd_wait_check,
    "wait-comment": _cmd_wait_comment,
    "capabilities": _cmd_capabilities,
    "assign": _cmd_assign, "set-model": _cmd_set_model,
    "reclaim": _cmd_reclaim, "move": _cmd_move, "reassign": _cmd_reassign,
    "diagnostics": _cmd_diagnostics, "diag": _cmd_diagnostics,
    "link": _cmd_link, "unlink": _cmd_unlink, "claim": _cmd_claim,
    "supervisor-requests": _cmd_supervisor_requests,
    "comment": _cmd_comment, "attach": _cmd_attach,
    "attachments": _cmd_attachments, "attach-rm": _cmd_attach_rm,
    "complete": _cmd_complete, "edit": _cmd_edit, "reopen": _cmd_reopen, "block": _cmd_block,
    **dict.fromkeys(("contain", "resume-contained"), lambda a: importlib.import_module("hermes_cli.kanban_containment_cli").command(a)),
    "schedule": _cmd_schedule, "unblock": _cmd_unblock,
    "renew-scope": lambda a: importlib.import_module("hermes_cli.kanban_scope_renewal_cli").command(a),
    "owner-chats": lambda a: importlib.import_module("hermes_cli.kanban_owner_chat").command(a),
    "delegate": lambda a: importlib.import_module("hermes_cli.kanban_scope_delegate").command(a),
    "release": lambda a: importlib.import_module("hermes_cli.kanban_release").command(a),
    "governors": lambda a: importlib.import_module("hermes_cli.kanban_governors_cli").governors_command(a),
    "why": lambda a: importlib.import_module("hermes_cli.kanban_governors_cli").why_command(a),
    "request-review": _cmd_request_review, "request-changes": _cmd_request_changes,
    "reopen-review": _cmd_reopen_review, "promote": _cmd_promote, "resume": _cmd_resume, "quota-release": _cmd_quota_release,
    "archive": _cmd_archive, "tail": _cmd_tail, "dispatch": _cmd_dispatch, "source-release": _cmd_source_release,
    "daemon": _cmd_daemon, "watch": _cmd_watch, "stats": _cmd_stats,
    "log": _cmd_log, "runs": _cmd_runs, "heartbeat": _cmd_heartbeat,
    "assignees": _cmd_assignees, "notify-subscribe": _cmd_notify_subscribe,
    "notify-list": _cmd_notify_list, "notify-unsubscribe": _cmd_notify_unsubscribe,
    "context": _cmd_context, "specify": _cmd_specify, "decompose": _cmd_decompose,
    "gc": _cmd_gc, "sync": _cmd_sync, "queue": _cmd_queue, "spend-budget": _spend_budget_cli.command, "lanes": _cmd_lanes,
}


# --- Slash-command entry point (used by /kanban from CLI and gateway) ---

_SLASH_KANBAN_HELP = """\
**/kanban** — manage the shared task board.

Common subcommands:
  `list` (alias `ls`)   List tasks on the current board
  `show <id>`           Task details + comments + events
  `stats`               Per-status / per-assignee counts
  `create <title>…`     Create a task (auto-subscribes you to events)
  `comment <id> <msg>`  Append a comment
  `attach <id> <path>`  Attach a local file; `attachments <id>` to list
  `complete <id>…`      Mark task(s) done
  `request-review <id>` Enter first-class review; `request-changes <id> <reason>` returns an active review to its implementer
  `block <id> [reason]` Mark blocked; `schedule <id> [reason]` parks time-delay work; `unblock <id>` to revive
  `assign <id> <profile>`  Reassign
  `boards list`         Show all boards
  `assignees`           Known profiles + counts
  `context <id>`        Full worker-context dump
  `runs <id>`           Attempt history
  `log <id>`            Worker log

Run `/kanban <subcommand> -h` for arguments. \
Read-only commands are safe while an agent is running.\
"""


def run_slash(rest: str, *, human_typed: bool = False) -> str:
    """Execute a ``/kanban …`` string (``rest`` = everything after ``/kanban``) and return captured
    stdout/stderr. Shared by the interactive CLI and the gateway so formatting is identical.

    ``human_typed``: only the composer/platform-message ingress points pass True (a person typed
    the line); any other caller (an agent's script importing this) gets the CLI's ancestry check
    for comment provenance. [daveey]"""
    import io

    # Non-posix split (Windows) keeps backslashes as path separators but
    # leaves quote characters in the tokens — strip a fully wrapping pair
    # so `"my task"` reaches argparse as `my task`, not `"my task"`.
    tokens = []
    if rest and rest.strip():
        for tok in shlex.split(rest, posix=os.name == "posix"):
            if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"'):
                tok = tok[1:-1]
            tokens.append(tok)

    # Bare ``/kanban`` or ``/kanban help`` / ``--help`` / ``-h`` / ``?``:
    # show the curated short-help block instead of dumping argparse's full
    # usage tree (which is enormous and reads as garbage in a chat
    # bubble).  Per-subcommand help still works via ``/kanban foo -h``.
    if not tokens or tokens[0] in {"help", "--help", "-h", "?"}:
        return _SLASH_KANBAN_HELP
    # build_parser() needs a subparsers action to attach to: build a throwaway one and drive
    # kanban_parser directly so usage/error text reads ``/kanban``.
    _wrap = argparse.ArgumentParser(prog="/kanban-wrap", add_help=False)
    _wrap.exit_on_error = False  # type: ignore[attr-defined]
    kanban_parser = build_parser(_wrap.add_subparsers(dest="_top"))
    kanban_parser.prog = "/kanban"
    kanban_parser.exit_on_error = False  # type: ignore[attr-defined]
    subparsers = [a for a in kanban_parser._actions if isinstance(a, argparse._SubParsersAction)]
    for _action in subparsers:
        for _name, _choice in _action.choices.items():
            _choice.prog = f"/kanban {_name}"
            _choice.exit_on_error = False  # type: ignore[attr-defined]

    def _usage_for_error() -> str:
        if tokens:
            for _action in subparsers:
                subparser = _action.choices.get(tokens[0])
                if subparser is not None:
                    return subparser.format_usage().rstrip()
        return kanban_parser.format_usage().rstrip()

    buf_out, buf_err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            args = kanban_parser.parse_args(tokens)
    except SystemExit as exc:
        out, err = buf_out.getvalue().rstrip(), buf_err.getvalue().rstrip()
        if exc.code in {0, None} and out:  # ``-h`` help dump
            return out
        body = err or out
        return f"⚠ /kanban usage error\n{body}" if body else "⚠ /kanban usage error"
    except argparse.ArgumentError as exc:
        return f"⚠ /kanban usage error\n{_usage_for_error()}\n{exc}"
    # [daveey] Set after parsing, so no argv spelling can claim it; only the ingress callers say a
    # person typed this /kanban line.
    args._slash_typed = bool(human_typed)

    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        try:
            kanban_command(args)
        except SystemExit:
            pass
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)

    out, err = buf_out.getvalue().rstrip(), buf_err.getvalue().rstrip()
    if out and getattr(args, "json", False):
        # --json callers parse the result: stderr diagnostics would make it invalid JSON, so
        # they stay out of it (commands that warn put the warning in the payload). [daveey]
        return out
    if err and out:
        return f"{out}\n{err}"
    return err if err else (out or "(no output)")
