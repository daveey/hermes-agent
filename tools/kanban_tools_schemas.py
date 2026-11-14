"""Tool schemas for tools.kanban_tools (model-facing; strings are byte-frozen)."""
from __future__ import annotations

import json
from typing import Any

import hermes_completion_receipt as hcr
from hermes_cli.kanban_workflow import ARCHIVED, DEFAULT_WORKFLOW

_DESC_TASK_ID_DEFAULT = (
    "Task id. If omitted, defaults to HERMES_KANBAN_TASK from the env — the "
    "task the dispatcher spawned you to work on. That default only exists for "
    "a dispatcher-spawned worker; any other caller has no default and must "
    "pass an explicit task_id (use kanban_list to discover ids)."
)

_DESC_BOARD = (
    "Kanban board slug to target. When omitted, the call resolves the "
    "active board the usual way: HERMES_KANBAN_DB env → "
    "HERMES_KANBAN_BOARD env → the 'current' symlink under the kanban "
    "home → 'default'. Pass an explicit slug only when the caller (e.g. "
    "a Telegram routing layer) needs to override the env-pinned active "
    "board for this one call. A dispatcher worker's explicit slug must match "
    "its pinned database's board; a mismatch returns board_pinned rather than "
    "silently using the pinned board (existing exact-target owner-wake grants excepted)."
)


def _prop(type_: str, description: str) -> dict[str, str]:
    return {"type": type_, "description": description}


def _board_schema_prop() -> dict[str, str]:
    """Schema fragment for the optional ``board`` parameter (one place to tweak)."""
    return _prop("string", _DESC_BOARD)


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    """Build a tool schema; every kanban tool takes an optional trailing ``board``."""
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {**properties, "board": _board_schema_prop()},
            "required": required,
        },
    }


KANBAN_ORIGIN_ADOPT_SCHEMA = _schema(
    "kanban_origin_adopt",
    "Attach an existing exact owner task to an origin via the authenticated courier. "
    "Requires trusted courier context; never creates or completes a task. A pending "
    "result is not a successful handoff: wait for the origin-owned receipt.",
    {"task_id": _prop("string", "Exact existing task ID, never a prefix.")},
    ["task_id", "board"],
)


KANBAN_SHOW_SCHEMA = _schema(
    "kanban_show",
    (
        "Read a task's full state: body, assignee, parent handoffs, attempts, comments. "
        "It also carries the title and recent events. Use this to (re)orient yourself before "
        "starting work, especially on retries. The response includes a "
        "pre-formatted ``worker_context`` string suitable for inclusion "
        "verbatim in your reasoning. Outside a dispatcher-spawned worker "
        "there is no default task: a bare call returns a pointer to "
        "``kanban_list`` instead of task state."
    ),
    {
        "task_id": _prop("string", _DESC_TASK_ID_DEFAULT),
    },
    [],
)

KANBAN_LIST_SCHEMA = _schema(
    "kanban_list",
    (
        "List Kanban task summaries so an orchestrator can discover work to route. Supports the same core filters as the CLI: assignee, "
        "status, tenant, include_archived, and limit. Returns compact rows "
        "with ids, title, status, assignee, priority, parent/child ids, and "
        "counts. Bounded to 50 rows by default, 200 max, with truncation "
        "metadata. Also recomputes ready tasks before listing, matching the "
        "CLI. Orchestrator-only — dispatcher-spawned task workers never see "
        "this tool."
    ),
    {
        "assignee": _prop("string", "Optional assignee/profile filter."),
        "status": {
            "type": "string",
            "enum": [*DEFAULT_WORKFLOW.keys(), ARCHIVED],
            "description": "Optional task status filter.",
        },
        "tenant": _prop("string", "Optional tenant/project namespace filter."),
        "include_archived": _prop("boolean", "Include archived tasks. Defaults to false."),
        "limit": _prop("integer", "Optional maximum rows to return (default 50, max 200)."),
    },
    [],
)


def _operator_delivery_schema() -> dict[str, Any]:
    """evidence.operator_delivery as the validator admits it: exactly hcr.OPERATOR_DELIVERY_FIELDS."""
    text = {"type": "string", "minLength": 1, "maxLength": hcr.OPERATOR_DELIVERY_TEXT_MAX,
            "pattern": hcr.TEXT_PATTERN}
    special = {"comment_id": {"type": "integer", "minimum": 1},
               "content_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}}
    return {
        "type": "object", "additionalProperties": False, "required": list(hcr.OPERATOR_DELIVERY_FIELDS),
        "description": "Optional: the applied operator result's operator_delivery, verbatim",
        "properties": {key: dict(special.get(key, text)) for key in hcr.OPERATOR_DELIVERY_FIELDS},
    }


def _completion_receipt_schema() -> dict[str, Any]:
    """metadata.completion_receipt v1, from the validator's own limits (hermes_completion_receipt) so the
    advertised shape never drifts from what the card writer admits (self-heal agent-f82d813de9)."""
    example = {"version": 1, "items": [{
        "scope": "README install section from the card body",
        "disposition": "delivered",
        "evidence": [{"ref": "commit 1a2b3c4 on origin/main; test_install passed",
                      "observed_at": "2026-11-13T06:40:00+00:00", "verification": "independently_checked"}],
        "remaining": [{"scope": "Windows install notes", "state": "deferred", "owner": None,
                       "reason": "not in this card's scope"}]}]}
    # No maxLength: llama.cpp's grammar converter rejects any bound >= 2000 (MAX_TEXT, MAX_REF) and refuses the
    # whole request; the card writer enforces the caps, and the description states them.
    text = {"type": "string", "minLength": 1, "pattern": hcr.TEXT_PATTERN}
    ref = {"type": "string", "minLength": 1, "pattern": hcr.TEXT_PATTERN}
    return {
        "type": "object", "additionalProperties": False, "required": ["version", "items"],
        "description": (
            "Optional v1 per-scope receipt. Include it when this completion claims delivery of a scoped "
            "artifact or ask (a fix, file, PR, reply, setting): one item per scope with its disposition "
            "(delivered, superseded with superseded_by, contained = mitigated not fixed, rejected), evidence "
            "refs with the timezone-aware ISO-8601 time you observed each (a naive timestamp is refused) and "
            "verification independently_checked (you re-ran or read it back this run) or reported (taken from "
            "someone's word), and what remains. A cross-card operator delivery claim attaches the applied "
            "result's operator_delivery object to its evidence. Remaining "
            "pending work needs an open owner card you created or linked, as @task:<board>@<gateway>/<id>; "
            "deferred work may have owner null with a reason. Chat close-outs relay this instead of guessing "
            f"from the summary. Text fields hold at most {hcr.MAX_TEXT} characters, refs and owners "
            f"{hcr.MAX_REF}; the whole receipt's canonical JSON at most {hcr.MAX_BYTES} bytes. Example: "
            + json.dumps(example, separators=(",", ":"))),
        "properties": {
            "version": {"type": "integer", "enum": [1]},
            "items": {"type": "array", "minItems": 1, "maxItems": hcr.MAX_ITEMS, "items": {
                "type": "object", "additionalProperties": False,
                "required": ["scope", "disposition", "evidence", "remaining"],
                "properties": {
                    "scope": text,
                    "disposition": {"type": "string", "enum": list(hcr.DISPOSITIONS)},
                    "superseded_by": ref,
                    "evidence": {"type": "array", "minItems": 1, "maxItems": hcr.MAX_EVIDENCE, "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["ref", "observed_at", "verification"],
                        "properties": {
                            "ref": ref,
                            "observed_at": {"type": "string", "maxLength": 64, "pattern": hcr.TEXT_PATTERN,
                                            "description": (
                                "ISO-8601 with timezone, e.g. 2026-11-13T06:40:00+00:00")},
                            "verification": {"type": "string", "enum": list(hcr.VERIFICATIONS)},
                            "operator_delivery": _operator_delivery_schema(),
                        }}},
                    "remaining": {"type": "array", "maxItems": hcr.MAX_REMAINING, "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["scope", "state", "owner", "reason"],
                        "properties": {
                            "scope": text,
                            "state": {"type": "string", "enum": ["pending", "deferred"]},
                            "owner": {"type": ["string", "null"], "pattern": hcr.TEXT_PATTERN},
                            "reason": text,
                        }}},
                }}},
        },
    }


KANBAN_COMPLETE_SCHEMA = _schema(
    "kanban_complete",
    (
        "Mark your current task done with a structured handoff for others. Prefer ``summary`` for a "
        "human-readable 1-3 sentence description of what you did; put "
        "machine-readable facts in ``metadata`` (changed_files, "
        "tests_run, decisions, findings, etc). At least one of "
        "``summary`` or ``result`` is required. If you created new "
        "tasks via ``kanban_create`` during this run, list their ids "
        "in ``created_cards`` — the kernel verifies them so phantom "
        "references are caught before they leak into downstream "
        "automation. If you produced deliverable files (charts, PDFs, "
        "spreadsheets, generated images), list their absolute paths "
        "in ``artifacts`` — the gateway notifier will upload them as "
        "native attachments to the human who subscribed to the task, "
        "so the deliverable lands in their chat alongside the summary "
        "instead of being a path they have to fetch by hand. PR contracts and metadata.published_pr "
        "require a landing disposition: MERGED or CLOSED, or for OPEN an open same-board card "
        "bound to the exact PR in metadata.landing_owner, or metadata.pr_left_open={\"reason\": "
        "\"<nonblank reason>\"}. OPEN/MERGED still require exact-head required CI; contract_waiver "
        "only waives absent required checks, never landing. Merge now with exact-head required CI green "
        "and every review comment (bots too) answered (standing approval), or create the landing card. "
        "The hub batch only installs merged main; it never merges PRs. A review-only card cites the "
        "reviewed PR in metadata.pr or metadata.reviewed_pr, never metadata.published_pr (it binds "
        "this card's completion contract to that PR permanently). When the completion claims delivery "
        "of a scoped artifact or ask, add metadata.completion_receipt (shape in the metadata schema): "
        "what each scope's disposition is, with evidence and remaining work."
    ),
    {
        "task_id": _prop("string", _DESC_TASK_ID_DEFAULT),
        "summary": _prop("string", (
                "Human-readable handoff, 1-3 sentences. Appears in "
                "Run History on the dashboard and in downstream "
                "workers' context."
        )),
        "metadata": {**_prop("object", (
                "Free-form dict of structured facts about this "
                "attempt — {\"changed_files\": [...], \"tests_run\": 12, "
                "\"findings\": [...]}. Surfaced to downstream "
                "workers alongside ``summary``."
        )), "properties": {"completion_receipt": _completion_receipt_schema()}},
        "result": _prop("string", (
                "Short result log line (legacy field, maps to "
                "task.result). Use ``summary`` instead when "
                "possible; this exists for compatibility with "
                "callers that still set --result on the CLI."
        )),
        "created_cards": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Optional structured manifest of task ids you "
                "created via ``kanban_create`` during this run. "
                "The kernel verifies each id exists and was "
                "created by this worker's profile; any phantom "
                "id blocks the completion with an error listing "
                "what went wrong (auditable in the task's events). "
                "Only list ids you got back from a successful "
                "``kanban_create`` call — do not invent or "
                "remember ids from prose. Omit the field if you "
                "did not create any cards."
            ),
        },
        "artifacts": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Optional absolute paths to deliverables produced this run "
                "(charts, PDFs, spreadsheets, images, archives), not intermediate "
                "files. The notifier sends native attachments to the subscribed "
                "chat with the completion notice. Completion records the executing "
                "machine, path and SHA256. Existing files up to 25 MB are "
                "copied to card attachments and replicated to the home board; "
                "larger scratch files are retained on the executing machine. "
                "Missing paths (including scratch paths) produce visible "
                "receipt warnings, not successful file-delivery claims. "
                "Failures preserving existing scratch bytes keep the task "
                "in-flight. Native chat uploads depend on local byte availability."
            ),
        },
    },
    [],
)

_NEEDS_PROP = {
    "needs": {"type": "array", "items": {"type": "string"}, "description": (
        "Optional. Gateway capabilities the wall needs, namespaced: 'aws:<account>:<action-or-role>', "
        "'ssh:<host>', 'github:<owner>:<scope>', 'token-broker:<scope>', 'sudo:nopasswd', "
        "'browser:signed-in'. When this gateway lacks one and another gateway holds it (live-checked "
        "hermes-hub capability registry), ONE owner card is opened on that gateway with your reason as "
        "its scoped action, and this card parks on it (no human ask; the owner card asks David only if "
        "the action exceeds the approved plan). A need no gateway holds is refused with a typed error: "
        "then use kind='needs_input' with a human_request.")},
}

_WAKE_CHECK_PROPS = {
    "wake_check": _prop("string", (
        "Shell command probed by the dispatcher while the card is parked (bash -lc, 30 s "
        "timeout, cwd = the task workspace): exit 0 = the condition holds and the card wakes "
        "at once (its stdout reaches the next run), exit 1 = not yet. Any other exit 3 times in "
        "a row wakes the card to fix the probe. REQUIRED for a wait on any readable fact (a DB "
        "row, a git tag, a file, a log line, a service state) that wakes more than 2 h out, e.g. "
        "`gh api repos/O/R/tags --jq '.[].name' | grep -q '^v2'`. A probe alone is not a producer: "
        "one that names no card or CI run is refused (opaque_probe_wait) unless `awaiting` says who "
        "will make it pass. Never filter task_comments by author (refused: author_probe; a relayed "
        "reply's author is 'hub:gw:<gateway>'): wait for a reply with `hermes kanban wait-comment <task> "
        "--after-id <last id> --from-gateway <gw>`. `hermes kanban show`/`list` read only this host's "
        "board. For a card on another gateway, use `hermes kanban wait-check task <id> --gateway <gw>`, "
        "or omit wake_check (a card wait gets that probe automatically). If that card is already "
        "blocked, in review or in triage, add `--from <that status>`. Those statuses count as met, so "
        "without it the probe passes at once. A wait on another card on "
        "this board is a parent link (kanban_link), not a park. On a park with blocker_id the "
        "check must test only the wall: its exit 0 clears the shared blocker and wakes every "
        "card behind it, so never OR it with unrelated probes (Slack, other cards). A check "
        "set by a park without blocker_id never clears a shared blocker."
    )),
    "check_every": {"type": "integer", "description": (
        "Seconds between wake_check probes (default 900, min 120, max 21600)."
    )},
    "clock_only": {"type": "boolean", "description": (
        "Set true only for a pure time wait with no fact to probe (a soak window, a calendar "
        "date, a timed re-measure). A park more than 2 h out needs wake_check or clock_only."
    )},
    "awaiting": {
        "type": "object",
        "description": (
            "Who produces the condition this park waits on. A wait needs a producer; a probe is "
            "not one. kind: 'card' (ref=<task id>, gateway=<name> when it lives on another "
            "gateway; it must be live, and a wake probe is added for you), 'ci' / 'deploy' "
            "(ref=<run, job or release id/URL>), 'external' (ref=<vendor or ticket>, why=<evidence>), "
            "'clock' (a real window: soak, cooldown, release slot). 'human' is always refused: a "
            "wait on David is kanban_block kind='needs_input' with human_request. A condition only "
            "an agent action changes (a grant, a permission, an install, a fix) is work, not a "
            "wait: it is refused (agent_action_wait) unless ref names the owner card doing it. "
            "Omitted: classified from the reason. The third park in a row on the same premise "
            "goes to the supervisor as an owner card instead of parking again."
        ),
        "properties": {
            "kind": {"type": "string", "enum": ["card", "ci", "deploy", "external", "clock", "human"]},
            "ref": {"type": "string"},
            "why": {"type": "string"},
            "gateway": {"type": "string"},
            "board": {"type": "string"},
        },
        "required": ["kind"],
        "additionalProperties": False,
    },
}


HUMAN_REQUEST = {
    "type": "object",
    "description": (
        "Explicit actionable human request. Omit for operational problems: missing metadata, "
        "capability failure or sudo authentication failure alone is NOT a new approval ask. "
        "Supply a concrete action/decision, why the agent cannot do it, supported interaction, "
        "scope and observed evidence. Standing authorization is not OS authentication; use a "
        "supported user-terminal flow when required, never request passwords in chat. This "
        "metadata never grants execution permission or completes/unblocks the task."
    ),
    "properties": {
        "action": {"type": "string", "minLength": 1, "description": (
            "The ask line David sees verbatim on the card and in chat notices: the exact thing to do and "
            "where, then what unblock and archive mean, e.g. \"Reply A or B in this chat. Unblock = build "
            "the pick; archive = drop it.\" An action missing either meaning is refused.")},
        "why_human": {"type": "string", "minLength": 1},
        "interaction": {"type": "string", "enum": ["approval", "reply", "user_terminal", "physical", "external"]},
        "scope": {"type": "string", "minLength": 1},
        "evidence": {"type": "string", "minLength": 1},
        "choices": {
            "type": "array", "minItems": 2, "maxItems": 6,
            "description": "Optional decision buttons David can click instead of typing, e.g. "
                           "[{\"label\": \"Ship it\", \"value\": \"ship\"}, {\"label\": \"Hold\", \"value\": \"hold\"}].",
            "items": {"type": "object", "properties": {
                "label": {"type": "string", "minLength": 1, "maxLength": 60},
                "value": {"type": "string", "minLength": 1, "maxLength": 120}},
                "required": ["label", "value"], "additionalProperties": False},
        },
        # no maxLength: llama.cpp's grammar converter rejects >=2000 (#131278); kanban_human_request enforces
        # RESOLVED_CHECK_MAX_CHARS server-side.
        "resolved_check": {"type": "string", "minLength": 1, "description": (
            "Optional shell probe (bash -lc, 30 s) that tells when David's step is done even if he never "
            "unblocks: exit 0 = done, 1 = not yet, anything else = broken. It runs once when you file the "
            "ask: exit 0 refuses the ask (ask_already_resolved: the state already holds, continue the work), "
            "a broken probe is refused (resolved_check_broken). While the card is blocked the dispatcher "
            "re-runs it every resolved_check_every seconds; exit 0 unblocks the card with a comment telling "
            "the next run to verify and continue, not redo the step. Examples: device button "
            "`curl -sf http://hub.local:8080/api/devices/watch | jq -e '.paired == true'`; Settings toggle "
            "`defaults read com.apple.AppleMultitouchTrackpad Clicking 2>/dev/null | grep -qx 1`.")},
        "resolved_check_every": {"type": "integer", "minimum": 120, "maximum": 21600, "description": (
            "Seconds between resolved_check runs while blocked (default 900).")},
    },
    "required": ["action", "why_human", "interaction", "scope", "evidence"],
    "additionalProperties": False,
}


_BLOCKER_ID_DESC = (
    "Optional. Set ONLY when the wall is outside this card and would stop other "
    "cards too (CI billing/quota refused, a provider or service outage, a release "
    "pipeline down): a short slug such as 'github-actions-billing'. Reuse an id "
    "from 'Open shared blockers' in your context when it is the same wall; when that "
    "entry is scoped (provider=X, not 'all'), pass blocker_provider=X with the reused id, "
    "every time: a reassertion without it (or with another provider) makes the id "
    "board-wide and holds every provider's cards. The "
    "dispatcher then holds every ready card behind it and runs one probe card per "
    "interval. The blocker clears only on evidence the wall is gone: a card behind "
    "it completes or hands off for review, a park passes clear_blocker=true, a run "
    "names a different blocker_id, or a wake_check set by a park that carried this id exits 0. "
    "Then every parked card behind it wakes. A later park WITHOUT the id (by you or "
    "a parent chat, via kanban_block, kanban_schedule or kanban_continue) does NOT "
    "clear it: the card stays behind the blocker."
)
_CLEAR_BLOCKER_PROP = {
    "blocker_provider": _prop("string", "Optional provider affected by blocker_id. Other providers may proceed; omit for a provider-independent wall. An id asserted anywhere without a provider (or with a different one) in the last 14 days stays board-wide: use a new id for a narrower wall."),
    "blocker_model": _prop("string", "Optional model that observed the wall (informational, not an applicability restriction)."),
    "blocker_lane": _prop("string", "Optional lane that observed the wall (informational, not an applicability restriction)."),
    "clear_blocker": {"type": "boolean", "description": (
        "Optional, default false. True records that the shared blocker this card is "
        "parked behind is gone (you checked it), although this run parks for another "
        "reason; that clears the blocker and wakes the other cards behind it. Never "
        "combine with blocker_id."
    )},
}


KANBAN_BLOCK_SCHEMA = _schema(
    "kanban_block",
    (
        "Stop work on this task and route it according to WHY you're stuck. "
        "This ends the current run: after a successful block, do not call "
        "lifecycle tools or kanban_link/kanban_unlink on this card again; write "
        "your final reply. "
        "Set ``kind`` to say which: 'dependency' (waiting on another task — "
        "goes to todo and auto-resumes when that task finishes, no human "
        "needed), 'needs_input' (a decision/answer only David can give; needs "
        "``human_request``), 'capability' (a hard wall no agent can clear: needs "
        "``human_request`` for David's step, or ``needs`` when another gateway "
        "holds the access), 'assignment' (this card needs another configured profile's tools: "
        "reassigns it to kanban.orchestrator_profile or the creator's profile when that differs from the "
        "assignee; refused with no_capable_supervisor on single-profile hosts), 'transient' (a flaky "
        "failure that may clear), or "
        "'time' (waiting on a clock, a cron, a soak or a release, not a person: "
        "parks the card 'scheduled' with a wake time, never as a human block). "
        "``reason`` is shown to the human on the board and, in Desktop, in a "
        "panel above the managing chat's composer as the steps to unblock it. "
        "If a task keeps "
        "getting unblocked and re-blocked for the same reason, it is "
        "auto-escalated to triage. Use for genuine blockers only — don't "
        "block on things you can resolve yourself. Waiting on time or an "
        "external event (a cap, a soak, a release window) is not a block: use "
        "kind='time' (put SCHEDULED_UNTIL=<ISO8601> in the reason to set the wake; "
        "default 24h), since a blocked card marks its chat as needing a human. "
        "A timed wait needs a producer, named in ``awaiting`` (a live card, a CI "
        "run, a vendor ticket, a real clock window); ``wake_check`` only says when "
        "to look, and a park more than 2 h out without one is refused unless "
        "``clock_only`` is true. Agent-action conditions (a grant, a permission, "
        "an install, a fix) are work, not waits: create the owner card on the "
        "gateway that holds the capability and point ``awaiting`` at it, declare "
        "``needs``, or use kanban_supervisor_request for archive/unlink/resume/retry_limit "
        "on your creator tree ('assignment' only when a different profile is configured). A wait on David is kind='needs_input' with "
        "``human_request``, never a park. A wait on another card on this board is "
        "a parent link (kanban_link), not a park."
    ),
    {
        "task_id": _prop("string", _DESC_TASK_ID_DEFAULT),
        "reason": _prop("string", (
                "The human's unblock instructions. Lead with the ask, not "
                "with what you finished. The ask itself goes in human_request "
                "(a needs_input/capability reason without one is refused); here "
                "write the numbered steps the human must perform (\"1) ... 2) ...\"), "
                "naming the exact device, app, setting, page or command, then "
                "what to do when done (e.g. \"then unblock this card\" or "
                "\"reply with the value\"). For a decision, state the question "
                "and the options. Keep it short; the human has the board and "
                "can ask follow-ups via comments."
        )),
        "human_request": HUMAN_REQUEST,
        "kind": {
            "type": "string",
            "enum": ["dependency", "needs_input", "capability", "assignment", "transient", "time"],
            "description": (
                "Why you're blocked. 'dependency' waits in todo and "
                "resumes automatically when an incomplete parent finishes; "
                "if no parent is open it is a timed wait (scheduled). 'time' "
                "parks scheduled until SCHEDULED_UNTIL=<ISO8601> in the reason "
                "(default 24h). 'assignment' reassigns to a different configured profile "
                "(kanban.orchestrator_profile or the creator's), else no_capable_supervisor. "
                "'needs_input', 'capability', 'transient' or an omitted kind with no "
                "human_request, wake_check, clock_only or blocker_id is refused "
                "(agent_owned_block): a step for David needs human_request; agent "
                "work is an owner card you create and park on, ``needs``, or "
                "kanban_supervisor_request; 'assignment' only when a different profile "
                "is configured; a wait is 'time'."
            ),
        },
        "blocker_id": _prop("string", _BLOCKER_ID_DESC + (
            " With SCHEDULED_UNTIL, kind='time', wake_check or clock_only the card parks "
            "'scheduled' with that wake and stays behind the blocker; with none of them it "
            "parks 'scheduled' and wakes after the shared probe interval. With human_request "
            "it stays a visible block."
        )),
        **_CLEAR_BLOCKER_PROP,
        **_NEEDS_PROP,
        **_WAKE_CHECK_PROPS,
    },
    ["reason"],
)

KANBAN_SCHEDULE_SCHEMA = _schema(
    "kanban_schedule",
    (
        "Park your current task as 'scheduled' while it waits for time or an event. "
        "This ends the current run and makes the "
        "task non-dispatchable until an orchestrator unblocks it, or until the "
        "time named by a ``SCHEDULED_UNTIL=<ISO8601>`` marker in ``reason`` "
        "(default 24h), or until ``wake_check`` exits 0. A wait needs a producer, "
        "named in ``awaiting``: a probe is not one. A park more than 2 h out "
        "without ``wake_check`` is refused unless ``clock_only`` is true. "
        "Agent-action conditions (a grant, a permission, an install, a fix) are "
        "work, not waits: they are refused (agent_action_wait) unless ``awaiting`` "
        "names the owner card doing it (create it on the gateway that holds the "
        "capability), and a wait on David is a kanban_block needs_input ask, never "
        "a park. A wait on another card on this board is a parent link "
        "(kanban_link), not a park. Without blocker_id a card already "
        "behind a shared blocker stays behind it when parked here (the park says "
        "nothing about the wall), and the blocker stays open for every other card; "
        "pass clear_blocker=true only when you saw the wall gone."
    ),
    {
        "task_id": _prop("string", _DESC_TASK_ID_DEFAULT),
        "reason": _prop("string", (
            "Optional reason or machine-readable wake-up marker recorded on "
            "the completed run and scheduled event."
        )),
        "blocker_id": _prop("string", _BLOCKER_ID_DESC),
        "watch": _prop("boolean", (
            "True for a later check that needs nothing from David before it reports (e.g. verify X on "
            "Friday): the parked card stops holding its chat unfinished (the chat shows done with a watch mark "
            "naming this card; asked about the chat, name this pending check); a block, failure or done still "
            "notifies the chat. Sticky across re-parks; false clears it."
        )),
        **_CLEAR_BLOCKER_PROP,
        **_NEEDS_PROP,
        **_WAKE_CHECK_PROPS,
    },
    [],
)


KANBAN_MOVE_SCHEMA = _schema(
    "kanban_move",
    (
        "Move your current task to another machine and end this run. Use it when the work needs something "
        "this machine lacks (a Mac for Xcode/iOS/simulators, a GPU, a specific host's files or logs) or "
        "belongs on a less loaded machine. Never move a card to the hub for hub files, logs, databases, cron, "
        "config, services, `hermes kanban` or the Board admin key: a worker reads them with `ssh hermes-hub` and "
        "does every routine hub write with `hub-admin '<command>'` (declare hub:write); a machine:hub move is "
        "judged by hub admission and denied unless only the hub can do the work. It saves your checkpoint, ends this run as 'continued', sets the "
        "card's runs_on to the labels you give and re-queues it, so ONLY a machine carrying every label claims "
        "the next run (the Board enforces it). Refused when no other active machine matches, after "
        "kanban.move.max_moves moves, when moving back to a machine the card already left (pass force), and "
        "for dir/worktree cards. After it returns, end your turn with a one-line reply; do not call more tools. "
        "Exception: task_id = a worker-first card YOUR task created that no pool worker can run (its "
        "'No pool worker can run this card' notice) and runs_on = [\"machine:<origin>\"] repins that card to the "
        "origin instead; your own run continues. Second exception: task_id = a READY, unclaimed card of this "
        "gateway whose runs_on no enrolled machine carries (stranded: e.g. ['hermes-hub']) may be re-routed by any "
        "worker to labels a machine does carry (e.g. ['machine:hub']); the old labels are kept in a comment. "
        "Third: a pickup-stall alarm's run may move a ready card its alarm lists off the stalled machine to "
        "runs_on = [\"pool:worker\"] while another pool worker is eligible now (or [\"machine:<X>\"] for one other "
        "eligible machine outside the pool). "
        "Every other card is refused."
    ),
    {
        "task_id": _prop("string", _DESC_TASK_ID_DEFAULT),
        "runs_on": {"type": "array", "items": {"type": "string"}, "description": (
            "Labels the next machine must have, e.g. [\"pool:worker\"] (any worker with a free slot: the default "
            "for portable work), [\"macos\"], [\"linux\"]. machine:<name> pins one registered machine that picks "
            "up this board (else refused); pinning one pool worker also needs host_reason."
        )},
        "reason": _prop("string", "Why the card must move (what this machine lacks). Logged on the card."),
        "host_reason": _prop("string", (
            "Required for runs_on naming one pool worker (machine:worker-N): the card works on that host, or "
            "holds unpushed state there. Capacity is never a host reason: use [\"pool:worker\"]. Also required "
            "when reason claims ship/landing custody: hermes-core-wt ship lands from every machine."
        )),
        "checkpoint": _prop("string", (
            "Everything the next run needs to resume on the other machine: what is done (commits, branches, "
            "pushed work, with ids), what is NOT portable (local files, processes) and the exact next steps. "
            "Markdown. No secrets."
        )),
        "force": {"type": "boolean", "description": "Allow moving back to a machine this card already left."},
    },
    ["runs_on", "reason", "checkpoint"],
)


KANBAN_CONTINUE_SCHEMA = _schema(
    "kanban_continue",
    (
        "Checkpoint your current task and end this run so a FRESH run continues "
        "it. Use when the work will not finish inside this run's max runtime "
        "(you get a 'Kanban soft deadline' message near the cap), or when the "
        "next step is a long wait (CI, a release, a soak, a training "
        "checkpoint) that should not burn runtime in a sleep/poll loop. The "
        "run closes with outcome 'continued' and the card is re-queued at once "
        "(or after ``resume_after_seconds``) with your checkpoint at the top of "
        "the next run's context plus this run's transcript tail. A "
        "continuation is NOT a failure or a block: it never counts toward the "
        "retry give-up or unblock-loop limits. After it returns, end your turn "
        "with a one-line reply; do not call more tools."
    ),
    {
        "task_id": _prop("string", _DESC_TASK_ID_DEFAULT),
        "checkpoint": _prop("string", (
            "Everything the next run needs to resume without re-deriving it: "
            "what is done (commits/branches/files/deploys, with ids), current "
            "state, the exact next steps, background processes to re-adopt "
            "(PIDs, process_manage session ids, log paths, worktrees), what is "
            "being waited on and how to check it, and what must not be redone. "
            "Markdown. No secrets."
        )),
        "resume_after_seconds": {"type": "integer", "description": (
            "Optional. 0 (default) re-queues immediately. N > 0 parks the card in "
            "'scheduled' and the dispatcher re-queues it after N seconds (max "
            "86400): use this to hand off a wait instead of polling. Over 7200 "
            "needs wake_check (or clock_only for a pure time wait). The wait needs a "
            "producer (``awaiting``): agent-action conditions (a grant, a permission, "
            "an install, a fix) are work, not waits."
        )},
        **_WAKE_CHECK_PROPS,
        "metadata": {"type": "object", "description": (
            "Optional machine-readable state (e.g. {\"pids\": [...], \"ci_run\": ...})."
        )},
        "blocker_id": _prop("string", _BLOCKER_ID_DESC),
        **_CLEAR_BLOCKER_PROP,
    },
    ["checkpoint"],
)


KANBAN_REQUEST_REVIEW_SCHEMA = _schema(
    "kanban_request_review",
    (
        "Hand a finished, self-verified task to a human or reviewer before it is done. Moves the task to the 'review' "
        "column and notifies the subscriber. Unlike ``kanban_block`` this is "
        "NOT a blocker — it never counts toward unblock-loop detection, so a "
        "task can cycle through review across follow-ups without ever being "
        "falsely escalated to triage. Use this instead of blocking with a "
        "free-form 'review-required:' reason."
    ),
    {
        "task_id": _prop("string", _DESC_TASK_ID_DEFAULT),
        "summary": _prop("string", (
                "What was implemented and how it was verified, in one or "
                "two sentences — shown to the reviewer. Don't paste "
                "the whole diff; the reviewer has the board and the PR."
        )),
        "reviewer": _prop("string", (
                "Optional reviewer profile. When provided, the task is "
                "reassigned to that profile before review dispatch."
        )),
        "metadata": {
            "type": "object",
            "description": (
                "Optional structured handoff facts for the reviewer, such "
                "as changed_files, tests_run, commit, or decisions."
            ),
            "additionalProperties": True,
        },
        "artifacts": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Optional list of absolute paths to deliverable "
                "files this handoff names — generated charts, "
                "PDFs, spreadsheets, images, archives. Examples: "
                "['~/.hermes/cache/scratch/q3-revenue.png', '~/.hermes/cache/scratch/report.pdf']. "
                "A review handoff is the last implementer "
                "transition, so the kernel copies these into the "
                "task's durable attachments before the reviewer's "
                "completion cleans the scratch workspace up, and "
                "the gateway notifier uploads them as native "
                "attachments to the subscribed chat. A missing "
                "declared scratch artifact keeps the task in place "
                "so you can fix the path and retry."
            ),
        },
    },
    ["summary"],
)

KANBAN_REQUEST_CHANGES_SCHEMA = _schema(
    "kanban_request_changes",
    (
        "Reviewer verdict: send the task back to its implementer with required changes. This closes the review "
        "run, reapplies parent dependency gating, and requeues the task without "
        "using block-loop accounting. Only use from a task claimed from the "
        "review column; use kanban_block only for a genuine external blocker."
    ),
    {
        "task_id": _prop("string", _DESC_TASK_ID_DEFAULT),
        "reason": _prop("string", (
                "Specific, actionable changes the implementer must make "
                "before requesting another review."
        )),
    },
    ["reason"],
)

KANBAN_HEARTBEAT_SCHEMA = _schema(
    "kanban_heartbeat",
    (
        "Signal you're still alive during a long operation (training, encoding, crawls). Call every few minutes so "
        "humans see liveness separately from PID checks. Pure side "
        "effect — no work changes."
    ),
    {
        "task_id": _prop("string", _DESC_TASK_ID_DEFAULT),
        "note": _prop("string", (
                "Optional short note describing current progress. "
                "Shown in the event log."
        )),
    },
    [],
)

KANBAN_COMMENT_SCHEMA = _schema(
    "kanban_comment",
    (
        "Append a comment to a task's thread. Use for durable notes "
        "that should outlive this run (questions for the next worker, "
        "partial findings, rationale). Ephemeral reasoning doesn't "
        "belong here — use your normal response instead."
    ),
    {
        "task_id": _prop("string", (
                "Task id. Required (may be your own task or "
                "another's — comment threads are per-task). Outside a "
                "dispatcher-spawned worker there is no default; use "
                "kanban_list to discover ids."
        )),
        "body": _prop("string", "Markdown-supported comment body."),
        "relays_user_message": _prop("boolean", (
                "Set true when this comment passes on the user's reply from this chat to a card that asked "
                "(e.g. a needs_input block). Counts as a human answer for the card's no-progress budget; "
                "the comment stays attributed to the agent. Only an interactive chat may set it, once per "
                "user message per card, and only for a message sent after the card's latest ask.")),
    },
    ["task_id", "body"],
)

KANBAN_ATTACH_SCHEMA = _schema(
    "kanban_attach",
    (
        "Attach a file to a task by passing its bytes inline (base64). "
        "Use for genuine file artifacts the next worker or a human should "
        "be able to download — generated reports, images, exports. The "
        "file is stored as a real attachment (not a comment link) under "
        "the task's attachments dir, capped at 25 MB. Prefer "
        "kanban_attach_url when you only have a URL."
    ),
    {
        "task_id": _prop("string", _DESC_TASK_ID_DEFAULT),
        "filename": _prop("string", (
                "File name to store it under (e.g. 'report.pdf'). "
                "Directory components are stripped; only the leaf is kept."
        )),
        "content_base64": {
            "type": "string",
            "description": "The file contents, base64-encoded. Max 25 MB decoded.",
        },
        "content_type": _prop("string", "Optional MIME type (e.g. 'application/pdf')."),
    },
    ["filename", "content_base64"],
)

KANBAN_ATTACH_URL_SCHEMA = _schema(
    "kanban_attach_url",
    (
        "Attach a file to a task by URL (Hermes downloads it server-side, max 25 MB). Use when "
        "you have a link rather than the bytes. Only http/https URLs are "
        "accepted."
    ),
    {
        "task_id": _prop("string", _DESC_TASK_ID_DEFAULT),
        "url": _prop("string", "http(s) URL to fetch and store."),
        "filename": _prop("string", (
                "Optional name to store it under. Defaults to the URL "
                "path's leaf component."
        )),
        "content_type": _prop("string", (
                "Optional MIME type override. Defaults to the "
                "Content-Type the server returns."
        )),
    },
    ["url"],
)

KANBAN_ATTACHMENTS_SCHEMA = _schema(
    "kanban_attachments",
    (
        "List a task's attached files, with the absolute on-disk path you can read. "
        "Rows carry id, filename, content_type, size and uploader."
    ),
    {
        "task_id": _prop("string", _DESC_TASK_ID_DEFAULT),
    },
    [],
)

KANBAN_CREATE_SCHEMA = _schema(
    "kanban_create",
    (
        "Create a kanban task, optionally a child of the current one (via ``parents``). Used by "
        "orchestrator workers to fan out — decompose work into child "
        "tasks with specific assignees, link them into a pipeline, "
        "then complete your own task. The dispatcher picks up the new "
        "tasks on its next tick and spawns the assigned profiles. "
        "Count owners by board/project, not by your chat's links: a card "
        "that open cards on any board or gateway already cover is refused "
        "(code create_overlap, with their ids); continue that card, or pass "
        "distinct_from + distinct_reason when the work really is separate."
    ),
    {
        "title": _prop("string", "Short task title (required)."),
        "assignee": _prop("string", (
                "Profile name that should execute this task "
                "(e.g. 'researcher-a', 'reviewer', 'writer'). "
                "Omit to use the creating agent's profile. Requirements are checked before creation."
        )),
        "requires": {"type": "array", "items": {"type": "string",
                     "pattern": "^(terminal|file|git|host_repo|kanban|[a-z][a-z0-9-]*(:[A-Za-z0-9._@/+-]+)+)$"},
                     "description": "Required configured capabilities: terminal (shell), file (read/edit "
                     "files), git, host_repo (a checkout on the dispatcher host), kanban. Declare what the "
                     "work needs, or [] when it needs none: an omitted list with no dir/worktree/project "
                     "workspace or capability-bearing skill behind it may be refused "
                     "(requires_declaration_missing; nothing is created). "
                     "Worktree/dir workspaces and pinned skills' declared toolsets add their needs even to []; "
                     "git means terminal access, not an executable probe. "
                     "Declare remote reads explicitly: 'hub:read' for restricted read-only hub state, "
                     "'ssh:mbp' or 'ssh:mini' for the corresponding installed SSH access. Worker pools "
                     "may carry these only when every destination attests the matching capability; "
                     "prose mentions alone never exempt host markers. Never declare hub:read for writes, "
                     "restarts or administrative SQL, and never use Mac SSH declarations for other hosts. "
                     "Gateway capabilities the plan needs (namespaced, e.g. 'aws:767406518141:ec2-describe', "
                     "'ssh:mini', 'github:daveey:admin', 'sudo:nopasswd') are placement needs: if this "
                     "gateway lacks one and another holds it, an owner card is opened there first and "
                     "this card waits on it."},
        "body": _prop("string", (
                "Opening post: full spec, acceptance criteria, "
                "links. The assigned worker reads this as part of "
                "its context."
        )),
        "parents": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Parent task ids. The new task stays in 'todo' "
                "until every parent reaches 'done'; then it "
                "auto-promotes to 'ready'. Typical fan-in: list "
                "all the researcher task ids when creating a "
                "synthesizer task."
            ),
        },
        "tenant": _prop("string", (
                "Optional namespace for multi-project isolation. "
                "Defaults to HERMES_TENANT env if set."
        )),
        "priority": _prop("integer", (
                "Dispatcher tiebreaker. Higher = picked sooner "
                "when multiple ready tasks share an assignee."
        )),
        "admission_class": {
            "type": "string", "enum": ["normal", "capacity", "urgent"],
            "description": "Hub admission class; default normal. Capacity requires admission_frees.",
        },
        "admission_frees": _prop("string", "One-line frees: claim naming capacity this work restores."),
        "workspace_kind": {
            "type": "string",
            "enum": ["scratch", "dir", "worktree"],
            "description": (
                "Workspace flavor: 'scratch' (fresh tmp dir, "
                "default), 'dir' (shared directory, requires "
                "absolute workspace_path), 'worktree' (git worktree)."
            ),
        },
        "workspace_path": _prop("string", (
                "Absolute path for 'dir' or 'worktree' workspace. "
                "Relative paths are rejected at dispatch. A 'worktree' path must be a git repo "
                "root (the dispatcher creates <repo>/.worktrees/<task-id>), an existing linked "
                "worktree, or a new path whose parent is inside a repo; anything else is "
                "rejected at create time."
        )),
        "project": _prop("string", (
                "Optional project id or slug to link the task to. When "
                "set, the task becomes a git worktree under the project's "
                "primary repo with a deterministic branch (project slug + "
                "task id), instead of a random branch."
        )),
        "triage": _prop("boolean", (
                "If true, task lands in 'triage' instead of 'todo' "
                "— a specifier profile is expected to flesh out "
                "the body before work starts. Triage is not a hold: the "
                "auto-decomposer specifies and dispatches triage cards after "
                "~2 min (kanban.auto_decompose_min_age_seconds); to keep a "
                "card from running, park it with kanban_schedule right after "
                "creating it."
        )),
        "distinct_from": {"type": "array", "items": {"type": "string"}, "description": (
                "Ids of open cards a create_overlap refusal named that this card is NOT a duplicate of. "
                "Needs distinct_reason; recorded as a create_overlap event on the new card.")},
        "distinct_reason": _prop("string", "Why this card is separate work from the distinct_from cards."),
        "hub_reason": _prop("string", (
                "Only for a card that needs the hub itself: what only the hub has (its signed-in "
                "Chrome, a Desktop-session interaction, an "
                "existing hub worktree with unpushed commits, a hermes-hub command acting with the "
                "hub's key, or live gateway state hub-admin cannot reach). Board admin key and "
                "instance-role SSM steps are not hub needs: run them with hub-admin on a "
                "capability:hub-write worker. A Sonnet admission judge "
                "reads it with the card; without admission a hub card goes to the worker pool (pool "
                "workers have hub ssh + hub-admin) and an explicit hub pin is refused.")),
        "force_hub": _prop("boolean", (
                "Operator override: keep the card on the hub without the admission judge. Refused "
                "inside dispatcher workers; recorded as a forced hub_admission event.")),
        "idempotency_key": _prop("string", (
                "If a non-archived task with this key already "
                "exists, return that task's id instead of creating "
                "a duplicate. Useful for retry-safe automation."
        )),
        "origin_session": _prop("string", (
                "Chat on ANOTHER gateway this card is work for "
                "(@session:<profile>@<gateway>/<id>). Defaults to the "
                "session's $HERMES_ORIGIN_SESSION. The card (and its "
                "children) then shows in that chat's task list and keeps "
                "it unfinished; run origin_binding.verify_visible on the origin gateway "
                "to verify the card appears in that chat's task projection. "
                "verify_on_origin is only a replica-link diagnostic, not visibility proof."
        )),
        "runs_on": {"type": "array", "items": {"type": "string", "pattern": "^[a-z0-9][a-z0-9:._-]{0,63}$"},
                    "description": "Machine labels the card needs (all of them), on a board that syncs to the "
                    "Board service: e.g. ['linux'], ['macos', 'xcode'], ['machine:<name>'] for one machine, "
                    "[] for any machine. Omit for the default: it runs here, or on a machine like this one "
                    "if it waits too long. 'machine:<name>' is refused (placement_unavailable) unless that "
                    "machine picks up this board; then use placement='<name>'."},
        "placement": _prop("string", (
                "Where the card runs. 'auto' uses configured worker pools for portable work, queueing without hub overflow. "
                "Before pinning a helper to the hub, check the workers' attested capabilities: hub log/DB reads, "
                "remote Mac access, GitHub work and shipping may already run there. Pin only for a concrete "
                "hub-local write, unavailable runtime access or unpublished input; name that operation or path "
                "in the body. A hub name in evidence is not a locality requirement. Read access does not grant "
                "foreign-card or registry write authority. "
                "Without a pool, kanban.placement routes (e.g. MacBook -> hub; hermes-hub aliases hub) create "
                "portable cards on that gateway, linked to this chat. 'local' pins it here "
                "(use it when the work needs this machine: its Chrome, "
                "Keychain, Apple apps, launchd/Desktop state, local "
                "checkouts). A gateway name pins it there; if the card cannot be "
                "placed there (it is not live, a parent link, a "
                "non-scratch workspace, a non-default board, an origin-bound "
                "card, target not live, ...) the call fails with code "
                "placement_unavailable and creates nothing (never a silent "
                "local card). Your own gateway's name means local. On a board that syncs to the "
                "Board service, the name of an enrolled machine (e.g. a worker node) creates the card "
                "here with runs_on=['machine:<name>'], so only that machine runs it."
        )),
        "max_runtime_seconds": _prop("integer", (
                "Per-task runtime cap. When exceeded, the "
                "dispatcher SIGTERMs the worker and re-queues the "
                "task with outcome='timed_out'."
        )),
        "max_retries": _prop("integer", (
            "Retry circuit breaker for this card (1-10): the dispatcher blocks it on the Nth "
            "consecutive failure instead of respawning. Omit for the config/default breaker."
        )),
        "human_request": HUMAN_REQUEST,
        "initial_status": {
            "type": "string",
            "enum": ["running", "blocked"],
            "description": (
                "Initial card status. 'blocked' is only for human action needed now "
                "and requires human_request (action, why_human, interaction, scope, "
                "evidence): an ask written only in body is refused. Not for a future "
                "approval or 'after X, then Y' hold. For task dependencies, "
                "set parents and omit initial_status: the card waits in todo until parents "
                "finish. The worker must still obtain any required approval when due. "
                "For external conditions use kanban_schedule with wake_check (clock_only "
                "for pure time waits). With unfinished parents, 'blocked' requires explicit "
                "human_action_now=true. Defaults to 'running' (usual dispatch path)."
            ),
        },
        "human_action_now": _prop("boolean", (
            "Only for initial_status='blocked': explicitly confirm a human can act now "
            "even while parents remain unfinished. Describe that present action in body. "
            "Do not set for future/conditional approval. This keeps the card blocked; "
            "it does not grant approval or authorize execution."
        )),
        "skills": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Skill names to force-load into the dispatched "
                "worker. The kanban lifecycle is already injected "
                "automatically; use this to pin a task to a specialist "
                "context — e.g. ['translation'] for a translation "
                "task, ['github-code-review'] for a reviewer task. "
                "The names must match skills installed on the "
                "assignee's profile."
            ),
        },
        "review_required": _prop("boolean", "Require a reviewer before completion. Defaults to false."),
        "goal_mode": _prop("boolean", (
                "Run the dispatched worker in a goal loop. When true, "
                "after each turn an auxiliary judge checks the worker's "
                "response against this card's title/body; if the work "
                "isn't done and budget remains, the worker keeps going "
                "in the same session until the judge agrees it's "
                "complete (or the goal-turn budget is exhausted, which "
                "blocks the task for human review). Use this for "
                "open-ended cards where one shot rarely finishes the "
                "work. Defaults to false (classic single-shot worker)."
        )),
        "completion_contract": _prop("string", (
            "Declare at creation: local-only (default), OWNER/REPO for PR publication, or an exact GitHub PR URL. "
            "OPEN/MERGED PRs require repository-required exact-head CI and a landing disposition. "
            "On publication pass metadata.published_pr (also enforced on local-only cards). "
            "OPEN needs metadata.landing_owner bound to the same PR on an open same-board card, "
            "or metadata.pr_left_open={\"reason\": ...}; CLOSED is disposition, not delivery. "
            "A repo with no required checks can only complete with metadata.contract_waiver={\"reason\": ...} "
            "(prefer local-only there)."
        )),
        "goal_max_turns": _prop("integer", (
                "Turn budget for goal_mode workers. Caps how many "
                "continuation turns the worker may take before the task "
                "is blocked for review. Ignored unless goal_mode is "
                "true. Defaults to the goal-engine default (20)."
        )),
        "model": _prop("string", (
                "Pin the dispatched worker to this model instead of "
                "the assignee profile's configured model. Leave this out "
                "unless you need another tier; the name must be in this "
                "host's model routing registry when one is enforced. Omit "
                "to use the profile default."
        )),
        "provider": _prop("string", (
                "Provider the 'model' belongs to (e.g. 'openrouter', "
                "'anthropic', 'nous'). Set this whenever the model "
                "is not from the assignee profile's configured "
                "provider — a model name alone is resolved against "
                "the profile's provider and will fail if it belongs "
                "to a different one. Requires 'model'."
        )),
        "pin_mandatory": _prop("boolean", (
                "Mark the 'model' pin mandatory. Without it, when the "
                "pinned provider's subscription quota is out for 6 h or "
                "more, single runs use another provider's model of the "
                "same class until the reset (recorded on the card). Set "
                "true only when the work must run on exactly this model. "
                "Requires 'model'."
        )),
    },
    ["title"],
)

KANBAN_UNBLOCK_SCHEMA = _schema(
    "kanban_unblock",
    (
        "Unblock a Kanban task: blocked, scheduled, or a block-loop triage card. "
        "It moves to ready when all parents are done, "
        "or todo while any parent remains open. Orchestrator-only, except "
        "two grants: a self-heal fix card may unblock the single card named in "
        "its defect (`kanban card t_...`), and a card that finished fix card "
        "created may do the same; an owner-wake supervisor card (\"Owner wake: "
        "supervise ...\") may unblock the cards its body lists while they are "
        "in triage or blocked; a missing-parent repair card may unblock the child "
        "whose newest parent_missing block names it (on another gateway the call "
        "is forwarded to the child's home as an operator call). Any other worker "
        "call is refused, and the worker shell stays fenced."
    ),
    {
        "task_id": _prop("string", "Blocked task id to move to ready or parent-gated todo."),
    },
    ["task_id"],
)

KANBAN_LINK_SCHEMA = _schema(
    "kanban_link",
    (
        "Add a parent→child dependency edge after both tasks already "
        "exist. The child won't promote to 'ready' until all parents "
        "are 'done'. Cycles and self-links are rejected. A running child "
        "is rejected unless the active owning worker is linking its own "
        "card for a dependency handoff."
    ),
    {
        "parent_id": {"type": "string", "description": "Parent task id."},
        "child_id":  {"type": "string", "description": "Child task id."},
    },
    ["parent_id", "child_id"],
)

KANBAN_UNLINK_SCHEMA = _schema(
    "kanban_unlink",
    (
        "Remove a parent→child dependency edge. A dispatcher worker may "
        "only remove edges of its own card (its dependencies or its "
        "children), e.g. to undo a kanban_link it made, while its run is "
        "still live (before it parks); once its run has ended (after "
        "kanban_block/kanban_schedule/...) it is refused with run_already_ended. "
        "An orchestrator may unlink any "
        "edge. A child that no longer waits on anything is promoted at once."
    ),
    {
        "parent_id": {"type": "string", "description": "Parent task id."},
        "child_id":  {"type": "string", "description": "Child task id."},
    },
    ["parent_id", "child_id"],
)

KANBAN_ARCHIVE_SCHEMA = _schema(
    "kanban_archive",
    (
        "Archive a card. A dispatcher worker may archive an unclaimed or done card "
        "its own task created (creator_task_id is the caller's task): the "
        "cleanup when kanban_create kept a card local because of a local "
        "marker and the work does not need this machine (re-create it as the "
        "result's placement.remedy says), or of a scratch card (e.g. a live-proof waiter) that "
        "has finished. A done card is refused while an open card lists it as a "
        "parent. It may also archive a done or unclaimed capability "
        "owner card (idempotency_key capability-owner:<card>:...) when <card> "
        "is its own task or a card its task created. A claimed card, a card another task created, and "
        "the worker's own assigned task are refused. With no "
        "HERMES_KANBAN_TASK (an orchestrator or a human) any card may be "
        "archived."
    ),
    {
        "task_id": _prop("string", "Task id to archive. Required; does not default to the assigned task."),
    },
    ["task_id"],
)

KANBAN_SUPERVISOR_REQUEST_SCHEMA = _schema(
    "kanban_supervisor_request",
    (
        "Request unlinks between cards this task created. "
        "Asks the board's non-worker supervisor executor (the gateway dispatcher) to "
        "remove dependency edges between cards THIS task created, which a "
        "dispatcher worker may not do itself: kanban_unlink only removes this "
        "card's own edges (do those yourself; they are refused here). To re-order a graph of cards you "
        "created, add the new edges yourself with kanban_link and request the "
        "unlinks here; link ops are accepted only with dry_run, to preview. The request is durable and deduplicated; it stays "
        "pending until an executor accepts it, then the result (applied / "
        "failed / rejected, per op) lands on this card as a supervisor_request_* "
        "event and a comment, and a scheduled card is woken. It never completes or "
        "unblocks this card. Cards another task created are refused. "
        "Pass dry_run=true first to see, per op, whether you can do it yourself "
        "(self), need the supervisor (supervisor), it is already so (noop), or it "
        "is refused (cards another task created, cycles), and whether an executor is live. After a real request, park "
        "with kanban_schedule (or kanban_block kind='time') naming the request id; "
        "it wakes you on the result. Completing this card does not cancel a filed request (archiving it "
        "does); park only if you need the result. "
        "Op reconcile_notify {task_id, chat_id, thread_id?} instead asks the executor to retire a "
        "wrong-local platform='tui' send_refused notify subscription for a chat another gateway owns "
        "(a phantom 'unseen' count): the executor asks the owner gateway itself which notices it holds and "
        "removes the row only on its receipt, else keeps it. Pass no evidence; a request carries only "
        "reconcile_notify ops or only edge ops. "
        "Op resume {task_id} asks the executor to requeue the card that spawned this one (its creator "
        "chain) or a card this one created, ONLY when the continuation budget parked it "
        "(continuation_exhausted triage block) and nothing else: never a card waiting on a human "
        "(needs_input/capability/human_request), never more than twice per card. It runs the ordinary "
        "unblock, so parent gating applies (ready, or todo while a parent is open); the result reads back "
        "the target's status. A request carries only resume ops. "
        "Op retry_limit {task_id, max_retries 1-10} asks the executor to set the retry breaker on a card "
        "THIS task created (never this card itself); a request carries only retry_limit ops. "
        "Op archive {task_id} asks the executor to cancel (archive) an UNSTARTED card orphaned in this "
        "task's creator tree: created by a card this task created (up to 3 hops) whose creator(s) are all "
        "done or archived, so no live card owns it. Cards this task created directly are refused (use "
        "kanban_archive), as are claimed/running/done cards and anything outside the tree; a request "
        "carries only archive ops. "
        "Op operator {action, task_id, ...} is the scoped Board-operator call for ONE card ANOTHER gateway "
        "created (the ones kanban_comment/kanban_move/kanban_unblock refuse as foreign_card): action "
        "comment {text}, move {runs_on, reason} (not while it runs) or unblock {reason} (an agent's block "
        "only, never one waiting on a human). That card's home gateway executes it; complete/archive/link/"
        "unlink of a foreign card are refused (its owner's), one op per request, every call logged. "
        "Action label_publish {machine, labels, proof: {kind: 'hub_probe'}} (no task_id) asks the hub to ADD "
        "skill:/capability: labels to one machine's Board registry entry (never machine:*, never a removal): the "
        "hub re-runs its own probe of that machine and publishes only what it proves, as one atomic add on the Board; "
        "the answer lands on this card."
    ),
    {
        "ops": {
            "type": "array",
            "description": "Edge edits applied in order, reconcile_notify ops, resume ops, retry_limit ops, "
                           "archive ops (max 20), or exactly one operator op.",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["link", "unlink", "reconcile_notify", "resume", "retry_limit",
                                                         "archive", "operator"]},
                    "action": {"type": "string", "enum": ["comment", "move", "unblock", "label_publish"],
                               "description": "operator: what to do on the foreign card (label_publish: on a "
                                              "machine's registry labels)."},
                    "text": {"type": "string", "description": "operator comment: the comment body."},
                    "runs_on": {"type": "array", "items": {"type": "string"},
                                "description": "operator move: the new machine labels."},
                    "machine": {"type": "string", "description": "operator label_publish: the Board machine."},
                    "labels": {"type": "array", "items": {"type": "string"},
                               "description": "operator label_publish: skill:/capability: labels to add."},
                    "proof": {"type": "object", "description": "operator label_publish: {kind: 'hub_probe'}."},
                    "reason": {"type": "string", "description": "operator move/unblock: why (required)."},
                    "parent_id": {"type": "string"},
                    "child_id": {"type": "string"},
                    "task_id": {"type": "string", "description": "reconcile_notify: the subscribed task; "
                                                                 "resume: the card to requeue; "
                                                                 "retry_limit: the card to edit; "
                                                                 "archive: the card to cancel; "
                                                                 "operator: the one foreign card."},
                    "max_retries": {"type": "integer", "description": "retry_limit: the new breaker, 1-10."},
                    "chat_id": {"type": "string", "description": "reconcile_notify: the tui row's chat (session id)."},
                    "thread_id": {"type": "string", "description": "reconcile_notify: the row's thread, if any."},
                },
                "required": ["op"],
            },
        },
        "reason": _prop("string", "Why the graph must change (recorded on the request)."),
        "dry_run": _prop("boolean", "Only preflight: report per-op verdicts, file nothing."),
    },
    ["ops"],
)
