"""Argparse tree for ``hermes kanban …`` (``build_parser``).

The subcommand tree is declared as data — one ``_cmd(...)`` record per
subcommand holding its ``add_parser`` kwargs and an ordered tuple of
``add_argument`` specs — and materialised by ``_add_commands``. Order of
records and arguments is the order argparse renders in ``--help``.
"""

from __future__ import annotations

import argparse
import json

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_notify as kbn
from hermes_cli.kanban_chat_links_settle import DEFAULT_SETTLE_S
# The `resume` handler rides kanban.py's existing kanban_parser import, so kanban.py (at its
# FILE_LINES cap) gains no line for it. [daveey]
from hermes_cli.kanban_operator_resume import cmd as cmd_resume
from hermes_cli.kanban_quota_episode_release import cmd as cmd_quota_release  # same ride as cmd_resume


def _arg(*flags: str, **kw):
    return (flags, kw)


class _RequiresAction(argparse.Action):
    """[daveey] ``--requires X`` (repeatable) and ``--no-requires`` (an explicit ``[]``) share the
    ``requires`` dest, so omitted stays ``None``; giving both is a usage error (exit 2)."""

    def __call__(self, parser, namespace, values, option_string=None):
        current = getattr(namespace, "requires", None)
        if (option_string == "--no-requires" and current) or getattr(namespace, "no_requires", False):
            parser.error("--no-requires and --requires are mutually exclusive")
        if option_string == "--no-requires":
            namespace.requires, namespace.no_requires = [], True
        else:
            namespace.requires = [*(current or []), values]


def _cmd(name: str, args=(), *, children=None, **parser_kw):
    """``children`` = ``(dest, [specs])`` for a nested subparser group."""
    return (name, parser_kw, tuple(args), children)


def _add_commands(sub: argparse._SubParsersAction, specs) -> None:
    for name, parser_kw, args, children in specs:
        p = sub.add_parser(name, **parser_kw)
        for flags, kw in args:
            p.add_argument(*flags, **kw)
        if children:
            dest, child_specs = children
            _add_commands(p.add_subparsers(dest=dest), child_specs)


def _json_flag(**kw):
    return _arg("--json", action="store_true", **kw)


def _reason(help: str):
    return _arg("--reason", help=help)


def _nonnegative_int(value: str) -> int:
    """argparse type for retention days: a negative window builds a future cutoff
    that matches every row, so reject it at the CLI boundary before any sweep."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("retention days must be >= 0 (0 disables that sweep)")
    return parsed


def _run_state_args(type_help: str):
    return (
        _arg("--state-type", choices=("status", "outcome"), help=f"With --state-name: {type_help}"),
        _arg("--state-name", metavar="VALUE",
             help="With --state-type: keep runs whose column equals this value"),
    )


def _triage_sweep_args(verb: str, Verb: str, noun: str):
    """Shared ``specify`` / ``decompose`` arguments."""
    return (
        _arg("task_id", nargs="?", help=f"Task id to {verb} (required unless --all is given)"),
        _arg("--all", dest="all_triage", action="store_true", help=f"{Verb} every task currently in the triage column"),
        _arg("--tenant", help="When used with --all, restrict the sweep to this tenant"),
        _arg("--author",
             help=f"Author name recorded on the audit comment (default: $HERMES_PROFILE or '{noun}')"),
        _json_flag(help="Emit one JSON object per task on stdout"),
    )


def _bulk_ids(verb: str):
    return _arg("--ids", nargs="+", help=f"Additional task ids to {verb} with the same reason (bulk mode)")


_TASK_ID = _arg("task_id")
_HUMAN_REQUEST = _arg("--human-request", type=json.loads, metavar="JSON", default=argparse.SUPPRESS,
                      help="Explicit human request evidence (action, why_human, interaction, scope, evidence; optional "
                           "choices, resolved_check, resolved_check_every); never authorization")


def _WAKE_CHECK_ARGS():
    """Condition-wake flags shared by ``block`` and ``schedule`` (``kanban_wake_check``). [daveey]"""
    return (
        _arg("--wake-check", dest="wake_check", default=None,
             help="Shell probe run while parked (bash -lc, 30 s timeout): exit 0 wakes the card at "
                  "once, exit 1 = not yet. Required for a park more than 2 h out unless --clock-only."),
        _arg("--check-every", dest="check_every", type=int, default=None,
             help="Seconds between --wake-check probes (default 900, min 120, max 21600)"),
        _arg("--clock-only", dest="clock_only", action="store_true",
             help="A pure time wait with nothing to probe (soak window, calendar date)"),
        _arg("--awaiting", dest="awaiting", default=None, metavar="KIND:REF|JSON",
             help="Who produces the condition: card:<task id> (JSON adds gateway/board), ci:<run id>, "
                  "deploy:<id>, external:<vendor/ticket>, clock:<window>, resource:<gate> (an internal "
                  "governor/pool hold; needs --wake-check, never escalates, backs off). A wait needs a producer; "
                  "agent-action conditions (grant, permission, install, fix) are work, not waits."),
        _arg("--awaiting-why", dest="awaiting_why", default=None, help="Evidence for --awaiting"),
    )
_NEED = _arg("--need", dest="needs", action="append", default=None, metavar="CAPABILITY",
             help="Gateway capability the wall needs (repeatable), e.g. aws:<acct>:<action>, ssh:<host>. "
                  "If another gateway holds it, an owner card opens there and this card parks on it.")
_TASK_IDS = _arg("task_ids", nargs="+")
_SLUG = _arg("slug")
_TENANT = _arg("--tenant", help="Tenant namespace")
_PRIORITY = _arg("--priority", type=int, default=0, help="Priority tiebreaker")
_RECLAIM_REASON = _reason("Human-readable reason (recorded on the reclaimed event)")
_NOTIFY_TARGET = (
    _arg("--platform", required=True),
    _arg("--chat-id", required=True),
    _arg("--thread-id"),
)
_STEP_HANDOFF = (
    _arg("--summary", help="Structured handoff summary. Falls back to --result if omitted."),
    _arg("--metadata", help="JSON dict of structured facts to store on the latest completed run."),
)

_BOARD_SPECS = [
    _cmd("list", [
        _json_flag(),
        _arg("--all", action="store_true", help="Include archived boards too"),
    ], aliases=["ls"], help="List all boards with task counts"),
    _cmd("create", [
        _arg("slug", help="Board slug (kebab-case, e.g. atm10-server)"),
        _arg("--name", help="Human-readable display name (defaults to Title Case of slug)"),
        _arg("--description", help="Optional description"),
        _arg("--icon", help="Optional emoji or single-character icon for the dashboard"),
        _arg("--color", help="Optional hex color (e.g. '#8b5cf6') for the dashboard"),
        _arg("--switch", action="store_true", help="Switch to the new board after creating it"),
        _arg("--default-workdir", help="Default workspace path for tasks created on this board"),
    ], aliases=["new"], help="Create a new board"),
    _cmd("rm", [
        _SLUG,
        _arg("--delete", action="store_true",
             help="Hard-delete the board directory instead of archiving it. "
                  "Default is to move it to boards/_archived/ so it's recoverable."),
    ], aliases=["remove", "delete"], help="Archive (default) or delete a board"),
    _cmd("switch", [_SLUG], aliases=["use"], help="Set the active board for subsequent CLI calls"),
    _cmd("show", aliases=["current"], help="Print the currently-active board slug"),
    _cmd("rename", [_SLUG, _arg("name", help="New display name")],
         help="Change a board's human-readable display name (slug is immutable)"),
    _cmd("dispatch-owner", [
        _SLUG,
        _arg("owner", nargs="?", help="Exact dispatcher slug to set; omit to read"),
        _arg("--clear", action="store_true", help="Explicitly remove dispatch ownership"),
        _json_flag(),
    ], help="Read/set native board dispatch ownership (same-UID coordination)"),
    _cmd("set-default-workdir", [
        _SLUG,
        _arg("path", nargs="?", help="Absolute path to use as default workdir. Omit to clear."),
    ], help="Set the default workspace path for tasks on a board"),
    _cmd("export", [
        _arg("slug", nargs="?", help="Board to export (default: the current board)"),
        _arg("-o", "--output", help="Archive path (default: ./<slug>.tar.gz)"),
        _arg("--no-attachments", action="store_true", help="Skip attachment files, keeping the archive small"),
        _arg("--include-logs", action="store_true", help="Include per-task worker logs"),
        _json_flag(),
    ], help="Export a board to a portable .tar.gz archive", description=(
        "Package a board's tasks, comments, links, history, and file attachments into one archive "
        "that can be imported on another machine. Claims, worker PIDs, chat subscriptions, and "
        "paths belonging to this machine are stripped. Workspaces are never included — they are "
        "rebuilt on demand."
    )),
    _cmd("import", [
        _arg("archive", help="Path to the .tar.gz archive"),
        _arg("--as", dest="as_slug", help="Slug for the imported board (default: from the archive)"),
        _arg("--switch", action="store_true", help="Switch to the imported board afterwards"),
        _json_flag(),
    ], help="Import a board archive as a new board", description=(
        "Import a .tar.gz produced by `hermes kanban boards export`. The board always lands as a "
        "NEW board — the slug gains a numeric suffix if it is already taken — so an import can "
        "never overwrite or merge into a board you already have."
    )),
]

# Top-level ``hermes kanban <action>`` records, in ``--help`` order.
_SPECS = [
    _cmd("selfwork-producer", [
        _arg("--operation", choices=["capabilities", "prepare", "advance"], required=True),
        _arg("--request-file", default="-", help="Producer JSON snapshot ('-' = stdin)"),
    ], help="Versioned durable headless self-work producer receipts"),
    _cmd("init", help="Create kanban.db if missing (idempotent)"),
    _cmd("selfwork-forward", [
        _arg("--request-file", required=True, help="Trusted courier envelope file ('-' = stdin)"),
        _arg("--json", action="store_true", help="Return structured authorization (always JSON)"),
    ], help="Authorize worker self-work forwarding through its accepted origin"),
    _cmd("selfwork-create", [
        _arg("--request-file", required=True, help="Trusted courier envelope file ('-' = stdin)"),
        _arg("--json", action="store_true", help="Return structured intake receipt (always JSON)"),
    ], help="Receive authenticated self-work at its configured intake home"),
    _cmd("origin-create", [
        _arg("--request-file", required=True, help="Trusted courier envelope file ('-' = stdin)"),
        _arg("--json", action="store_true", help="Return structured pending creation (always JSON)"),
    ], help="Create owner work through registered tools, pending origin receipt"),
    _cmd("origin-adopt", [
        _arg("--request-file", required=True, help="Trusted courier envelope file ('-' = stdin)"),
        _arg("--json", action="store_true", help="Return pending adoption (always JSON)"),
    ], help="Attach existing owner work through registered tools, pending origin receipt"),
    _cmd("ownership-snapshot", [
        _arg("--request-file", required=True, help="Trusted courier envelope file ('-' = stdin)"),
        _arg("--json", action="store_true", help="Return the owner reading (always JSON)"),
    ], help="Read-only owner continuation evidence for an origin binding (never writes)"),
    _cmd("origin-forward", [
        _arg("--request-file", required=True, help="Trusted courier envelope file ('-' = stdin)"),
        _arg("--json", action="store_true", help="Return frozen outbound envelope (always JSON)"),
    ], help="Authorize worker placement forwarding at the origin gateway; never a completion receipt"),
    _cmd("origin-bind", [
        _arg("--request-file", required=True, help="Trusted courier envelope file ('-' = stdin)"),
        _arg("--json", action="store_true", help="Return structured receipt (always JSON)"),
    ], help="Accept a courier origin obligation and read back its durable receipt"),
    _cmd("quiet-hold", [
        _arg("--check", action="store_true",
             help="Read-only preflight: print what quiet-observe-start would refuse (JSON) without taking a "
                  "lease or starting a cooldown; exit 0 when admitted apart from the lease, 3 otherwise"),
        _arg("--max-s", type=int, metavar="N", help="Lease length in seconds (1..480, no renewal)"),
        _arg("--reason", help="Why local automatic claims should pause (nonempty text)"),
        _arg("observer_argv", nargs=argparse.REMAINDER, metavar="-- OBSERVER_COMMAND",
             help="Observer argv, after a literal '--'; run without a shell in its own process group"),
    ], help="Pause local automatic claims for a bounded, non-renewable observer run (root-scoped)"),
    _cmd("boards", children=("boards_action", _BOARD_SPECS),
         help="Manage kanban boards (one board per project / workstream)",
         description=(
             "Boards let you separate unrelated streams of work (projects, repos, domains) into "
             "isolated queues. Each board has its own DB, workspaces directory, and dispatcher "
             "loop — tasks on one board cannot collide with tasks on another. The first board is "
             "'default' and always exists."
         )),
    _cmd("create", [
        _arg("title", help="Task title"),
        _HUMAN_REQUEST,
        _arg("--body", help="Optional opening post"),
        _arg("--body-file", metavar="PATH",
             help="Read the opening post from a file ('-' = stdin), so bodies with embedded "
                  "newlines or flag-like lines survive shell quoting. "
                  "Mutually exclusive with --body."),
        _arg("--assignee", help="Profile name to assign"),
        _arg("--requires", action=_RequiresAction,
             help="Required configured capability (repeatable): terminal, file, git, host_repo, or a "
                  "namespaced gateway capability such as aws:<acct>:<action> or ssh:<host> "
                  "(placed/pre-routed where it is held); repo work also infers requirements"),
        _arg("--no-requires", dest="requires", action=_RequiresAction, nargs=0,
             help="Declare that the card needs no configured capability (an explicit empty list); "
                  "an agent-run create that omits both --requires and --no-requires may be refused "
                  "with requires_declaration_missing. Workspace/skill-implied needs still apply."),
        _arg("--runs-on", dest="runs_on", default=None, metavar="LABELS",
             help="Machine labels the card needs, comma-separated (synced boards only): linux, "
                  "macos,xcode, machine:<name>; '' or '[]' = any machine; omit for the default placement"),
        _arg("--hub-reason", dest="hub_reason", default=None, metavar="TEXT",
             help="Only for a card that needs the hub itself: what only the hub has (its signed-in "
                  "Chrome, a Desktop-session interaction, an "
                  "existing hub worktree with unpushed commits, a hermes-hub command acting with the "
                  "hub's key, or live gateway state hub-admin cannot reach). Board admin key and "
                  "instance-role SSM steps are not hub needs: run them with hub-admin on a "
                  "capability:hub-write worker. A Sonnet admission judge "
                  "reads it with the card; without admission a hub card goes to the worker pool (pool "
                  "workers have hub ssh + hub-admin) and an explicit hub pin is refused."),
        _arg("--force-hub", dest="force_hub", action="store_true",
             help="Operator override: keep the card on the hub without the admission judge. Refused "
                  "inside a dispatcher worker; recorded as a forced hub_admission event."),
        _arg("--parent", action="append", default=[], help="Parent task id (repeatable)"),
        _arg("--workspace",
             help="scratch | worktree | worktree:<path> | dir:<path> (default: scratch; "
                  "an explicit 'scratch' also opts out of a project-scoped board's project)"),
        _arg("--branch", help="Branch name for worktree tasks, e.g. wt/t6-wire"),
        _arg("--project",
             help="Link to a project (id or slug). Anchors the task's "
                  "worktree under the project's primary repo with a "
                  "deterministic branch. See `hermes project list`."),
        _TENANT,
        _PRIORITY,
        _arg("--class", dest="admission_class", choices=["normal", "capacity", "urgent"], default="normal",
             help="Admission class (capacity requires --frees)"),
        _arg("--frees", dest="admission_frees", help="Single-line frees: claim"),
        _arg("--triage", action="store_true",
             help="Park in triage — a specifier will flesh out the spec and promote to todo"),
        _arg("--self-work", type=json.loads, metavar="JSON",
             help="Versioned Hermes self-maintenance declaration (repository, area, kind); omit for product work."),
        _arg("--idempotency-key",
             help="Dedup key. If a non-archived task with this key exists, "
                  "its id is returned instead of creating a duplicate."),
        _arg("--distinct-from", action="append", default=[], metavar="TASK_ID",
             help="Open card this is NOT a duplicate of (repeatable). A create that open cards already "
                  "cover is refused (create_overlap) unless every match is listed here with --distinct-reason."),
        _arg("--distinct-reason", default=None,
             help="Why this card is separate work from the --distinct-from cards (recorded on the new card)"),
        _arg("--origin-session", default=None,
             help="Chat on ANOTHER gateway this card is work for (@session:<profile>@<gateway>/<id>; "
                  "default $HERMES_ORIGIN_SESSION). It then shows in that chat's task list."),
        _arg("--max-runtime",
             help="Per-task runtime cap. Accepts seconds (300) or durations (90s, "
                  "30m, 2h, 1d). When exceeded, the dispatcher SIGTERMs (then "
                  "SIGKILLs) the worker and re-queues the task."),
        _arg("--created-by", default="user", help="Author name recorded on the task (default: user)"),
        _arg("--skill", action="append", default=[], dest="skills",
             help="Skill to force-load into the worker (repeatable). The kanban "
                  "lifecycle is already injected automatically. Example: --skill "
                  "translation --skill github-code-review"),
        _arg("--max-retries", type=int, metavar="N",
             help="Per-task override for the consecutive-failure "
                  f"circuit breaker. Trip on the Nth failure — e.g. --max-retries 1 blocks on the "
                  f"first failure (no retries), --max-retries 3 allows two retries. Omit to use "
                  f"the dispatcher's kanban.failure_limit config (default "
                  f"{kb.DEFAULT_FAILURE_LIMIT})."),
        _arg("--model", dest="model_override",
             help="Pin the worker to this model (passed as -m <model>) without "
                  "changing the profile's configured model. Combine with --provider "
                  "when the model belongs to a different backend than the profile's default."),
        _arg("--provider", dest="provider_override",
             help="Provider the --model belongs to (passed as --provider <name> to "
                  "the worker). Requires --model."),
        _arg("--pin-mandatory", action="store_true", dest="pin_mandatory",
             help="The --model pin is mandatory: a far quota wall never moves it to a same-class peer. "
                  "Requires --model."),
        _arg("--completion-contract", metavar="CONTRACT",
             help="local-only (default), OWNER/REPO for publication, or exact GitHub PR URL; required CI gates done."),
        _arg("--review-required", action="store_true", help="Require a reviewer before completion"),
        _arg("--goal", action="store_true", dest="goal_mode",
             help="Run the worker in a goal loop: after each turn a judge checks the "
                  "response against the card title/body and, if not done, the worker "
                  "keeps going in the same session until the judge agrees it's "
                  "complete (or the turn budget runs out, which blocks the card for "
                  "review). Best for open-ended cards one shot rarely finishes."),
        _arg("--goal-max-turns", type=int, metavar="N", dest="goal_max_turns",
             help="Turn budget for --goal workers (default 20). Ignored without --goal."),
        _arg("--reasoning-effort", metavar="EFFORT", dest="reasoning_effort",
             help="Worker reasoning effort (e.g. low, medium, high, none); default: the profile's."),
        _arg("--initial-status", choices=sorted(kb.VALID_INITIAL_STATUSES), default="running",
             help="Initial card status. Use 'blocked' for cards "
                  "that must stay parked. Human asks additionally require explicit --human-request evidence."),
        _json_flag(help="Emit JSON output"),
    ], help="Create a new task"),
    _cmd("swarm", [
        _arg("goal", help="Swarm goal / final outcome"),
        _arg("--worker", action="append", default=[], metavar="PROFILE:TITLE[:SKILL,SKILL]",
             help="Parallel worker card (repeatable)"),
        _arg("--verifier", required=True, help="Verifier profile"),
        _arg("--synthesizer", required=True, help="Synthesizer/writer profile"),
        _TENANT,
        _PRIORITY,
        _arg("--created-by", help="Creator/anchor profile"),
        _arg("--idempotency-key", help="Dedup key for the root card"),
        _json_flag(help="Emit JSON output"),
    ], help="Create a Kanban Swarm v1 graph (parallel workers → verifier → synthesizer)"),
    _cmd("chat-tasks", [
        _arg("session_id"), _arg("--expect", help="Expected task, board/task or gateway/board/task"),
        _json_flag(),
    ], help="Read the chat's task projection; --expect exits 4 if absent, 1 on read error"),
    _cmd("list", [
        _arg("--mine", action="store_true", help="Filter by $HERMES_PROFILE as assignee"),
        _arg("--assignee"),
        _arg("--status", choices=sorted(kb.VALID_STATUSES)),
        _arg("--tenant"),
        _arg("--session",
             help="Filter by originating chat/agent session id (set on tasks created from inside an ACP loop)"),
        _arg("--archived", action="store_true", help="Include archived tasks"),
        _json_flag(),
        _arg("--sort", choices=sorted(kb.VALID_SORT_ORDERS.keys()),
             help="Sort order for listed tasks (default: priority)"),
        _arg("--workflow-template-id", metavar="ID", help="Restrict to tasks with this workflow_template_id"),
        _arg("--step-key", dest="current_step_key", metavar="KEY",
             help="Restrict to tasks with this current_step_key"),
    ], aliases=["ls"], help="List tasks"),
    _cmd("wait-check", [
        _arg("check", choices=("task", "board-empty", "capability-owner"),
             help="task <id> | board-empty <slug> | capability-owner <hermes-hub request id[,id...]>"),
        _arg("target", help="Task id (task), board slug (board-empty) or owner request ids (capability-owner)"),
        _arg("--gateway", default=None, help="Gateway to read via ssh (e.g. hub); omit for this machine"),
        _arg("--board", default=None, help="Board slug for `task` (default board if omitted)"),
        _arg("--until", default=None,
             help="Comma-separated statuses that count as met (default: done,review,blocked,triage,parked-agent,archived "
                  "-- a tracker wakes when its owner finishes, wants review, blocks, goes to triage, or parks on "
                  "something only an agent can do; parked-agent = scheduled with no live producer)"),
        _arg("--from", dest="since", default=None,
             help="Comma-separated statuses the card had when you parked: not met while it is still in one of them "
                  "(re-park after handling a wake without waking again at once)"),
    ], help="Wake-probe primitive: exit 0 met, 1 not yet, 2 cannot tell. The only supported cross-gateway wait idiom"),
    _cmd("show", [_TASK_ID, _json_flag(), *_run_state_args("filter listed runs by task_runs column")],
         help="Show a task with comments + events"),
    _cmd("assign", [_TASK_ID, _arg("profile", help="Profile name (or 'none' to unassign)")],
         help="Assign or reassign a task"),
    _cmd("set-model", [
        _TASK_ID,
        _arg("model", nargs="?", help="Model to pin the worker to (or 'none' to clear the override)"),
        _arg("--provider",
             help="Provider the model belongs to (worker is spawned with "
                  "--provider <name>). Cleared together with the model."),
        _arg("--mandatory", action=argparse.BooleanOptionalAction, default=None,
             help="--mandatory: a far quota wall never moves this pin to a same-class peer (the card waits "
                  "for the reset). --no-mandatory lifts that. Omit to keep the card's setting."),
    ], help="Set or clear a task's model/provider override (takes effect on the next dispatch)"),
    _cmd("reclaim", [
        _TASK_ID, _RECLAIM_REASON,
        _arg("--urgency-source", dest="urgency_source", choices=("explicit_operator",), default=None,
             help="Attribution for an explicit core-update interruption (with --interrupt-target)"),
        _arg("--interrupt-target", dest="interrupt_target", default=None,
             help="40-hex minimum core target of that interruption (with --urgency-source)"),
    ], help="Release an active worker claim on a running task"),
    _cmd("move", [
        _TASK_ID,
        _arg("--runs-on", dest="runs_on", default=None,
             help="Comma-separated labels the next machine must have, e.g. pool:worker or macos"),
        _arg("--pool", action="store_true",
             help="Queue the card for any worker (= --runs-on pool:worker): the first worker with a free slot "
                  "claims it"),
        _arg("--host-reason", dest="host_reason", default=None,
             help="Required for any machine:<name> label: the state on that host that holds the card there "
                  "(a worktree, a stamp, unpublished work, a live process, Mac-only or hub-only work). Capacity "
                  "is never one: use --pool"),
        _arg("--reason", required=True, help="Why the card must move (logged as a `moved` event)"),
        _arg("--checkpoint", default=None,
             help="Resume notes (required when a card's own worker moves its running card)"),
        _arg("--force", action="store_true", help="Allow moving back to a machine the card already left"),
        _arg("--reclaim", action="store_true",
             help="The card is running here under another run: stop that run, then move it"),
        _json_flag(),
    ], help="Move a card to machines with other labels (runs_on): a worker moves its own running card, an operator "
            "moves a queued one or (--reclaim) one running here. Guards: kanban.move.max_moves, no unknown or held "
            "target, no ping-pong without --force"),
    _cmd("reassign", [
        _TASK_ID,
        _arg("profile", help="New profile name (or 'none' to unassign)"),
        _arg("--reclaim", action="store_true",
             help="Release any active claim before reassigning (required if task is running)"),
        _RECLAIM_REASON,
    ], help="Reassign a task to a different profile, optionally reclaiming first"),
    _cmd("diagnostics", [
        _arg("--severity", choices=["warning", "error", "critical"],
             help="Only show diagnostics at or above this severity"),
        _arg("--task", help="Only show diagnostics for one task id"),
        _json_flag(help="Emit JSON (structured) instead of the default human table"),
    ], aliases=["diag"], help="List active diagnostics on the current board"),
    _cmd("link", [_arg("parent_id"), _arg("child_id")], help="Add a parent->child dependency"),
    _cmd("unlink", [_arg("parent_id"), _arg("child_id")], help="Remove a parent->child dependency"),
    _cmd("supervisor-requests", [
        _arg("--priority-operation", choices=["prepare", "stage", "apply", "restore", "verify", "verify-restore", "migrate", "origins", "publish"],
             help="Operator-only attached priority migration; never executable by workers"),
        _arg("--host-map", help="Explicit origin-to-SSH transport/task/backup JSON for migrate"),
        _arg("--host-map-file", help="File containing the explicit migrate host-map JSON"),
        _arg("--writer", help="Explicit local primary gateway identity"),
        _arg("--manifest", help="Original manifest input file for stage"),
        _arg("--backup", help="New exclusive backup path for prepare/stage"),
        _arg("--exception", action="append", default=[], help="Reserved exception ID, low to high"),
        _arg("--attachment-id", type=int, help="Locally owned manifest attachment"),
        _arg("--digest", help="Exact attached manifest SHA256"),
        _arg("--allow-legacy-rollback", action="store_true", help="Restore exact above-cap originals; no effect after a capped restore (already restored)"),
        _arg("--drain", action="store_true",
             help="Execute queued requests as this board's non-worker supervisor executor"),
        _arg("--task", help="Only requests filed by this task id"),
        _arg("--id", type=int, help="Only this request id"),
        _arg("--settled", action="store_true",
             help="With --id: exit 0 once that request is done/rejected/failed, else 1 (a wake_check)"),
        _arg("--operator-log", action="store_true",
             help="List the operator-call log (calls on foreign cards, accepted and refused) instead"),
        _arg("--status", choices=["pending", "accepted", "done", "rejected", "failed"]),
        _json_flag(help="Emit JSON"),
    ], help="List (or --drain) graph-edit requests workers filed with kanban_supervisor_request"),
    _cmd("claim", [
        _TASK_ID,
        _arg("--ttl", type=int, default=kb.DEFAULT_CLAIM_TTL_SECONDS, help="Claim TTL in seconds (default: 900)"),
    ], help="Atomically claim a ready task (prints resolved workspace path)"),
    _cmd("comment", [
        _TASK_ID,
        _arg("text", nargs="+", help="Comment body"),
        _arg("--author", help="Author name (default: $HERMES_PROFILE or 'user')"),
        _arg("--max-len", type=int, help="Trim the stored comment body to this many characters"),
        _arg("--origin-gateway", default=None,
             help="Gateway the comment came from (the hermes-hub relay passes the sender). Default: the gateway "
                  "the author names (hub:gw:<gw>, <gw>/t_...), else this gateway"),
    ], help="Append a comment"),
    _cmd("wait-comment", [
        _TASK_ID,
        _arg("--after-id", type=int, default=0,
             help="Only comments with a higher id (the last comment id when you parked)"),
        _arg("--from-gateway", default=None,
             help="Only comments from this gateway: its origin_gateway, or any author spelling of it "
                  "(hub:gw:<gw>, <gw>, <gw>-chat, <gw>/t_...)"),
        _arg("--contains", default=None, help="Only comments whose body contains this text (case-insensitive)"),
        _arg("--from-card", default=None,
             help="Only comments written by this card's worker (it commented from inside its run, or the relay "
                  "authored it <gateway>/<task id>). Use it to wait for another card's answer instead of a phrase "
                  "the other card has to reproduce"),
    ], help="Wake-probe primitive for a reply: exit 0 a matching comment exists, 1 not yet, 2 cannot tell. "
            "Use it instead of matching task_comments.author (the park guard refuses that: author_probe)"),
    _cmd("attach", [
        _TASK_ID,
        _arg("path", help="Path to the local file to attach"),
        _arg("--content-type", help="MIME type (default: guessed from the file extension)"),
        _arg("--name", help="Stored filename (default: the source file's basename)"),
        _arg("--author", help="uploaded_by label (default: $HERMES_PROFILE or 'user')"),
    ], help="Attach a local file to a task"),
    _cmd("attachments", [_TASK_ID, _json_flag()], help="List a task's attachments"),
    _cmd("attach-rm", [_arg("attachment_id", type=int)], help="Delete an attachment by id"),
    _cmd("complete", [
        _arg("task_ids", nargs="+", help="One or more task ids (only --result applies to all of them)"),
        _arg("--result", help="Result summary"),
        _arg("--summary",
             help="Structured handoff summary for downstream tasks. Falls back to --result if omitted."),
        _arg("--metadata",
             help='JSON dict of structured facts (e.g. \'{"changed_files": [...], '
                  '"tests_run": 12}\'). Stored on the closing run.'),
        _arg("--force", action="store_true",
             help="Override the live-claim guard: complete a running, claimed task "
                  "even without owning its run (closes the worker's run)."),
    ], help="Mark one or more tasks done"),
    _cmd("edit", [
        _TASK_ID,
        _arg("--requires", metavar="CAPABILITIES",
             help="Orchestrator only: replace declared requirements with a comma-separated list; empty clears. "
                  "Idle open cards only; workspace/skill needs remain. Cannot combine with other edit flags."),
        _arg("--title", help="Replace the task title"),
        _arg("--body", help="Replace the task body"),
        _arg("--class", dest="admission_class", choices=["normal", "capacity", "urgent"],
             help="Admission class (capacity requires --frees)"),
        _arg("--frees", dest="admission_frees", help="Single-line frees: claim"),
        _arg("--priority", type=int, help="Replace priority (cap 1000; lowering requires --reason)"),
        _arg("--priority-bump", type=int, help="Atomically raise priority by this positive amount (cap 1000)"),
        _arg("--result", help="Backfilled task result text for a done task"),
        *_STEP_HANDOFF,
        _arg("--max-retries", type=int, metavar="N", dest="max_retries",
             help="Per-task failure circuit breaker: block on the Nth consecutive failure"),
        _arg("--goal-mode", choices=["on", "off"], dest="goal_mode",
             help="Run the worker in a goal loop (on) or single-shot (off)"),
        _arg("--review-required", choices=["on", "off"], help="Require review (workers cannot turn it off)"),
        _arg("--goal-max-turns", type=int, metavar="N", dest="goal_max_turns",
             help="Turn budget for goal-mode workers"),
        _arg("--max-runtime-seconds", type=int, metavar="N", dest="max_runtime_seconds",
             help="Per-task runtime cap in seconds"),
        _arg("--session-id", metavar="SID", dest="session_id",
             help="Backfill the originating chat session id (refuses to overwrite one without --force)"),
        _arg("--force", action="store_true", help="With --session-id: overwrite a non-empty session_id"),
        _arg("--completion-contract", metavar="CONTRACT", dest="completion_contract",
             help="Orchestrator only: re-point the completion gate (local-only, OWNER/REPO or an exact "
                  "PR URL) when the bound one can never pass, e.g. the repo moved or was suspended. "
                  "Requires --reason; can't be combined with other edit flags"),
        _arg("--reason", help="With --completion-contract: why (recorded as an event and a comment). "
                              "With --priority: why (required to lower it; recorded on the reprioritized event)"),
    ], help="Edit task fields, operator fields (retries, goal mode, runtime cap, session, completion "
            "contract), or recovery fields on an already-completed task"),
    _cmd("reopen", [
        _TASK_ID,
        _arg("--reason", required=True, help="Why the done task is reopened (event + comment)"),
    ], help="Reopen a done task: done -> ready (todo while a parent is open); refuses other states, "
            "except a stale blocked/scheduled copy of a done card"),
    _cmd("block", [
        _HUMAN_REQUEST,
        _arg("--metadata-only", action="store_true",
             help="Audit parked legacy classification only; requires reason and --human-request (null clears); never unblocks"),
        _TASK_ID,
        _arg("reason", nargs="*", help="Reason (also appended as a comment)"),
        _bulk_ids("block"),
        _arg("--kind", choices=sorted(kb.VALID_BLOCK_KINDS),
             help="Typed block reason. 'dependency' waits in todo (auto-promoted when "
                  "parents finish, no human); 'needs_input'/'capability' go to "
                  "blocked (human asks require --human-request); 'assignment' routes worker-local tool/backend "
                  "mismatches to a capable supervisor; 'transient' marks a maybe-flaky failure. "
                  "Repeated same-kind re-blocks after unblock route the task to "
                  "triage to break unblock loops. Omit for a generic block."),
        _NEED,
        *_WAKE_CHECK_ARGS(),
    ], help="Mark one or more tasks blocked"),
    _cmd("schedule", [
        _TASK_ID,
        _arg("reason", nargs="*", help="Reason/timing note (also appended as a comment)"),
        _bulk_ids("schedule"),
        _NEED,
        *_WAKE_CHECK_ARGS(),
    ], help="Park one or more tasks in Scheduled (waiting on time, not human input)"),
    _cmd("unblock", [
        _arg("--seen", dest="seen_comment_id", type=int, help="Newest comment id read; otherwise refuse foreign comments from the last 60 minutes."),
        _reason("Optional reason/note — recorded as a comment before unblocking. Quote multi-word reasons."),
        _TASK_IDS,
    ], help="Return blocked/scheduled/triage tasks to ready, or todo while parents remain open"),
    _cmd("request-review", [
        _TASK_ID,
        _arg("--summary", help="What was implemented and how it was verified — shown to the reviewer."),
        _arg("--reviewer", help="Optional reviewer profile; reassigns the task before review dispatch."),
        _arg("--metadata", help="JSON object with structured reviewer handoff facts."),
        _arg("--force", action="store_true",
             help="Override the live-claim guard: move a running, claimed "
                  "task to review even without owning its run (clears the worker's claim)."),
    ], help="Move a task to 'review' (implementation done, awaiting review) — NOT a block"),
    _cmd("request-changes", [_TASK_ID, _arg("reason", nargs="+", help="Concrete changes required before re-review")],
         help="Reviewer verdict: return the active review run to its implementer"),
    _cmd("reopen-review", [
        _TASK_IDS,
        _reason("Optional reason/note — recorded as a comment before reopening. Quote multi-word reasons."),
    ], help="Send one or more review tasks back for changes (review -> ready/todo)"),
    _cmd("resume", [
        _TASK_ID,
        _arg("--reason", required=True, help="Why this owner should continue its PR (recorded as an event + comment)"),
    ], help="Operator-only: let a READY card held by the active_pr respawn guard run once more"),
    _cmd("quota-release", [
        _TASK_ID,
        _arg("--reason", default="", help="Why the quota episode no longer holds (recorded as an event + comment)"),
        _arg("--probe", action="store_true",
             help="Release only when a live usage read shows headroom on every walled provider (else refuse, audited)"),
    ], help="Operator-only: end the card's quota episode (a stale 'resets at' hold) so it is admissible again"),
    _cmd("source-release", [
        _TASK_ID,
        _arg("--to", required=True, choices=("published", "unchanged"),
             help="Custody state to set (the branch on GitHub must equal the manifest commit)"),
        _arg("--by", default=None, help="Operator name recorded on the source_released event"),
        _json_flag(),
    ], help="Operator-only (hub, Board admin key): free workspace_source custody held by a dead or disabled holder"),
    _cmd("promote", [
        _arg("--seen", dest="seen_comment_id", type=int, help="Newest comment id read; otherwise refuse foreign comments from the last 60 minutes."),
        _TASK_ID,
        _arg("reason", nargs="*", help="Audit-trail reason (recorded on the task_events row)"),
        _bulk_ids("promote"),
        _arg("--dry-run", action="store_true", help="Validate the promotion without mutating state"),
        _arg("--json", dest="json", action="store_true", help="Emit machine-readable JSON result"),
    ], help="Manually move one or more todo/blocked/triage tasks to ready (recovery path)"),
    _cmd("spend-budget", [
        _TASK_ID,
        _arg("--runs", type=_nonnegative_int, help="This card's spending-run limit (0 = no limit)"),
        _arg("--cache-read-tokens", type=_nonnegative_int, help="This card's cache-read token limit (0 = no limit)"),
        _arg("--exempt", action="store_true", help="No spend budget for this card (standing cards)"),
        _arg("--clear", action="store_true", help="Back to the board's kanban.spend_budget_* limits"),
        _reason("Why (required for a change; recorded on the spend_budget_set event)"),
        _json_flag(),
    ], help="Show or set one card's own spend budget (kanban_spend_budget)"),
    _cmd("archive", [
        _arg("task_ids", nargs="*", help="Task ids to archive (default mode)"),
        _arg("--rm", dest="purge_ids", nargs="+",
             help="Permanently delete already-archived task ids from the board"),
    ], help="Archive one or more tasks"),
    _cmd("tail", [_TASK_ID, _arg("--interval", type=float, default=1.0)], help="Follow a task's event stream"),
    _cmd("dispatch", [
        _arg("--dry-run", action="store_true", help="Don't actually spawn processes; just print what would happen"),
        _arg("--max", type=int, help="Cap number of spawns this pass"),
        _arg("--failure-limit", type=int, default=kbd.DEFAULT_FAILURE_LIMIT,
             help=f"Auto-block a task after this many consecutive non-success attempts "
                  f"(spawn_failed, timed_out, or crashed; default: {kbd.DEFAULT_FAILURE_LIMIT})"),
        _json_flag(),
    ], help="One dispatcher pass: reclaim stale, promote ready, spawn workers"),
    _cmd("daemon", [
        _arg("--interval", type=float, default=60.0, help="Seconds between dispatch ticks (default: 60)"),
        _arg("--max", type=int, help="Cap number of spawns per tick"),
        _arg("--failure-limit", type=int, default=kbd.DEFAULT_FAILURE_LIMIT),
        _arg("--pidfile", help="Write the daemon's PID to this file on start"),
        _arg("--verbose", "-v", action="store_true", help="Log each tick's outcome to stdout"),
        # Escape hatch for hosts that truly cannot run the gateway; hidden from
        # --help so nobody casually keeps the double-dispatcher pattern alive.
        _arg("--force", action="store_true", help=argparse.SUPPRESS),
    ], help="DEPRECATED — dispatcher now runs in the gateway. Use `hermes gateway start`."),
    _cmd("watch", [
        _arg("--assignee", help="Only show events for tasks assigned to this profile"),
        _arg("--tenant", help="Only show events from tasks in this tenant"),
        _arg("--kinds",
             help="Comma-separated event kinds to include (e.g. 'completed,blocked,gave_up,crashed,timed_out')"),
        _arg("--interval", type=float, default=0.5, help="Poll interval in seconds (default: 0.5)"),
    ], help="Live-stream task_events to the terminal (Ctrl+C to exit)"),
    _cmd("stats", [_json_flag()], help="Per-status + per-assignee counts + oldest-ready age"),
    _cmd("queue", [_arg("--explain", metavar="TASK_ID", help="Score breakdown, lane position and pools for one card"),
                   _arg("--report", action="store_true", help="Capacity-class effect report (ready-wait p50)"),
                   _arg("--days", type=int, default=7, help="Report window in days (default 7)"),
                   _json_flag()], help="Ready/review dispatch order with hub admission score breakdowns"),
    _cmd("lanes", children=("lanes_action", [
        _cmd("show", [_arg("--hours", type=float, default=6.0, help="Trailing window for actual share (default 6)"),
                      _arg("--local", action="store_true", help="This board only, not the fleet replica"),
                      _json_flag()], help="Target against actual share, and what is backed up, per lane"),
        _cmd("held", [_arg("--lane"), _arg("--limit", type=int, default=200), _json_flag()],
             help="Cards waiting at a lane gate, oldest first"),
        _cmd("unclassified", [_arg("--limit", type=int, default=200), _json_flag()],
             help="Open cards no rule or parent placed in a lane"),
        _cmd("assign", [_TASK_ID, _arg("lane"), _reason("Why the card belongs there"),
                        _arg("--source", choices=("given", "llm"), default="given")], help="Move a card to a lane"),
        _cmd("admit", [_TASK_ID, _reason("Why it should run now")], help="Release a card held at a lane gate"),
        _cmd("pin", [_TASK_ID, _arg("--off", action="store_true", help="Remove the pin")],
             help="Keep the planner from reprioritizing a card"),
        _cmd("set", [_arg("lane"), _arg("--share", type=float), _arg("--admit-per-day", type=int),
                     _arg("--pause", action="store_true"), _arg("--resume", action="store_true")],
             help="Change a lane's share, daily admit budget or pause state"),
        _cmd("apply", [_arg("file", help="JSON action list, or - for stdin"),
                       _arg("--dry-run", action="store_true"), _json_flag()],
             help="Validate and apply a planner's action list"),
        _cmd("explain", [_TASK_ID, _json_flag()], help="A card's lane, where it came from and why it waits"),
    ]), help="Project lanes: the split of worker time between projects"),
    _cmd("notify-subscribe", [
        _TASK_ID,
        *_NOTIFY_TARGET,
        _arg("--user-id"),
        _arg("--user-id-alt"),
        _arg("--chat-type", choices=("dm", "group", "channel", "thread"),
             help="Originating source chat_type, recorded so the active-wake delivery "
                  "modes resolve the operator's real session. Omit to leave an "
                  "existing sub unchanged (new subs default to 'dm')."),
        _arg("--parent-chat-id",
             help="Parent channel ID for a thread or forum post, used for multiplex profile routing."),
        _arg("--guild-id",
             help="Discord guild ID, used for multiplex profile routing."),
        _arg("--notifier-profile",
             help="Profile gateway that owns/delivers this subscription (default: active profile)"),
        # choices: single source of truth shared with the DB/watcher enum.
        _arg("--delivery-mode", choices=kbn._NOTIFY_DELIVERY_MODES,
             help="How the kanban-notifier reacts to terminal events for this "
                  "subscription: 'notify' (passive message only; default), "
                  "'notify+wake' (message AND wake the destination gateway agent so "
                  "it reads the full board context and replies in its own voice), or "
                  "'wake' (wake the agent only, no passive message). Omit to leave an "
                  "existing subscription's mode unchanged (new subs default to 'notify')."),
    ], help="Subscribe a gateway source to a task's terminal events (used by /kanban subscribe in the gateway adapter)"),
    _cmd("notify-list", [_arg("task_id", nargs="?"), _json_flag()],
         help="List notification subscriptions (optionally for a single task)"),
    _cmd("notify-unsubscribe", [_TASK_ID, *_NOTIFY_TARGET], help="Remove a gateway subscription from a task"),
    _cmd("log", [_TASK_ID, _arg("--tail", type=int, help="Only print the last N bytes")],
         help="Print the worker log for a task (from <kanban-root>/kanban/logs/)"),
    _cmd("runs", [_TASK_ID, _json_flag(), *_run_state_args("filter runs by task_runs column")],
         help="Show attempt history for a task (one row per run: profile, outcome, elapsed, summary)"),
    _cmd("heartbeat", [
        _TASK_ID,
        _arg("--note", help="Optional short note attached to the heartbeat event"),
    ], help="Emit a heartbeat event for a running task (worker liveness signal)"),
    _cmd("assignees", [_json_flag()],
         help="List known profiles + per-profile task counts (union of ~/.hermes/profiles/ and current assignees on the board)"),
    _cmd("context", [_TASK_ID],
         help="Print the full context a worker sees for a task (title + body + parent results + comments)."),
    _cmd("specify", _triage_sweep_args("specify", "Specify", "specifier"),
         help="Flesh out a triage-column task into a concrete spec (title + "
              "body) and promote it to todo. Uses the auxiliary LLM "
              "configured under auxiliary.triage_specifier."),
    _cmd("decompose", (*_triage_sweep_args("decompose", "Decompose", "decomposer"),
                       _arg("--force", action="store_true",
                            help="Decompose even a card that already ran or was escalated by "
                                 "block-loop detection (normally refused: it redoes shipped work)")),
         help="Decompose a triage-column task into a graph of child tasks "
              "routed to specialist profiles by description. Falls back "
              "to specify-style single-task promotion when the task "
              "doesn't benefit from fan-out. Uses auxiliary.kanban_decomposer."),
    _cmd("capabilities", children=("cap_action", [
        _cmd("list", help="Capabilities held by this gateway and its hermes-hub peers, with check age"),
        _cmd("refresh", help="Run every capability probe now and rewrite kanban/capabilities.json"),
        _cmd("locate", [_arg("needs", nargs="+", metavar="CAPABILITY")],
             help="Where each capability is held (exit 1 if some need has no fresh holder)"),
    ]), help="Gateway capability registry used for cross-gateway owner-card routing"),
    _cmd("gc", [
        _arg("--worktree-retention-days", type=_nonnegative_int, default=None,
             help="Archive committed work and reap terminal task worktrees after N days (config/default: 7; 0 disables)"),
        _arg("--event-retention-days", type=_nonnegative_int, default=30,
             help="Delete task_events older than N days for terminal tasks (default: 30; 0 disables)"),
        _arg("--log-retention-days", type=_nonnegative_int, default=30,
             help="Delete worker log files older than N days (default: 30; 0 disables)"),
        _arg("--scratch-budget-seconds", type=_nonnegative_int, default=None,
             help="Start no new scratch archive after N seconds; the rest wait for the next pass "
                  "(default: 1200; 0 = no limit)"),
    ], help="Garbage-collect archived-task workspaces, old events, and old logs"),
    _cmd("sync", children=("sync_action", [
        _cmd("status", [_json_flag()], help="Mode, service board, cursor, outbox depth and last error"),
        _cmd("attach", [_arg("--mode", choices=("shadow", "primary", "off"), default="shadow",
                             help="shadow (default): push, apply the feed to a shadow copy; primary: the service is "
                                  "the source of truth; off: stop capturing")],
             help="Bind the board to the central Board service under this gateway's hub name"),
        _cmd("import", help="Queue every existing row of the board for upload (idempotent)"),
        _cmd("diff", [_json_flag()], help="Compare this gateway's live cards with the shadow copy (exit 1 if not clean)"),
        _cmd("run", [_json_flag()], help="Run one sync round for every synced board now"),
        _cmd("links", help="Reconcile this gateway's chat links (which chats each card belongs to) now"),
        _cmd("links-diff", [_json_flag(), _arg("--settle", type=float, default=None, metavar="SECONDS",
                                                help=f"Re-read for up to SECONDS (default {DEFAULT_SETTLE_S:g}); "
                                                     "only differences "
                                                     "every read lists count, the rest are in_flight (0: one read)")],
             help="Compare chat links from the origin ledger with the synced links in the replicas (exit 1 if not clean)"),
        _cmd("machine-links", [_json_flag(), _arg("--release", action="store_true",
                                                  help="Release them now (default: dry run)")],
             help="Unreleased chat links to this gateway's worker/cron/one-shot/subagent sessions (dry run)"),
        _cmd("reconcile", [_json_flag()],
             help="After the board file lost rows (a recovery): upload rows the service lacks, restore this "
                  "gateway's rows the file lost from the service snapshot, then diff (exit 1 if not clean)"),
        _cmd("refill-history", [_json_flag(), _arg("task_ids", nargs="*", metavar="TASK_ID",
                                                   help="Cards to refill (default: every open copied card)")],
             help="Refill events an older pickup cut off local copies of other gateways' cards, from the replica "
                  "(by uid; never invents rows; safe on running cards)"),
        _cmd("reserved-key", [_json_flag(), _arg("task_id", metavar="TASK_ID"),
                              _arg("--new-key", required=True, metavar="KEY",
                                   help="Replacement idempotency key (non-empty, not pickup-stall:/worker-resources-unknown:)"),
                              _arg("--reason", required=True, metavar="TEXT",
                                   help="Why this card is corrected (required, non-blank; bound to the token and recorded)"),
                              _arg("--apply", action="store_true",
                                   help="Apply the correction (default: read-only preview)"),
                              _arg("--expect", metavar="TOKEN", default=None,
                                   help="The preview's token; --apply refuses if the card, its rows or its outbox changed")],
             help="Correct a never-created local card's reserved idempotency key (preview by default; see "
                  "hermes_cli/kanban_sync_reserved_key.py)"),
    ]), help="Central Kanban sync (kanban.sync.*): attach, import, status, diff"),
    _cmd("recover", [
        _json_flag(help="Emit the recovery report as JSON"),
        _arg("--dry-run", action="store_true", help="Rebuild and report the loss, but do not write the board"),
        _arg("--no-fill", action="store_true",
             help="Do not fill rows the damaged file lost from the newest healthy quarantine backup"),
    ], help="Rebuild a board whose table b-trees are corrupt (what `repair` refuses), in place, with a loss report",
         description=(
             "For integrity_check failures that REINDEX cannot fix. Runs in a fresh process under the board's "
             "recovery lock (a concurrent recovery exits 'busy'): snapshots the damaged file with the SQLite "
             "backup API (kept as <db>.recover-<ts>.damaged.bak), reads every table forward, backward and through "
             "each index, fills rows still missing from the newest quarantine backup that passes quick_check, "
             "writes <db>.recovery-<ts>.json with per-table loss counts, and writes the rebuilt board back INTO "
             "the live file with the backup API, so open connections stay valid. Never `mv` a rebuilt copy over a "
             "live kanban.db: every process with the old file open keeps writing to it. Also files a follow-up "
             "card and runs `sync reconcile` for synced boards. The gateway dispatcher starts it automatically "
             "when kanban.auto_recover_corruption is true (default)."
         )),
    _cmd("repair", [_json_flag(help="Emit the repair report as JSON")],
         help="Check kanban.db integrity and auto-repair index-only corruption",
         description=(
             "Runs PRAGMA integrity_check on the board's DB and reports the result. When the "
             "failure consists only of index-scoped errors ('wrong # of entries in index <name>' / "
             "'row N missing from index <name>'), the corrupt file is quarantined to a "
             ".corrupt.<hash>.bak sibling first and the damaged indexes are rebuilt with REINDEX — "
             "the same narrow auto-repair the connect-time guard applies. Any other corruption "
             "class is reported and left untouched (fail-closed). Exits 0 when the DB is healthy "
             "or was repaired, non-zero when it is still corrupt."
         )),
]


def build_parser(parent_subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Attach the ``kanban`` subcommand tree; returns the ``kanban`` parser."""
    kanban_parser = parent_subparsers.add_parser(
        "kanban",
        help="Multi-profile collaboration board (tasks, links, comments)",
        description="Durable SQLite-backed task board shared across Hermes profiles. "
                    "Tasks are claimed atomically, can depend on other tasks, and "
                    "are executed by a named profile in an isolated workspace. "
                    "See https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban.",
    )
    # --board scopes every subcommand to one board's DB; when omitted the
    # resolution is HERMES_KANBAN_BOARD, then the persisted current-board
    # file, then "default" (kanban_db.get_current_board()).
    kanban_parser.add_argument("--board", default=None, metavar="<slug>",
                               help="Board slug to operate on. Defaults to the current board (set "
                                    "via `hermes kanban boards switch <slug>` or the "
                                    "HERMES_KANBAN_BOARD env var). Use `hermes kanban boards "
                                    "list` to see all boards.")
    actions = kanban_parser.add_subparsers(dest="kanban_action")
    _add_commands(actions, _SPECS)
    # Root-scoped: bypasses ``kanban_command`` (board override / auto-init) via the subparser default.
    actions.choices["quiet-hold"].set_defaults(func=lambda a: __import__("hermes_cli.kanban_quiet", fromlist=["entry"]).entry(a))
    kanban_parser.set_defaults(_kanban_parser=kanban_parser)
    return kanban_parser
