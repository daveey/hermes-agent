---
sidebar_position: 12
title: "Kanban (Multi-Agent Board)"
description: "Durable SQLite-backed task board for coordinating multiple Hermes profiles"
---

# Kanban — Multi-Agent Profile Collaboration

> **Want a walkthrough?** Read the [Kanban tutorial](./kanban-tutorial) — four user stories (solo dev, fleet farming, role pipeline with retry, circuit breaker) with dashboard screenshots of each. This page is the reference; the tutorial is the narrative.

Hermes Kanban is a durable task board, shared across all your Hermes profiles, that lets multiple named agents collaborate on work without fragile in-process subagent swarms. Every task is a row in `~/.hermes/kanban.db`; every handoff is a row anyone can read and write; every worker is a full OS process with its own identity.

### Completion checkpoints before the iteration cap

Dispatcher-owned workers get one checkpoint notice near 90% of their finite iteration
budget, attached to a fresh tool result while another tool-capable call remains. Use
`agent.budget_warning_ratio` to choose an earlier threshold. Tiny budgets warn no later
than their penultimate iteration; a one-iteration run has no pre-cap checkpoint window.
The notice is saved in the session transcript before the next request. Workers should
call `kanban_complete` only after verifying the task contract, or persist a progress
comment and continue. A commit or diff alone never automatically completes a task.

The hard cap, toolless final summary, and consecutive-failure circuit breaker are
unchanged: workers that still exhaust their budget remain subject to bounded retries.
This is a reporting opportunity, not a guarantee that a model will heed the notice.
Ordinary conversations and delegated children do not inherit the automatic Kanban
checkpoint; their iteration warning remains opt-in.

### Two surfaces: the model talks through tools, you talk through the CLI

The board has two front doors, both backed by the same `~/.hermes/kanban.db`:

- **Agents drive the board through a dedicated `kanban_*` toolset** — `kanban_show`, `kanban_list`, `kanban_complete`, `kanban_request_review`, `kanban_request_changes`, `kanban_block`, `kanban_heartbeat`, `kanban_comment`, `kanban_attach`, `kanban_attach_url`, `kanban_attachments`, `kanban_create`, `kanban_link`, `kanban_unblock`. The dispatcher spawns each worker with these tools already in its schema; orchestrator profiles can also enable the `kanban` toolset explicitly. The model reads and routes tasks by calling tools directly, *not* by shelling out to `hermes kanban`. See [How workers interact with the board](#how-workers-interact-with-the-board) below.
- **You (and scripts, and cron) drive the board through `hermes kanban …`** on the CLI, `/kanban …` as a slash command, or the dashboard. These are for humans and automation — the places without a tool-calling model behind them.

Both surfaces route through the same `kanban_db` layer, so reads see a consistent view and writes can't drift. The rest of this page shows CLI examples because they're easy to copy-paste, but every CLI verb has a tool-call equivalent the model uses.

This is the shape that covers the workloads `delegate_task` can't:

- **Research triage** — parallel researchers + analyst + writer, human-in-the-loop.
- **Scheduled ops** — recurring daily briefs that build a journal over weeks.
- **Digital twins** — persistent named assistants (`inbox-triage`, `ops-review`) that accumulate memory over time.
- **Engineering pipelines** — decompose → implement in parallel worktrees → review → iterate → PR.
- **Fleet work** — one specialist managing N subjects (50 social accounts, 12 monitored services).

The eight canonical collaboration patterns are catalogued in [Collaboration patterns](#collaboration-patterns) below.

## Duplicate-owner checks

`kanban.create_overlap_check` defaults to `true`. Creation checks open cards across
local boards and available gateway replicas for overlapping work, rather than only
cards linked to the current chat. A refusal returns `create_overlap` with the existing
card IDs; continue the existing owner, or pass `distinct_from` and `distinct_reason`
when the proposed work is genuinely separate. Set `kanban.create_overlap_check` to
`false` to disable this check. Missing or unreadable replicas are skipped and logged;
the check cannot guarantee uniqueness against unavailable boards.

Two creates that run at the same moment both pass the first check before either card
exists. So every entry (the `kanban_create` tool, `hermes kanban create`, and the
dashboard REST API) runs the match again inside the write transaction that inserts the
card. That second pass reads only open cards on the target board that were created
since the first check began. Of two concurrent owners of one piece of work on one
board, exactly one lands and the other gets the same `create_overlap` refusal. A
concurrent create on another board or another gateway is not re-checked under that
lock, so that race remains.

Every refusal is recorded with each match's `why`: as a `create_overlap` event on the
creator's card or the best local match, or, when every match is on another gateway, as a
line in `<kanban home>/logs/create_overlap.jsonl` (rotated to `.1` at 1 MiB).

## PR completion contracts

Declare PR work at creation with `--completion-contract OWNER/REPO` (or an exact
`https://github.com/OWNER/REPO/pull/123` URL for existing work). `kanban_create`
accepts the same `completion_contract`. Use `local-only` for intentionally local
work; existing and undeclared cards retain that default. Prose URLs are not policy.

After publishing, pass `metadata.published_pr` to completion, including for a card
initially marked `local-only`. The first matching URL binds the card; retries cannot
substitute a green sibling PR (a positively closed-unmerged binding can be replaced).
A review-only card must not pass `metadata.published_pr` for the PR it reviewed: cite it
in `metadata.pr` or `metadata.reviewed_pr`. A value that is not one exact PR URL string
is refused as `malformed_published_pr` without a GitHub read.
CLI `show --json` and `kanban_show` expose the persisted contract.

An **OPEN** PR cannot complete merely because it was published or CI passed. Merge
now with exact-head required CI green and every review comment (bots too) answered
(standing approval), or create a landing card on the same board, bind its
`completion_contract` to the exact PR URL, and pass `metadata.landing_owner=<card id>`.
The owner must be another open card; self, done, archived, missing, and different-PR
owners are refused. The owner is checked again under the completion write lock,
without network I/O. A pre-created review/release child alone is not a landing owner.

To deliberately leave the PR open instead, pass
`metadata.pr_left_open={"reason": "<nonblank reason>"}`. The completed event and done
notice retain that reason and the caveat: **the hub batch only installs merged main;
it never merges PRs**. A **MERGED** PR still requires exact-head CI acceptance.
A **CLOSED** PR is an accepted disposition, not a claim of delivery; its closure
does not require green CI. GitHub read errors remain fail-closed.

The shared `complete_task` boundary covers worker tools, CLI, review approval and
dashboard completion. It reads classic branch protection and active ruleset
required contexts, paginates exact-head check runs and legacy statuses, then
re-reads the PR head/base. Optional failed/skipped telemetry does not veto accepted
required checks. Missing, pending, failed, cancelled, timed-out, stale, skipped or
neutral **required** evidence cannot complete the card. Neither can zero-run
acceptance, unreadable policy or GitHub API failures. For OPEN/MERGED PRs with no
required checks, `metadata.contract_waiver={"reason": "<local verification>"}` waives
only that CI condition, never the landing gate. `gh` must be authenticated with read access to
the repository's checks and rules; no remote writes are performed by this gate.
Acceptance reads run as the **assignee profile's** `gh` login — its `GH_TOKEN` /
`GH_CONFIG_DIR` from the profile's own `.env`, never the ambient login of the
process completing the card. On multi-profile hosts (one GitHub identity per
org), sign `gh` in per profile (`GH_CONFIG_DIR` in that profile's `.env`). The
token must live in the profile's `.env` (or a configured secret source): a
`GH_TOKEN` merely exported in the shell or a systemd unit is scrubbed from the
`gh` child and never re-added. An assignee profile with no `gh` login of its
own — or one that no longer exists — is refused `not logged in` rather than
falling through to `~/.config/gh`; unassigned cards still use the ambient
login. A login that cannot see the repository is rejected with
`classification=auth`, naming the profile and repository, instead of a
retryable infra failure.

On short-lived credential-broker hosts, opt in separately in each assignee's
config with `kanban.pr_acceptance.gh_command_prefix`, an argv list prepended to
all gate `gh` commands. For example:

```yaml
kanban:
  pr_acceptance:
    gh_command_prefix:
      - /absolute/path/to/token-broker
      - exec
      - --scope
      - user/github
      - --reason
      - Kanban PR acceptance
      - --
```

The first element must be an installed absolute executable path. No shell is
used. The wrapper receives the isolated profile environment and must exec the
appended command, preserve its exit status, and emit only the command's JSON on
stdout. It must vend credentials into that child's environment, never print or
persist tokens. Use noninteractive/already-approved scopes: the whole read has
a 30-second timeout. Do not put credentials in this setting. The default is an
empty list (ordinary profile-isolated login); routed profiles do not inherit the
launch profile's wrapper. `gh` is resolved on the child PATH, then in `~/bin` and
`~/.local/bin`; a missing CLI or invalid wrapper setting is a `setup` rejection,
not a transient infrastructure failure.

The dispatcher's own GitHub reads use the same setting from the **dispatcher's
home** (the home whose dispatcher ticks the board), not the assignee's: the
PR-state read that releases cards held on an active PR, and the CI-run progress
sweep that wakes a card parked (`awaiting` kind `ci`) on a GitHub Actions run
that no runner is picking up. Configure the prefix there too when that home's
`gh` has no ambient login (a hub or service host), or those reads fail open:
merged-PR holds wait out their cap, and the CI-run check is disabled with an
hourly `no credential` WARNING per home and board (`gh not installed` when the
CLI itself is missing, which no prefix fixes).

Rejection retains the active card and workspace. Durable `pr_acceptance` events
store PR URL, SHA, required contexts, check IDs/URLs, classifications and recovery
instructions; `last_failure_error` surfaces the next step. Fix failures, rerun
infrastructure checks or wait, then retry completion. Use `kanban_block` when
human action is needed. Generic GitHub `failure` cannot establish whether a test
or artifact upload failed; inspect its retained URL. Explicit infrastructure
conclusions and API failures are classified separately. No extra worker is spawned.

Receipt persistence and the terminal write recheck run/status/contract ownership
under one SQLite lock: a reclaimed worker cannot complete or attach acceptance to
the new run. The final GitHub read is a completion-time snapshot, not a distributed
transaction or a continuous post-completion monitor. This is a single-user lifecycle
guard, not OS isolation against arbitrary direct database writes. GitHub Enterprise
is not covered. Related publication/lifecycle work: #91230, #84254, #52311; local
verification and publication alone are not remote acceptance.

## Kanban vs. `delegate_task`

They look similar; they are not the same primitive.

| | `delegate_task` | Kanban |
|---|---|---|
| Shape | RPC call (fork → join) | Durable message queue + state machine |
| Parent | Blocks until child returns | Fire-and-forget after `create` |
| Child identity | Anonymous subagent | Named profile with persistent memory |
| Resumability | None — failed = failed | Block → unblock → re-run; crash → reclaim |
| Human in the loop | Not supported | Comment / unblock at any point |
| Agents per task | One call = one subagent | N agents over task's life (retry, review, follow-up) |
| Audit trail | Lost on context compression | Durable rows in SQLite forever |
| Coordination | Hierarchical (caller → callee) | Peer — any profile reads/writes any task |

**One-sentence distinction:** `delegate_task` is a function call; Kanban is a work queue where every handoff is a row any profile (or human) can see and edit.

:::caution Don't link a support card to the card it is meant to unblock
A worker that is blocked on `t_parent` and creates a support card for the missing piece must **not** `kanban_link(t_parent, t_support)`: the link makes the support card a *child* of the blocked parent, so it is gated behind the parent it exists to unblock and neither card ever runs. Reference the parent id in the support card's body instead. `link`/`kanban_link` report `gated: true` and record a `dependency_wait` event when they demote a `ready` child (and `kanban_create` with `parents` does the same when it parks the new card), so the deadlock is visible on the board; `hermes kanban unlink <parent> <child>` releases it.
:::

**Use `delegate_task` when** the parent agent needs a short reasoning answer before continuing, no humans involved, result goes back into the parent's context.

**Use Kanban when** work crosses agent boundaries, needs to survive restarts, might need human input, might be picked up by a different role, or needs to be discoverable after the fact.

They also coexist inside one run. A kanban task executes in an isolated managed worker under the profile's gateway, and its `delegate_task` children are reserved by that gateway under the worker's execution: each child gets its own session (listed as a subagent of the card's session), counts against `delegation.max_concurrent_children` and `delegation.max_spawn_depth`, returns its result inside the card's turn, and ends with the worker. If the gateway goes down mid-child, the child is retired, never resumed. Work that must outlive the gateway, or needs its own reviewer, still belongs in child cards (`kanban_create` with `parents`).

## Core concepts

- **Board** — a standalone queue of tasks with its own SQLite DB, workspaces
  directory, and dispatcher loop. A single install can have many boards
  (e.g. one per project, repo, or domain); see [Boards (multi-project)](#boards-multi-project)
  below. Single-project users stay on the `default` board and never see the
  word "board" outside this docs section.
- **Task** — a row with title, optional body, one assignee (a profile name), status (`triage | todo | ready | running | blocked | review | done | archived`), optional tenant namespace, optional idempotency key (dedup for retried automation).
- **Link** — `task_links` row recording a parent → child dependency. The dispatcher promotes `todo → ready` when all parents are `done`. Adding a link to a running child is rejected because it cannot gate work already claimed; the one exception is an active worker linking its own card, with dispatcher-provided run ownership, immediately before a dependency-block handoff.
- **Comment** — the inter-agent protocol. Agents and humans append comments; when a worker is (re-)spawned it reads the full comment thread as part of its context.
- **Workspace** — the directory a worker operates in. Three kinds:
  - `scratch` (default) — fresh tmp dir under `~/.hermes/kanban/workspaces/<id>/` (or `~/.hermes/kanban/boards/<slug>/workspaces/<id>/` on non-default boards). **Deleted when the task completes** — scratch is ephemeral by design. Files explicitly declared through `kanban_complete(artifacts=[...])` or `kanban_request_review(artifacts=[...])` are copied into durable per-task attachment storage before cleanup (a review handoff stages them at handoff time, since the reviewer's later completion is what deletes the scratch workspace); existing deliverable paths in legacy completion summaries receive the same treatment. Other scratch files are removed. Completion records a machine-qualified `artifact_receipt` for declared artifacts and absolute file paths in completion evidence. Missing paths, including scratch paths, produce visible warnings rather than a file-delivery claim (the `kanban_complete` result repeats them as `artifact_warnings`); failures preserving existing scratch bytes still keep the task in-flight. Files up to 25 MB are copied into card attachments even when outside scratch and replicated to the home board, increasing storage on the executor and receiving replicas by the declared file sizes (declare only evidence you intend to retain). Larger files retain a machine-qualified path and SHA256; large scratch files receive a durable executor-local copy before cleanup. Native chat uploads use verified local bytes: replicated attachments on receivers, or digest-checked large files only on their executing machine. Only declared `artifacts` are uploaded to chat; evidence files keep custody (attachment, digest) without a chat upload, and evidence refs that name no file (`/api/route`, `/src/x.py:12`, `/repo commit abc`) get no receipt line. Files over 25 MB are hashed (and large scratch files copied) before the completion takes the board write lock, never under it. Use `worktree:` or `dir:<path>` when the whole workspace should remain available. The first time a scratch workspace is created on an install, the dispatcher logs a warning and emits a `tip_scratch_workspace` event on the task (visible via `hermes kanban show <id>`).
  - `dir:<path>` — an existing shared directory (Obsidian vault, mail ops dir, per-account folder). **Must be an absolute path.** Relative paths like `dir:../tenants/foo/` are rejected at dispatch because they'd resolve against whatever CWD the dispatcher happens to be in, which is ambiguous and a confused-deputy escape vector. The path is otherwise trusted — it's your box, your filesystem, the worker runs with your uid. This is the trusted-local-user threat model; kanban is single-host by design. **Preserved on completion.**
  - `worktree` — a git worktree under `.worktrees/<id>/` for coding tasks. Use `worktree:<path>` to pin the exact target path. Worker-side `git worktree add` creates it, using `--branch` when provided. **Preserved on completion.**
- **Dispatcher** — a long-lived loop that, every N seconds (default 60): reclaims stale claims, reclaims crashed workers (PID gone but TTL not yet expired), reaps workers that outlived their finished run (a worker still alive after its own `kanban_complete`/`kanban_block` is terminated once its run has been closed for two minutes, leaving it time to finish its final turn — matched by PID *and* spawn-time fingerprint, so a recycled PID is never signalled; recorded as a `terminal_worker_reaped` event), promotes ready tasks, atomically claims, spawns assigned profiles. Runs **inside the gateway** by default (`kanban.dispatch_in_gateway: true`). One dispatcher sweeps all boards per tick; workers are spawned with `HERMES_KANBAN_BOARD` pinned so they can't see other boards. After `kanban.failure_limit` consecutive spawn failures on the same task (default: 2) the dispatcher auto-blocks it with the last error as the reason — prevents thrashing on tasks whose profile doesn't exist, workspace can't mount, etc.
- **Tenant** — optional string namespace *within* a board. One specialist fleet can serve multiple businesses (`--tenant business-a`) with data isolation by workspace path and memory key prefix. Tenants are a soft filter; boards are the hard isolation boundary.

## Boards (multi-project)

Dedicated supervised runtimes can reserve a board with the optional native
`dispatch_owner` field. See [per-board dispatch ownership](../../developer-guide/kanban-dispatch-ownership.md)
for operator set/read/clear commands, the Python dispatcher ID contract, and
safe rollout. This is same-UID coordination, not an authentication boundary.

Boards let you separate unrelated streams of work — one per project, repo,
or domain — into isolated queues. A new install has exactly one board
called `default` (DB at `~/.hermes/kanban.db` for back-compat). Users who
only want one stream of work never need to know about boards; the feature
is opt-in.

Per-board isolation is absolute:

- Separate SQLite DB per board (`~/.hermes/kanban/boards/<slug>/kanban.db`).
- Separate `workspaces/` and `logs/` directories.
- Workers spawned for a task see **only** their board's tasks — the
  dispatcher sets `HERMES_KANBAN_BOARD` in the child env and every
  `kanban_*` tool the worker has access to reads it.
- Linking tasks across boards is not allowed (keeps the schema simple; if
  you really need cross-project refs, use free-text mentions and look
  them up by id manually).

### Managing boards from the CLI

```bash
# See what's on disk. Fresh installs show only "default".
hermes kanban boards list

# Create a new board.
hermes kanban boards create atm10-server \
    --name "ATM10 Server" \
    --description "Minecraft modded server ops" \
    --icon 🎮 \
    --switch                   # optional: make it the active board

# Operate on a specific board without switching.
hermes kanban --board atm10-server list
hermes kanban --board atm10-server create "Restart ATM server" --assignee ops

# Change which board is "current" for subsequent calls.
hermes kanban boards switch atm10-server
hermes kanban boards show             # who's active right now?

# Rename the display name (the slug is immutable — it's the directory name).
hermes kanban boards rename atm10-server "ATM10 (Prod)"

# Archive (default) — moves the board's dir to boards/_archived/<slug>-<ts>/.
# Recoverable by moving the dir back.
hermes kanban boards rm atm10-server

# Hard delete — `rm -rf` the board dir. No recovery.
hermes kanban boards rm atm10-server --delete
```

Board resolution order (highest precedence first):

1. Explicit `--board <slug>` on the CLI call.
2. `HERMES_KANBAN_BOARD` env var (set by the dispatcher when spawning a
   worker, so workers can't see other boards).
3. `~/.hermes/kanban/current` — the slug persisted by `hermes kanban
   boards switch`.
4. `default`.

Slugs are validated: lowercase alphanumerics + hyphens + underscores, 1-64
chars, must start with alphanumeric. Uppercase input is auto-downcased.
Anything else (slashes, spaces, dots, `..`) is rejected at the CLI layer
so path-traversal tricks can't name a board.

### Managing boards from the dashboard

`hermes dashboard` → Kanban tab shows a board switcher at the top as soon
as more than one board exists (or any board has tasks). Single-board users
see only a small `+ New board` button; the switcher is hidden until it
matters.

- **Board dropdown** — pick the active board. Your selection is saved to
  the browser's `localStorage` so it persists across reloads without
  shifting the CLI's `current` pointer out from under a terminal you left
  open.
- **+ New board** — opens a modal asking for slug, display name,
  description, icon, project directory, and (when any Project exists) a
  **Project** selector. Option to auto-switch to the new board.
- **Settings** — opens a modal for editing the current board's display
  name, description, and **project directory** (`default_workdir`). The
  project directory is the board-level workspace default every new task
  inherits (git repo → preserved worktree, plain dir → preserved
  directory); each task can still override it at creation time. Clearing
  the field reverts new tasks to disposable scratch workspaces — unless a
  Project is bound, in which case the Project's primary folder is used;
  unbind the project first to fall back to scratch.
- **Project** (in both modals) — binds the board to a Hermes Project
  (the board's `project_id`).
  Tasks created on a bound board inherit the Project; picking a project
  while the directory field is blank also seeds the project directory
  with the Project's primary folder. The bound project shows as a
  `Project: <name>` badge next to the board dropdown; its `×` unbinds the
  board (sends `project_id: ""`) without touching the project directory,
  and `No binding` in Settings does the same on save. The selector only
  appears when at least one Project exists.
- **Archive** — only shown on non-`default` boards. Confirms, then moves
  the board dir to `boards/_archived/`.

All dashboard API endpoints accept `?board=<slug>` for board scoping. The
events WebSocket is pinned to a board at connection time; switching in
the UI opens a fresh WS against the new board.

### Switching boards in the Desktop app

In the Desktop app the board switcher sits in the header row at the top of
the Kanban page, beside the page title: a **Board** control showing the current board's
name and task count, with a chevron — hover it for "Switch board". Click
it to pick another board, or to rename, configure, export, import,
create, or archive boards. When Kanban is open in a split tile, the same
**Board** control sits in the board's own header row, after the task count.
Like the dashboard, the desktop keeps its own
selection (persisted locally) and does not move the CLI's `current`
pointer.


## File attachments

Tasks can carry file attachments — PDFs, images, source documents — so a
worker has the source material it needs without you pasting paths into the
body and hoping it finds them.

- **Upload** — open a task in the dashboard drawer and use the
  **Attachments** section's *Upload file* button (multiple files at once
  are fine). Each upload is capped at 25 MB.
- **Storage** — files land under
  `<hermes-home>/kanban/attachments/<task_id>/` for the default board, or
  `<hermes-home>/kanban/boards/<slug>/attachments/<task_id>/` for a named
  board. Set `HERMES_KANBAN_ATTACHMENTS_ROOT` to pin a custom location.
- **What the worker sees** — when the dispatcher hands a task to a worker,
  the worker's context includes an **Attachments** section listing each
  file's name and its **absolute path**. The worker has full file/terminal
  tool access, so it reads attachments directly (`read_file`, or shell
  tools like `pdftotext`).
- **Download / remove** — the drawer lists each attachment with a download
  link and a remove (×) control. Removing an attachment deletes its
  metadata row; the on-disk file is deleted only when no other attachment
  row still references it (a file shared by several tasks stays until its
  last reference is removed). From the CLI, `hermes kanban attach-rm
  ATTACHMENT_ID` removes an attachment the same way.

:::note Remote terminal backends
Attachment paths resolve directly on the **local** terminal backend, which
is the default for Kanban workers. If you run workers on a remote backend
(Docker, Modal), mount the board's `attachments/` directory into the
sandbox so the absolute paths in the worker context are reachable.
:::


## Quick start

The commands below are **you** (the human) setting up the board and creating tasks. Once a task is assigned, the dispatcher spawns the assigned profile as a worker, and from there **the model drives the task through `kanban_*` tool calls, not CLI commands** — see [How workers interact with the board](#how-workers-interact-with-the-board).

```bash
# 1. Create the board (you)
hermes kanban init

# 2. Start the gateway (hosts the embedded dispatcher)
hermes gateway start

# 3. Create a task (you — or an orchestrator agent via kanban_create)
hermes kanban create "research AI funding landscape" --assignee researcher

# 4. Watch activity live (you)
hermes kanban watch

# 5. See the board (you)
hermes kanban list
hermes kanban stats
```

When the dispatcher picks up `t_abcd` and spawns the `researcher` profile, the very first thing that worker's model does is call `kanban_show()` to read its task. It doesn't run `hermes kanban show t_abcd`.

### Gateway-embedded dispatcher (default)

The dispatcher runs inside the gateway process. Nothing to install, no
separate service to manage — if the gateway is up, ready tasks get picked
up on the next tick (60s by default).

```yaml
# config.yaml
kanban:
  dispatch_in_gateway: true        # default
  dispatch_interval_seconds: 60    # default
  review_dispatch: true            # default: spawn the assigned profile with
                                   # the bundled sdlc-review skill. Set false
                                   # for human-only review boards.
  # dispatch_profiles: [sage]       # unset (key omitted): this home may claim
                                   # cards for any existing profile. Set to a
                                   # list (or comma-separated string) of profile
                                   # names to restrict which assignees this home
                                   # claims. Fail-closed: an empty list, `null`
                                   # or a bare `dispatch_profiles:` claims
                                   # nothing, and so does an unreadable config.
```

Override the config flag at runtime via `HERMES_KANBAN_DISPATCH_IN_GATEWAY=0`
for debugging. Standard gateway supervision applies: run `hermes gateway
start` directly, or wire the gateway up as a systemd user unit (see the
gateway docs). Without a running gateway, `ready` tasks stay where they are
until one comes up — `hermes kanban create` warns about this at creation
time.

#### Restart survival and retry context

On POSIX, workers start in separate process sessions with file-backed logs, so
terminating the gateway or Desktop backend process group does not signal them.
The next dispatcher observes the existing PID and run rather than starting a
replacement. On macOS, new claim ownership uses the boot-session UUID, not the
network-dependent hostname; hostname changes on wake do not make these claims
appear remote. Legacy hostname claims remain recognized under that hostname.

Workers register their persisted session against their exact board, task, run,
profile and live claim. After a crash, reclaim or iteration-budget exhaustion,
`kanban_show` includes a bounded historical transcript tail from the latest
registered closed run (including a compression summary when available). The new
run keeps its own ownership and must verify uncertain tool side effects before
retrying them; it does not replay tools or reopen the old session. Existing
iteration and failure limits still apply. Older runs without registration, deleted
transcripts and cross-profile handoffs do not receive transcript recovery.

A run requeued because a gateway restart cut off the run immediately before it
(`owner_restart_lost`, exit 76) instead continues that run's whole conversation.
The owner copies the lost run's persisted active transcript into the new run's own
fresh session before its first turn, and the prompt says the run resumed after a
restart in place of the transcript tail. The new run keeps its own run id, claim,
session and `owner_admitted` marker; the old session is only read. A trailing tool
call with no recorded result gets a synthetic "interrupted by gateway restart"
result rather than being replayed. The run falls back to the transcript tail when
the lost session is missing, empty or unreadable, belongs to another home or
profile, was rotated by compaction, or is already within 15% of the model's
compaction threshold. Each such run logs a
`restart_resume` event carrying `resumed_session_id` (also on `owner_admitted`) or
its `fallback` reason. Crash, timeout and other requeues are unchanged.

#### Runtime caps: checkpoint and continue

A card with `max_runtime_seconds` is still hard-killed at the cap (`timed_out`,
which counts toward the failure breaker). Before that, at
`kanban.soft_deadline_fraction` of the cap (default `0.85`; `0` disables it),
the running worker gets an out-of-band notice, and a final one shortly before the
kill. A worker that will not finish in time calls
`kanban_continue(checkpoint=...)`. The run closes with outcome `continued` and the
card goes straight back to `ready` (or `review` for a reviewer run). The next
run's context opens with that checkpoint, followed by the transcript tail
described above.

`kanban_continue(..., resume_after_seconds=N)` hands off a wait (CI, a release,
a soak) instead of polling inside the run: the card parks in `scheduled` and
the dispatcher re-queues it after `N` seconds (at most 24 h). A plain
`kanban_schedule` park wakes at its `SCHEDULED_UNTIL=` marker, else after 24 h.

Condition wakes: `kanban_schedule`, `kanban_block(kind="time")` and
`kanban_continue(resume_after_seconds=N)` accept `wake_check` (a shell command)
and `check_every` (seconds, default 900, min 120, max 21600). While the card is
parked the dispatcher runs the probe off-thread (`bash -lc`, 30 s timeout, cwd =
the task workspace, at most 4 at once) without spawning a worker. Exit 0 wakes
the card at once with a `condition_met` event carrying the probe's stdout;
exit 1 means not yet; any other exit (or a timeout) 3 times in a row wakes it
with `wake_check_broken` so a worker fixes the probe. The wake time stays the
deadline. A park that wakes more than 2 h out with no `wake_check` is refused
unless the call passes `clock_only=true` (a pure time wait). A wait on another
card on the same board is a parent link (`kanban_link`), not a park. CLI:
`hermes kanban schedule|block <id> --wake-check '<cmd>' [--check-every N] [--clock-only]`.
Probe state is in the `task_wake_checks` table and shown by `kanban show`,
the worker context and the board API; `kanban diagnostics` flags failing or
overdue probes, and parks more than 2 h out that have neither.

A clean exit-1 probe that never passes wakes its card to reconsider the wait after
`kanban.wake_check_stale_hours` (default 48; invalid/nonpositive values use 48).
The `wake_check_stale` event and comment carry the elapsed hours, check count and
episode start in Pacific time. Identical-probe re-parks retain the episode, with
no further stale wake until another full interval has elapsed. A changed/removed
probe, a passing probe (including the initial success before confirmation), or
leaving the wake/worker/re-park cycle for blocked, triage or a terminal status resets
it. Errors do not advance it and retain their existing handling. Machine-local
`task_wake_episodes` stores the start, count and last stale wake independently of
probe leases; `kanban show` and board probe state expose these episode fields.
Existing synced lifecycle events carry episode snapshots and per-store reset
sequences. A receiver joins reset knowledge and rejects snapshots that have not
observed its resets, so delayed starts cannot resurrect a finished episode and
replayed reset receipts cannot erase a newer one. Steady exit-1 checks only update
local state; the first check, resets, wakes and remote-alive events publish it.
Relaying execution to another gateway does not change the logical probe. The
store identity is local: cloning a live database for concurrent operation is not
a supported way to enroll another gateway.

Human asks that resolve outside Hermes: a blocked card's `human_request` may
carry `resolved_check` (a shell command) and `resolved_check_every` (seconds,
default 900, 120–21600). Use it when David's step leaves a readable trace, so
the card resumes when he does the step even if he never clicks unblock. A
device button:

```json
{"action": "Press the pair button on the watch. Unblock = finish pairing; archive = drop the watch.",
 "why_human": "physical button on David's device", "interaction": "physical",
 "scope": "watch pairing", "evidence": "hub lists the watch as unpaired",
 "resolved_check": "curl -sf http://hub.local:8080/api/devices/watch | jq -e '.paired == true'"}
```

A Settings toggle works the same way, e.g. "turn on Tap to click" with
`"resolved_check": "defaults read com.apple.AppleMultitouchTrackpad Clicking 2>/dev/null | grep -qx 1"`.
Exit codes are read literally: 0 = the step is done, 1 = not yet, anything
else (exit 2 or higher, a 30 s timeout, a probe that cannot start) = broken; the
output is only quoted, never interpreted. The probe runs once, synchronously,
when the ask is filed (`kanban_block`, `kanban_create` with
`initial_status="blocked"`, `hermes kanban create|block --human-request`,
`hermes kanban block --metadata-only --human-request`, the dashboard's
blocked/metadata-only PATCH and bulk updates), in the cwd and profile env the
dispatcher will use for that card; a bulk block runs it for every card before
any is blocked. A create runs it before the card exists: same board pin,
assignee env and workspace cwd, but no `HERMES_KANBAN_TASK` (file a probe that
needs its own card id with `kanban block` after the create), and a create whose
`idempotency_key` returns an existing card runs nothing. Exit 1 files it; exit 0 refuses the
ask with `ask_already_resolved` (the asked-for state already holds: don't ask,
continue the work); anything else refuses it with `resolved_check_broken` and
the output tail. A probe containing a secret or credential-shaped value is
refused (`resolved_check_secret`): asks are stored redacted, so the stored
command would differ from the one that ran. While the card stays `blocked` the
dispatcher runs it with the same runner, concurrency cap and failure count as a
park's `wake_check` (state row in `task_wake_checks` with
`purpose='ask_resolved'`). One exit 0 appends an `ask_resolved` event, comments
that the ask was satisfied outside Hermes, and unblocks the card the normal way
(`ready`, or `todo` behind an open parent). Three failures in a row append
`resolved_check_broken`, comment once and stop probing; the card stays blocked,
is never unblocked by a broken probe, and the probe is not restored until the
ask is re-filed. A plain `wake_check` on a human block is still refused: the
ask's probe goes in `human_request.resolved_check`.

On a synced board the probe runs on the gateway that filed the ask (its filing
event names it as `probe_host`). A worker node keeps its blocked copy while it
runs that probe; a board that loses the probe of an ask it filed re-registers it
once (`resolved_check_restored`, due at once), and a second loss comments once
(`resolved_check_lost`) and stops probing. Another gateway's ask probe is never
started here. The probing worker node notes `resolved_check_alive` at most every
3 h; when the home board has heard nothing from it for that long plus two
intervals and 15 min, it comments once (`resolved_check_host_silent`) and leaves
the card blocked on David's ask.

For an external GitHub PR wait, use the installed runtime's read-only probe:

```text
hermes --run-module hermes_cli.kanban_pr_wake https://github.com/OWNER/REPO/pull/NUMBER
```

Set this as `wake_check` only when the task is waiting for that PR to reach a
terminal state. The probe uses the current task profile's GitHub transport and
`kanban.pr_acceptance.gh_command_prefix` configuration. OPEN returns 1;
MERGED returns 0 to resume integration/verification; CLOSED without merge also
returns 0, but explicitly requests **reconciliation, not acceptance**. A closed
PR may be abandoned or superseded: verify the replacement's containing-main
commit and exact successful CI evidence before changing dependencies or claiming
delivery. All existing completion/CI/consent gates remain in force. API, auth or
malformed-response failures return 2 (or the outer probe timeout), never pending.

For a known replacement that may land while the original PR remains OPEN, the
owning worker can explicitly supply a pair of evidence anchors:

```text
hermes --run-module hermes_cli.kanban_pr_wake https://github.com/OWNER/REPO/pull/NUMBER --superseding-commit FULL_SHA --verification-job ACTIONS_JOB_ID
```

The helper requires the named job in that repository to be completed/successful
for exactly FULL_SHA and a GitHub comparison proving that SHA is contained in the
repository's default branch. Only then does an OPEN PR wake for reconciliation.
A failed/skipped/unfinished job, wrong SHA or noncontaining default branch stays
pending; incomplete/unreadable evidence is an error. The caller owns identifying
the replacement: the helper does not infer semantic equivalence from commits or
comments, and one successful job does not satisfy all required CI. Reconciliation
must verify actual replacement behavior, owner dependencies and release gates.

Do not use `test "$(gh ... --jq .state)" = MERGED`: it treats API failures as
pending and waits forever when a PR is intentionally superseded. Existing stored
shell probes are not rewritten automatically; the owning worker/supervisor must
adopt the terminal-state probe on the next explicit park. This is not a CI-ready
probe: an OPEN PR still waits even if checks finish. Use a separate exact-head
CI probe when the next action is review/landing rather than an external merge.


Continuations leave `consecutive_failures` and the unblock-loop counter
untouched, so they never trip the give-up or block-loop limits. Their own
bound is `kanban.max_continuations` (default 12) consecutive `continued` runs.
Past that, the worker has to complete or block. The refusal records a run-scoped
`continuation_exhausted` event with the checkpoint and recommends
`kanban_block(kind='transient', reason='continuation_exhausted: split the work or reset the continuation budget')`.
That budget-management block enters agent `triage`, not the human-input list.
A budget-only `needs_input` following the same run's refusal is corrected to
`transient`, with `requested_kind` and `rekind_reason=continuation_exhausted`
in the block event. Actual user questions, capability requests, and time waits
retain their own routing; unknown prose is not suppressed. This is a supervisor
handoff, not an automatic budget reset: previously run triage cards are excluded
from automatic decomposition, so the supervising agent must split or resume the
preserved work. Local session counts and remote replica counts treat this as
unfinished agent work, never as waiting on a human.

#### No-progress budget

A card can also loop without ever failing: it parks, its clock wakes it, the
worker finds nothing changed, comments and parks again. Before claiming a
`ready` card the dispatcher counts its trailing runs that ended in a wait
(`scheduled`, `blocked`, or `continued` into `scheduled`) since the card's last
progress marker: a completion or review hand-off, a link, attachment, edit,
reassignment, move, contract change, met wake condition, human comment, or a
completion, archive, review, reopen or edit on a parent or child card. Agent
comments are not progress, nor is a bare `unblocked` (a recovery loop is made
of them). Crashes, reclaims and quota releases neither count nor reset; a declared time window (`clock_only`, `awaiting.kind=clock`) is not
counted; a park with no wake condition (a clock poll) counts a quarter run.

At `kanban.no_progress_runs` (default 5; 0 disables) the dispatcher spawns
nothing and, deciding again under the write lock and never under another
dispatcher's live claim, instead (event `no_progress_parked` / `no_progress_escalated`,
`dispatch --json` key `no_progress_held`):

- re-parks the card behind the latest wake condition it declared since its
  last progress, with a 24 h clock fallback, so only the condition or real
  progress brings the next run (a fallback that finds nothing new re-parks it
  again: one event a day, no worker run);
- or, when there is no usable condition (none declared since the last
  progress, or that probe reported `wake_check_broken`), files one idempotent
  owner card (`created_by=kanban-no-progress`) the card waits on in `todo`.
  A looping owner card goes to `triage`. No `continuation_exhausted` or
  `goal_unachievable` event is written, so no recovery card is opened.

#### Spend budget

The no-progress budget and the continuation cap count *consecutive* idle
outcomes, so a card whose runs alternate (continue, block, schedule, reclaim)
or change something small every time is never stopped by them. The spend
budget bounds the total instead. Before claiming a `ready` or `review` card the
dispatcher counts its *spending runs* (closed runs that lasted at least 60 s;
quota walls, released claims and spawn failures end in seconds and are not
counted) since the card last handed work on (a run that ended `completed`,
`review_requested` or `changes_requested`), since a human last answered on it,
or since it was last sent to triage by this budget, and sums the tokens those
runs reported. A human answer is a comment typed by a person (dashboard, CLI,
`/kanban`) or a chat's verified relay of the user's reply
(`kanban_comment(relays_user_message=true)`), the same rule the no-progress
budget uses: answering a `needs_input` card that spent its budget while
blocked gets it a run instead of sending it straight to triage. Agent comments
and a bare `unblock` do not open a new window. Each managed worker books its
agent's session tokens (`usage`: input, output, cache-read, cache-write, estimated
cost) on the run's `worker_result` event, on whichever host ran it, so the
card's home board sees the whole spend.

At `kanban.spend_budget_runs` spending runs (default 15) or
`kanban.spend_budget_cache_read_tokens` cache-read tokens (default 30000000),
0 disabling either, the dispatcher spawns nothing and, deciding again under
the write lock and never under another dispatcher's live claim, moves the card
to `triage` with a `spend_budget_triaged` event and a comment showing the spend
(`dispatch --json` key `no_progress_held`, action `spend_budget:triage`). The
auto-decomposer skips cards that already ran, and the stale-triage sweep cannot
release a spend hold. Only a recorded manual promote (after splitting or
re-scoping, if needed) or archive releases it; a promote starts a new budget
window. Changing status or disabling the budget does not clear an existing hold.

An over-budget card that **published** an OPEN PR on
`softmax-universe/daveey-hermes` targeting `main` is spared from a new spend
triage. Publication is evidenced by its own run's `published_pr` metadata or
an owning-run canonical-ship publication comment, not a review or a bare PR
mention. State/base use the dispatcher's existing cached GitHub read (up to
five minutes old); unknown state or base retains normal enforcement. A
`spend_budget_spared` event and comment record the PR, runs and tokens once
per spend window. Counts keep accumulating: after merge/close, the next check
with refreshed state enforces the normal cap. This does not spare `state` PRs,
release an existing triage, or change the independent no-progress budget.

Each stop records its producer in `spend_budget_decision_ready.producer`: owning chats
receive one Kanban notice with spend and decision options, or a card with no
chat gets one board-supervisor decision card. The supervisor uses
`kanban.orchestrator_profile`, falling back to `default`. No Inbox post is made.

When the stopped card lives on another gateway, that decision card's worker
releases it with one operator call, never an unblock:
`kanban_supervisor_request` op `{"op": "operator", "action": "spend_promote",
"task_id", "runs" (1-20), "episode" (uid of the current spend_budget_triaged
event), "decision_comment_id" (its earlier operator comment recording the
decision), "reason"}`. Only the card the stop's producer names may make it, and
only after its decision comment (a human comment after it needs a new decision
comment); the card's home rechecks the stop, producer, comment, human holds and
parents, then
records `spend_budget_set` (exactly `runs` runs, any cache-read override kept)
and `promoted_manual` (with `spend_budget`) in one transaction. That promotion
opens the new window, so the card gets exactly `runs` more runs.

A card can carry its own limits, so a standing card meant to run indefinitely
(an upkeep shepherd) is not sent to triage every 15 runs:

```bash
hermes kanban spend-budget <id>                    # effective limits, override, current spend
hermes kanban spend-budget <id> --exempt --reason "standing shepherd"
hermes kanban spend-budget <id> --runs 40 --reason "long migration"
hermes kanban spend-budget <id> --clear --reason "back to normal"
```

`--runs` / `--cache-read-tokens` take 0 for no limit; a field not given keeps
the board's value; `--exempt` sets both to 0. Every change needs `--reason` and
is recorded as a `spend_budget_set` event (who, why, the limits) plus a card
comment; the newest one wins. A card over its own limit is triaged with
`override: true` on the `spend_budget_triaged` event. Exemptions are explicit
per card, never inferred from a title, and worker and `delegate_task` shells
are fenced from the verb like every other board write, so a card cannot
exempt itself.

#### Workers and systemd cgroups

Workers are fire-and-forget processes that outlive the dispatcher tick, so
wherever the dispatcher runs inside a systemd unit the worker is launched in
its own transient scope (`systemd-run --user --scope --unit hermes-worker-kanban-<task>-run-<run>`)
and survives that unit's exit or restart. Creating the scope needs the
dispatching user's session bus (`/run/user/<uid>/bus`); on a system-level
install give the user one with `sudo loginctl enable-linger <user>`.

- **Gateway dispatcher** (`dispatch_in_gateway: true`, gateway under systemd):
  the scope is mandatory. If the bus is unreachable the spawn is refused and
  recorded on the card as an *infrastructure* failure — `spawn_failed` with
  `metadata.infrastructure: true`, the linger remedy in `last_failure_error`,
  a warning in the gateway log — and the card stays `ready`. It does **not**
  advance `consecutive_failures`, so a host blip never parks cards as a bare
  `blocked`; the respawn guard spaces the retries (`infrastructure_cooldown`,
  same window as the rate-limit cooldown) until the bus is back.
- **`hermes kanban dispatch` from your own unit** (a `Type=oneshot` timer, a
  sequencer service): the worker gets the same scope when the bus is
  reachable. Without one the worker is still spawned — inside *your* unit's
  cgroup — and the dispatcher logs once, loudly, that the unit's exit will kill
  it. `Type=oneshot` with the default `KillMode=control-group` loses every
  worker within a second of the pass finishing; either enable linger so the
  scope can be created, or set `KillMode=process` on that unit so its exit
  only kills the dispatcher itself.

Running `hermes kanban daemon` as a separate process is **deprecated**;
use the gateway. If you truly cannot run the gateway (headless host
policy forbids long-lived services, etc.) a `--force` escape hatch keeps
the old standalone daemon alive for one release cycle, but running both
a gateway-embedded dispatcher AND a standalone daemon against the same
`kanban.db` causes claim races and is not supported.

### Shared boards across homes

Mounting one `kanban.db` in several Hermes homes (containers, fleet hosts) shares
the board, but profile names are home-local and every home has a root profile
named `default` — so `default` collides by construction. The dispatcher's spawn
gate checks `profile_exists(assignee)` against the *claiming* home, and without
further configuration every home's dispatcher considers a card assigned to
`default` claimable, so the wrong home can claim and run it. Either give each
home unique profile names and never assign cards to `default` on a shared board,
or set `kanban.dispatch_profiles` per home to declare exactly which assignees
that home may claim — anything else lands in the dispatcher's
`skipped_nonspawnable` bucket instead of spawning, and no longer counts as
spawnable work for the gateway's wake-up probe.

### Portable worktree sources (`kanban.workspace_source`)

A synced board can move a branch-backed card between machines: the Board keeps a
manifest (`repository`, `branch`, exact `commit`) and custody (`source_holder`,
`state`), and a machine that picks the card up materializes that exact branch
and commit in a dedicated registered clone. Off by default; every machine that
enables it needs a core with the full protocol and a Board that knows the source
actions (an older Board is detected and the host falls back to legacy behaviour).

```yaml
kanban:
  workspace_source:
    enabled: false            # opt in per machine
    clones:                   # GitHub "owner/name" -> absolute path of a dedicated clone
      acme/widget: /home/me/code/_src/widget
    publish_on_park: true     # push the task branch when a run parks or ends
    push_timeout_s: 30
    preflight_timeout_s: null    # null = push_timeout_s + 30
    materialize_timeout_s: null  # null = claim ttl / 3 - 30 s
```

**Lifecycle.** The dispatcher's claim asks the Board for custody cards, resolves
the source, then attaches the run to custody *before* the worker starts
(`HERMES_KANBAN_SOURCE_COMMIT/PATH/BRANCH` are set in its environment and a
`workspace_source_materialized` event records where it ran). A legacy worktree
card is adopted by its creator on its first enabled run. If the worker never
starts, the run is released with an `unchanged` receipt so custody goes back
exactly as it was.

**Publishing.** Every voluntary ending (complete, block, schedule, review
handoff, request changes, continue/move) first publishes the task branch: it
must be the card's own branch (`wt/<task>` or `<slug>/<task>`, never a protected
or shared one), clean, free of private ignored files, LFS and submodules, and
every object the push would send passes a secret scan: the entire raw bytes of
each new object (blobs as UTF-8, UTF-16/32 or binary; whole commits including
every header, signature and the message; trees) and each new path name,
including denylisted names such as `.env*` or `*.pem`. The scan and the push run
in one isolated git environment (no system or global config, replace refs,
grafts, attributes or hooks), and the push sends the exact scanned commit as a
fast-forward with
`--force-with-lease`, after a lease renewal; the Board then records the new
manifest. An unchanged branch publishes nothing. A refusal or timeout never stops
the ending: custody simply stays `retained` on this machine and the event
`workspace_source_publish_refused` names why. Crashes and timeouts publish
nothing, so the source stays with the machine that has it.

**Moving.** `dir` cards never move. A worktree card moves only with portable
custody (`published`/`unchanged` at the manifest's revision); moving a running
card publishes first and checks that result. `hermes kanban move --reclaim`
refuses source cards (a reclaim cannot publish), and `hermes kanban claim`
refuses cards under custody (`source_cli_claim_refused`). A portable worktree
card does not need `host_repo` on its target.

**Freeing a stuck source.** When the holder is gone, an operator on the hub runs
`hermes kanban source-release <task> --to published` (Board admin key). It first
checks that the branch on GitHub equals the manifest commit, and the Board
refuses while a lease is live.

A clone is proven before it is used or attested to the Board: its root, a
GitHub `origin` with no `pushurl` or `url.*` rewrite, a reachable remote, and a
GitHub repository id (cached 6 h per home) that matches the manifest. A host
that cannot materialize a card (wrong or dirty clone, occupied path, timeout)
releases it without a failure and skips that manifest revision; a source that
moved or vanished blocks the card for a person. On such cards
`workspace_kind` and `branch_name` cannot be changed (`source_fenced`).

#### Scratch cards that edit a repository (source protocol 2)

A scratch card's working directory is throwaway, so a repository it edits must
live in a **linked worktree of a registered clone**, registered with the card.
An ordinary `git clone` inside the scratch directory, or a worktree of any other
checkout, is never adopted: it stays on that machine and is lost if the card
moves. As the worker:

1. Find the registered clone for the repository
   (`hermes config get kanban.workspace_source.clones`). If none is listed on
   this machine, do not improvise one; ask for it to be configured.
2. Create the worktree from that clone, checked out on the card's branch: its
   assigned `branch_name`, or `wt/<task id>` when none is assigned (any other
   branch is refused):
   `git -C <clone> worktree add -b wt/<task id> <absolute path> origin/<base>`
   (drop `-b` if the branch already exists). The clone must pass the same proof
   as above (GitHub `origin`, no rewrites), and a path another active card uses
   is refused.
3. Register it from the live run with `kanban_heartbeat(source_path=<path>)`.
   Only an implementation run may register a first source (a review run may only
   resume one), and a replay with the same path is harmless.
4. Optionally carry up to 32 files that are not in Git with
   `kanban_heartbeat(artifacts=[...])` (or `artifacts` on the ending call). Each
   call replaces the selection with byte snapshots taken at that moment; `[]`
   clears it. Symlinks, special files and secret-looking names or bytes are
   refused.

Endings then publish the branch exactly as for worktree cards, and the selected
artifacts are verified by digest when another machine picks the card up. Until
a source is registered, the card has no custody and nothing is carried.

### Idempotent create (for automation / webhooks)

```bash
# First call creates the task. Any subsequent call with the same key
# returns the existing task id instead of duplicating.
hermes kanban create "nightly ops review" \
    --assignee ops \
    --idempotency-key "nightly-ops-$(date -u +%Y-%m-%d)" \
    --json
```

### Bulk CLI verbs

All the lifecycle verbs accept multiple ids so you can clean up a batch
in one command:

```bash
hermes kanban complete t_abc t_def t_hij --result "batch wrap"
hermes kanban archive  t_abc t_def t_hij
hermes kanban unblock  t_abc t_def
hermes kanban block    t_abc "need input" --ids t_def t_hij
```

:::note Where an unblocked task lands
`unblock` restores the safe source phase: **`review`** for reviewer-origin work
whose parents are complete, **`ready`** for implementation work whose parents
are complete, or **`todo`** while any parent remains open. A `todo` task keeps
its source-phase provenance and returns to `review` or `ready` automatically
when the dependency gate clears. `unblock` never routes directly to `triage`.

If you unblock a task and it later shows up in **`triage`**, the unblock is not
what put it there. A subsequent *re-block for the same reason* did: after a task
is blocked → unblocked → re-blocked for the same cause `BLOCK_RECURRENCE_LIMIT`
times (default `2`), the unblock-loop breaker stops sending it back to `blocked`
— where a cron would just keep unblocking it — and routes it to `triage` for
orchestration attention. This is a deterministic DB guard, not an LLM judgment call, and
a task's body text cannot opt out of it: the recurrence counter deliberately
survives each unblock (it resets only on a successful `complete`). To keep an
unblocked task in the work pool, resolve *why it keeps re-blocking* (unfinished
parent, missing input, unmet capability) before unblocking, or raise
`BLOCK_RECURRENCE_LIMIT` if the loop is expected.

A `needs_input` re-block whose reason text changed since the previous block
(the human answered one question, the worker asks the next) counts as
progress and restarts the counter at 1. A card escalated to `triage` this way,
or any triage card that has already run, is never picked up by the
specifier/decomposer sweeps (rewriting it from its body would redo shipped
work), even after someone comments on it. Return it to work with
`hermes kanban unblock <id>` / `kanban_unblock`, or `hermes kanban promote <id>`;
`hermes kanban decompose <id> --force` overrides the refusal explicitly.
:::

## Enabling tools for a chat profile

The Desktop Kanban plugin displays the board; it does not grant the chat agent
permission to manage tasks. Enable the `kanban` toolset for the profile and
platform that should orchestrate work:

```bash
hermes -p planner tools enable kanban                      # CLI / TUI / Desktop chats
hermes -p planner tools enable kanban --platform telegram  # a gateway platform
```

Each platform has its own selection under `platform_toolsets.<platform>` in
`config.yaml`; the toolset is also a checkbox in `hermes tools` and the dashboard.
A gateway agent that has it can `kanban_create` from a chat and is auto-subscribed
to that task's completion/block notifications in the same thread. Start a new chat
after changing this setting; existing conversations retain their tool schemas and
prompt cache. `agent.disabled_toolsets` remains authoritative. Legacy top-level
`toolsets: [kanban]` is honoured as a fallback only when no platform selection was
saved; `all` alone is not a Kanban opt-in.

Dispatcher-owned workers receive their task lifecycle tools automatically.
`delegate_task` children do not gain permission to mutate the board.

## How workers interact with the board

**Workers do not shell out to `hermes kanban`.** When the dispatcher spawns a worker it sets `HERMES_KANBAN_TASK=t_abcd` in the child's env, and that env var flips on a dedicated **kanban toolset** in the model's schema. The same toolset is also available to orchestrator profiles that enable `kanban` in their toolsets config. These tools read and mutate the board directly via the Python `kanban_db` layer, same as the CLI does. A running worker calls these like any other tool; it never sees or needs the `hermes kanban` CLI.

| Tool | Purpose | Required params |
|---|---|---|
| `kanban_show` | Read the current task (title, body, prior attempts, parent handoffs, comments, full pre-formatted `worker_context`). Defaults to the env's task id. | — |
| `kanban_list` | List task summaries with filters for `assignee`, `status`, `tenant`, archived visibility, and limit. Intended for orchestrators discovering board work. | — |
| `kanban_complete` | Finish with `summary` + `metadata` structured handoff. | at least one of `summary` / `result` |
| `kanban_request_review` | Start same-card review with a durable `summary`, optional `metadata`, and optional reviewer profile. The task moves to `review`; this is not a block. | `summary` |
| `kanban_request_changes` | Reviewer verdict from an active review run. Closes that run, reapplies parent gating, and routes the task to its original implementer without block-loop accounting. | `reason` |
| `kanban_block` | Stop work and route by why: `kind=dependency` (waits in `todo`, auto-resumes when an incomplete parent finishes; with no open parent it is recorded as `needs_input` instead, since the wait could never be satisfied), `needs_input`/`capability`/`transient` (surface to a human). Repeated same-kind re-blocks auto-escalate to `triage`. | `reason` |
| `kanban_heartbeat` | Signal liveness during long operations. Pure side-effect. | — |
| `kanban_comment` | Append a durable note to the task thread. | `task_id`, `body` |
| `kanban_attach` | Attach a file to a task by passing its bytes inline (base64); stored under the task's attachments dir (25 MB cap). | file bytes + name |
| `kanban_attach_url` | Attach a file to a task by URL. | `url` |
| `kanban_attachments` | List a task's attachments. | — |
| `kanban_create` | (Orchestrators) fan out into child tasks with an `assignee`, optional `parents`, `skills`, etc. Returns `gated: true` + `gated_by` when an open parent parked the new card in `todo`. | `title`, `assignee` |
| `kanban_link` | (Orchestrators) add a `parent_id → child_id` dependency edge after the fact. Returns `gated: true` when the child was `ready` and got demoted back to `todo` because the parent is not done — the child will only run after the parent completes. Refused with `child is already running` when the child is already claimed — an edge added after the claim cannot serialise the run (a worker may still link its *own* running card ahead of a `kind=dependency` block). | `parent_id`, `child_id` |
| `kanban_unblock` | (Orchestrators) restore a blocked task to its source phase (`review` or `ready`), or `todo` while a parent remains open. | `task_id` |

A typical worker turn looks like:

```
# Model's tool calls, in order:
kanban_show()                                     # no args — uses HERMES_KANBAN_TASK
# (model reads the returned worker_context, does the work via terminal/file tools)
kanban_heartbeat(note="halfway through — 4 of 8 files transformed")
# (more work)
kanban_complete(
    summary="migrated limiter.py to token-bucket; added 14 tests, all pass",
    metadata={"changed_files": ["limiter.py", "tests/test_limiter.py"], "tests_run": 14},
)
```

An **orchestrator** worker fans out instead:

```
kanban_show()
kanban_create(
    title="research ICP funding 2024-2026",
    assignee="researcher-a",
    body="focus on seed + series A, North America, AI-adjacent",
)
# → returns {"task_id": "t_r1", ...}
kanban_create(title="research ICP funding — EU angle", assignee="researcher-b", body="…")
# → returns {"task_id": "t_r2", ...}
kanban_create(
    title="synthesize findings into launch brief",
    assignee="writer",
    parents=["t_r1", "t_r2"],                     # promotes to ready when both complete
    body="one-pager, 300 words, neutral tone",
)
kanban_complete(summary="decomposed into 2 research tasks + 1 writer; linked dependencies")
```

The "(Orchestrators)" tools — `kanban_list`, `kanban_create`, `kanban_link`, `kanban_unblock`, and `kanban_comment` on foreign tasks — are available through the same toolset; the convention (encoded in the auto-injected kanban guidance) is that worker profiles don't fan out or route unrelated work, and orchestrator profiles don't execute implementation work. Dispatcher-spawned workers are still task-scoped for destructive lifecycle operations and cannot mutate unrelated tasks.

### Why tools instead of shelling to `hermes kanban`

Three reasons:

1. **Backend portability.** Workers whose terminal tool points at a remote backend (Docker / Modal / SSH / a plugin sandbox) would run `hermes kanban complete` *inside* the container, where `hermes` isn't installed and `~/.hermes/kanban.db` isn't mounted. The kanban tools run in the agent's own Python process and always reach `~/.hermes/kanban.db` regardless of terminal backend.
2. **No shell-quoting fragility.** Passing `--metadata '{"files": [...]}'` through shlex + argparse is a latent footgun. Structured tool args skip it entirely.
3. **Better errors.** Tool results are structured JSON the model can reason about, not stderr strings it has to parse.

**Zero schema footprint on normal sessions.** A regular `hermes chat` session has zero `kanban_*` tools in its schema unless the active profile explicitly enables the `kanban` toolset for orchestrator work. Dispatcher-spawned task workers get task-scoped tools because `HERMES_KANBAN_TASK` is set; orchestrator profiles get the broader routing surface through config. No tool bloat for users who never touch kanban.

The auto-injected kanban guidance teaches the model which tool to call when and in what order. It is injected only for the dispatcher-spawned worker that owns the task: an interactive session that merely has the `kanban` toolset enabled keeps the tools but is not told it "has been assigned ONE task", and a `delegate_task` child or a cron run fired from inside a worker — which inherit the worker's `HERMES_KANBAN_TASK` — neither receives the worker protocol nor records a terminal outcome against the worker's card when its own turn budget runs out.

### Recommended handoff evidence

`kanban_complete(summary=..., metadata={...})` is intentionally flexible:
the summary is the human-readable closeout, and `metadata` is the
machine-readable handoff that downstream agents, reviewers, or dashboards can
reuse without scraping prose.

For engineering and review tasks, prefer this optional metadata shape:

```json
{
  "changed_files": ["path/to/file.py"],
  "verification": ["pytest tests/hermes_cli/test_kanban_db.py -q"],
  "dependencies": ["parent task id or external issue, if any"],
  "blocked_reason": null,
  "retry_notes": "what failed before, if this was a retry",
  "residual_risk": ["what was not tested or still needs human review"]
}
```

These keys are a convention, not a schema requirement. The useful property is
that every worker leaves enough evidence for the next reader to answer four
questions quickly:

1. What changed?
2. How was it verified?
3. What can unblock or retry this if it fails?
4. What risk is still deliberately left open?

Keep secrets, raw logs, tokens, OAuth material, and unrelated transcripts out of
`metadata`. Store pointers and summaries instead. If a task has no files or
tests, say so explicitly in `summary` and use `metadata` for the evidence that
does exist, such as source URLs, issue ids, or manual review steps.

### The worker lifecycle

Every profile that works kanban tasks automatically gets the worker lifecycle — it's injected into the worker's system prompt at spawn (the `KANBAN_GUIDANCE` block), so there is **nothing to install or configure**. It teaches the worker the full lifecycle in **tool calls**, not CLI commands:

1. On spawn, call `kanban_show()` to read title + body + parent handoffs + prior attempts + full comment thread.
2. `cd $HERMES_KANBAN_WORKSPACE` (via the terminal tool) and do the work there.
3. Call `kanban_heartbeat(note="...")` every few minutes during long operations. **If your work may run longer than 1 hour, call `kanban_heartbeat` at least once an hour** — the dispatcher reclaims tasks that have been running past `kanban.dispatch_stale_timeout_seconds` (default 4 h) with no heartbeat in the last hour, on the assumption the worker crashed without cleanup. A reclaim is benign (the task goes back to `ready` for re-dispatch without a failure-counter tick) but you lose your current run's progress.
4. Complete with `kanban_complete(summary="...", metadata={...})`, hand a code change off for same-card review with `kanban_request_review(summary="...")`, or `kanban_block(reason="...")` if stuck.

Normal tool activity also extends the claim automatically (the worker mirrors its in-process liveness onto the board about once a minute). That bridge only works for a process the dispatcher spawned itself: a process that carries `HERMES_DELEGATED_CHILD_CONTEXT` next to `HERMES_KANBAN_TASK` (a `delegate_task` descendant, or a hand-launched copy of a worker's environment) is fenced from the board — its auto-heartbeat logs one `kanban auto-heartbeat for task … refused` warning and `kanban_complete` / `kanban_request_review` refuse. Fix the launch (let the dispatcher spawn the worker) rather than exporting the marker away.

That final terminal board call (`kanban_complete` / `kanban_request_review` /
`kanban_block`; reviewers end with `kanban_complete` or `kanban_request_changes`)
is part of the worker
protocol. If the worker process exits with status 0 while the task is still
`running`, the dispatcher treats that as a protocol violation and emits a
`protocol_violation` event, unless the worker's systemd scope records an OOM
or signal death. Scope `oom-kill` is recorded as `oom_killed`, and `signal` as
`signaled`, even when the agent handled SIGTERM and exited 0. These consume the
existing crash retry budget, not the protocol-violation budget. Run metadata and
the next worker's context carry the memory limit, peak, and remedy. Failed scopes
are retained until the reaper commits the diagnosis, then reset for collection.
Workers also snapshot cgroup memory counters on exit: older systemd releases do
not retain peak memory after cgroup deletion. If the worker itself takes SIGKILL
and systemd cannot report a peak, it is explicitly `unknown`, never estimated.

A dispatcher-spawned worker whose turn failed
therefore exits non-zero: `1` for an ordinary failure, `75`
(`EX_TEMPFAIL`) when the provider was rate-limited, overloaded, returning
5xx or timing out, or the account hit a billing/quota wall — the dispatcher records that run as `rate_limited` and
requeues the task without counting a failure, so a quota window is never
booked as a protocol violation — and `78` (`EX_CONFIG`) when the provider
rejected something a retry cannot fix: the profile's credential (401/403,
revoked or invalid key), the model (404 / model not found) or the TLS chain.
That **terminal provider error** trips the circuit breaker on the first
occurrence: the dispatcher records the run as `crashed` with
`exit_kind: terminal_provider`, emits `gave_up` with `terminal_provider: true`
and parks the card `blocked` (sticky — `recompute_ready` will not auto-resume
it) with the provider's own words in `last_failure_error`, instead of
re-spawning into the same wall until `kanban.failure_limit` / `max_retries`
is spent. Both lanes get the same booking: a reviewer worker that dies on a
revoked key parks the card exactly like an implementer. `hermes kanban show`
surfaces it as *Provider rejected this profile's credential or model — blocked
after one attempt*; fix the assignee profile's provider (`hermes -p <profile>
auth` / `setup`), then `hermes kanban unblock <id>`. The worker also writes its exit code as the
last line of its own log (`[kanban-worker-exit] rc=<code>`), so a per-tick
`hermes kanban dispatch` process — which never reaped the worker and cannot
read its exit status — books the same death the same way the gateway-embedded
dispatcher does; a worker killed before it reaches that line is a plain
`crashed` (`pid <n> not alive`).

**Agent-side prevention:** Before the worker exits, Hermes injects up to two
synthetic nudges when it detects the model is about to stop without a terminal
board tool call. This catches the common case where the model narrates the next
step ("Let me write the report") and stops with `finish_reason=stop`. The nudge
reminds the model to call `kanban_complete`, `kanban_request_review` or `kanban_block`
immediately. A worker that already handed its card off (`kanban_request_review`, or
`kanban_request_changes` from a reviewer) is never nudged — the handoff is its terminal
call, and the nudge never asks it to `kanban_complete` a card that is under review. This
guard is active only for the dispatcher-spawned worker itself (`HERMES_KANBAN_TASK` is
set and the run owns that task) — `delegate_task` children and cron jobs run inside
the worker inherit the variable but are never nudged, since they have no board tools —
and can be disabled with `HERMES_KANBAN_STOP_NUDGE=0`.

**Dispatcher-side recovery:** If the nudges are exhausted or the worker crashes
before reaching the nudge, the dispatcher gives the violation a **bounded retry**
(up to `_PROTOCOL_VIOLATION_FAILURE_LIMIT` consecutive violations, default 3)
before auto-blocking the task instead of respawning it into the same loop. The
budget counts only *consecutive* clean-exit protocol violations — interleaved
rate-limited requeues are neutral, and any other failure kind resets the
streak — and a per-task `max_retries` overrides the bound. A card blocked by
this budget stays blocked (it is not auto-promoted like a below-`failure_limit`
breaker block) until `hermes kanban unblock <id>`, which also grants a fresh
retry budget. This usually means
the model wrote a plain-text answer and exited without using the Kanban tool
surface.

The lifecycle plus the load-bearing reference details (workspace kinds, deliverable `artifacts`, claiming created cards) ship in that system-prompt block, so every worker has them regardless of which profile it runs under — no per-profile skill setup required.

**Worker session names.** A worker's session is titled after its card (`Fix the swap modal`, or `Kanban task <id>` when the board row can't be read) at spawn, so `hermes sessions` and session search show the card, not a model's guess. Workers never make the auxiliary `title_generation` model call that names interactive sessions; a manual `/title` in a worker session still wins.

### Pinning extra skills to a specific task

Sometimes a single task needs specialist context the assignee profile doesn't carry by default — a translation job that needs the `translation` skill, a review task that needs `github-code-review`, a security audit that needs `security-pr-audit`. Rather than editing the assignee's profile every time, attach the skills directly to the task.

**From an orchestrator agent** (the usual case — one agent routing work to another), use the `kanban_create` tool's `skills` array:

```
kanban_create(
    title="translate README to Japanese",
    assignee="linguist",
    skills=["translation"],
)

kanban_create(
    title="audit auth flow",
    assignee="reviewer",
    skills=["security-pr-audit", "github-code-review"],
)
```

**From a human (CLI / slash command)**, repeat `--skill` for each one:

```bash
hermes kanban create "translate README to Japanese" \
    --assignee linguist \
    --skill translation

hermes kanban create "audit auth flow" \
    --assignee reviewer \
    --skill security-pr-audit \
    --skill github-code-review
```

**From the dashboard**, type the skills comma-separated into the **skills** field of the create-task dialog.

The dispatcher emits one `--skills <name>` flag per skill listed, so the worker spawns with all of them loaded on top of the auto-injected kanban guidance. The skill names must match skills that are actually installed on the assignee's profile (run `hermes skills list` to see what's available); there's no runtime install.

### Per-task model override

Pin a task's worker to a specific model (and optionally provider), independent of the assignee profile's default:

```bash
# At creation
hermes kanban create "hard refactor" --assignee coder \
    --model claude-opus-4.6 --provider anthropic

# Or later — takes effect on the next dispatch
hermes kanban set-model t_abcd claude-opus-4.6 --provider anthropic
hermes kanban set-model t_abcd none    # clear the override
```

The dispatcher spawns the worker with the pinned model (`--provider <name>` is passed when set; `--provider` requires a model). The dashboard's per-task model dropdown drives the same `model_override` field. With no override, the worker uses its profile's configured model.

### Cost strategy: frontier orchestrator, inexpensive workers

Kanban's per-profile configs make the planner/worker cost split natural. Decomposing a project into well-scoped cards takes frontier-level judgment; executing a card that already carries a clear goal, context, and handoff evidence usually doesn't — and the workers are where the vast majority of tokens are spent, so the worker model is where the cost lives. Run your orchestrator/dispatcher profile on a frontier model and point worker profiles at inexpensive models. Each profile has its own `config.yaml` under `~/.hermes/profiles/<name>/`, and the dispatcher injects the profile-scoped `HERMES_HOME` when it spawns `hermes -p <assignee>`, so each worker reads its own profile's model settings:

```yaml
# ~/.hermes/config.yaml (orchestrator / dispatcher profile)
model:
  default: "your-frontier-model"

# ~/.hermes/profiles/coder/config.yaml (worker profile)
model:
  default: "your-inexpensive-model"

# ~/.hermes/profiles/researcher/config.yaml (another worker profile)
model:
  default: "your-inexpensive-model"
```

For the occasional quality-sensitive card, pin just that task back to a stronger model with the [per-task model override](#per-task-model-override) (`--model`/`--provider` at create time, `hermes kanban set-model` later, or the dashboard's model dropdown) — no profile edits needed.

### Lifecycle plugin hooks

Board transitions fire [plugin hooks](./hooks.md#plugin-hooks): `kanban_task_claimed`, `kanban_task_completed`, and `kanban_task_blocked`, each carrying `task_id` and `profile_name`. Hooks fire **after** the board DB change commits, so callbacks always see durable state. Note the process split: `kanban_task_claimed` fires in the **dispatcher** process, while `kanban_task_completed`/`kanban_task_blocked` fire in the **worker** process — register the hook in the dispatcher profile to observe every transition centrally.

```python
def register(ctx):
    def on_blocked(task_id=None, profile_name=None, **kw):
        ctx.dispatch_tool("terminal", {"command": f"notify-send 'kanban blocked: {task_id}'"})
    ctx.register_hook("kanban_task_blocked", on_blocked)
```

### Goal-mode cards (`--goal`)

By default each worker gets **one shot** at its card — do the work, call `kanban_complete`/`kanban_block`, exit. Pass `--goal` (CLI) or `goal_mode=True` (the `kanban_create` tool / dashboard) to instead run that worker in a **goal loop**, the same Ralph-style engine behind the `/goal` slash command: after every turn an auxiliary judge checks the worker's output against the card's title + body (treated as the acceptance criteria), and if the work isn't done — and the turn budget remains — the worker keeps going **in the same session** until the judge agrees, the worker terminates the task itself, or the budget runs out (which **blocks** the card for human review rather than exiting silently). If the judge rules the goal **unachievable** as written, the card is blocked immediately with the judge's reason — an impossible card is never marked done, and `kanban complete` / `kanban request-review` on such a card are rejected with a pointer to `kanban block` or re-scoping.

```bash
hermes kanban create "Translate the docs site to French" \
    --body "Acceptance: every page translated, no English left, links intact." \
    --assignee linguist \
    --goal \
    --goal-max-turns 15      # optional; default 20
```

Use it for open-ended, multi-step, or "keep going until X is true" cards. Skip it for cheap one-shot work — the per-turn judge overhead isn't worth it, and the dispatcher's existing retry/circuit-breaker already handles transient worker failures. The judge is only as good as your goal text, so write the body as **explicit acceptance criteria**.

The judge gate on `kanban complete` / `kanban request-review` (and the matching `kanban_complete` / `kanban_request_review` tools) only rejects on a **real verdict**. If the judge call itself fails — relay error, auth, timeout — the handoff is allowed and a `goal judge unreachable … allowing lifecycle handoff` warning lands in the log, so an unreachable judge never blocks a human approval. The headless CLI gate sends the same per-task relay-affinity key (`kanban:<task-id>`) as `specify`/`decompose`, so relays that require a session key accept the judge request.

A goal-mode worker's **Worker log** (dashboard drawer, `hermes kanban log <id>`) carries the same live tool feed as any other worker's, plus one `kanban goal loop: turn N/M verdict=…` line per judged turn, so you can follow what the loop is doing while the card is running.

:::note Goal-mode cards borrow the `/goal` engine — they don't connect to it
`--goal` runs the continuation loop *inside that one card's worker session*. It shares the engine with the [`/goal` slash command](./goals), not the state: setting a `/goal` in a chat session never creates, claims, or moves a kanban card, and a goal-mode card's loop is invisible to any chat session's `/goal status`. If you want this conversation to keep iterating, use [`/goal`](./goals); if you want work on the board, create a card.
:::

### How the orchestrator behaves

A **well-behaved orchestrator does not do the work itself.** It decomposes the user's goal into tasks, links them, assigns each to one of the profiles you've set up, and steps back. The orchestrator guidance — anti-temptation rules, a Step-0 profile-discovery prompt (the dispatcher silently fails on unknown assignee names, so the orchestrator must ground every card in profiles that actually exist on your machine), and a decomposition playbook keyed on `kanban_create` / `kanban_link` / `kanban_comment` — is injected into the worker's system prompt automatically; there is nothing to install.

A canonical orchestrator turn (two parallel researchers handing off to a writer):

```
# Goal from user: "draft a launch post on the ICP funding landscape"
kanban_create(title="research ICP funding, NA angle",  assignee="researcher-a", body="…")  # → t_r1
kanban_create(title="research ICP funding, EU angle",  assignee="researcher-b", body="…")  # → t_r2
kanban_create(
    title="synthesize ICP funding research into launch post draft",
    assignee="writer",
    parents=["t_r1", "t_r2"],        # promoted to 'ready' when both researchers complete
    body="one-pager, neutral tone, cite sources inline",
)                                     # → t_w1
# Optional: add cross-cutting deps discovered later without re-creating tasks
kanban_link(parent_id="t_r1", child_id="t_followup")
kanban_complete(
    summary="decomposed into 2 parallel research tasks → 1 synthesis task; writer starts when both researchers finish",
)
```

The orchestrator guidance ships in the worker's system prompt automatically — there is nothing to install or sync per profile.

**Decide before you fan out.** Design decisions belong to the orchestrator, not to the workers. If two parallel cards would each have to pick the same thing — a naming scheme, a schema, a file format, an API shape — the orchestrator decides it once and stamps the decision into **both** card bodies. Workers cannot see sibling cards, so every child card body must carry every decision it depends on. Example: for the parallel cards "build the exporter" and "build the importer", don't let each worker invent its own file format — pick one up front (say, newline-delimited JSON with a `version` field) and write it into both bodies, or the two halves will never round-trip.

For best results, pair it with a profile whose toolsets are restricted to board operations (`kanban`, `gateway`, `memory`) so the orchestrator literally cannot execute implementation tasks even if it tries.

## Dashboard (GUI)

The `/kanban` CLI and slash command are enough to run the board headlessly, but a visual board is often the right interface for humans-in-the-loop: triage, cross-profile supervision, reading comment threads, and dragging cards between columns. Hermes ships this as a **bundled dashboard plugin** at `plugins/kanban/` — not a core feature, not a separate service — following the model laid out in [Extending the Dashboard](./extending-the-dashboard).

Open it with:

```bash
hermes kanban init      # one-time: create kanban.db if not already present
hermes dashboard        # "Kanban" tab appears in the nav, after "Skills"
```

### What the plugin gives you

- A **Kanban** tab showing one column per status: `triage`, `todo`, `ready`, `running`, `blocked`, `done` (plus `archived` when the toggle is on).
  - Queue columns list cards in dispatch order (priority, then oldest first — top card spawns next). The `done` column is history and lists newest-completed first; `hermes kanban list --status done --sort completed-desc` gives the same order on the CLI.
  - `triage` is the parking column for rough ideas. By default (`kanban.auto_decompose: true`), the dispatcher auto-runs the **decomposer** on tasks that land here. The built-in decomposer uses the `auxiliary.kanban_decomposer` model path, reads your profile roster (with descriptions), and fans the task out into a small graph of child tasks routed to the best-fit specialists. The original task stays alive as the parent of every child so its assignee (`kanban.orchestrator_profile`, else the assignee the task already had, else the active default profile) wakes back up to judge completion when everything finishes. Flip the **Orchestration: Auto/Manual** pill at the top of the page (emerald = Auto, muted gray = Manual), or by editing `config.yaml` directly. Both modes coexist with `hermes kanban specify` - that's still available as a single-task spec rewrite when you don't want fan-out.
- Cards show the task id, title, priority badge, tenant tag, assigned profile, comment/link counts, a **progress pill** (`N/M` children done when the task has dependents), and "created N ago". A per-card checkbox enables multi-select.
- **Per-profile lanes inside Running** — toolbar checkbox toggles sub-grouping of the Running column by assignee.
- **Live updates via WebSocket** — the plugin tails the append-only `task_events` table on a short poll interval; the board reflects changes the instant any profile (CLI, gateway, or another dashboard tab) acts. Reloads are debounced so a burst of events triggers a single refetch.
- **Drag-drop** cards between columns to change status. The drop sends `PATCH /api/plugins/kanban/tasks/:id` which routes through the same `kanban_db` code the CLI uses — the three surfaces can never drift. Moves into destructive statuses (`done`, `archived`, `blocked`) prompt for confirmation. Touch devices use a pointer-based fallback so the board is usable from a tablet.
- **Create-task dialog** — click `+` on any column header to open a modal with labeled fields: title, assignee, priority, skills, workspace kind/path (seeded from the board's project directory; per-task override), goal mode, and (optionally) a parent task from a dropdown over every existing task. Press Enter to create the task, Shift+Enter to insert a newline in the title field, or Escape to cancel. Creating from the Triage column automatically parks the new task in triage.
- **Multi-select with bulk actions** — shift/ctrl-click a card or tick its checkbox to add it to the selection. A bulk action bar appears at the top with batch status transitions, archive, and reassign (by profile dropdown, or "(unassign)"). Destructive batches confirm first. Per-id partial failures are reported without aborting the rest.
- **Click a card** (without shift/ctrl) to open a side drawer (Escape or click-outside closes) with:
  - **Editable title** — click the heading to rename.
  - **Editable assignee / priority** — click the meta row to rewrite.
  - **Editable description** — markdown-rendered by default (headings, bold, italic, inline code, fenced code, `http(s)` / `mailto:` links, bullet lists), with an "edit" button that swaps in a textarea. Markdown rendering is a tiny, XSS-safe renderer — every substitution runs on HTML-escaped input, only `http(s)` / `mailto:` links pass through, and `target="_blank"` + `rel="noopener noreferrer"` are always set.
  - **Dependency editor** — chip list of parents and children, each with an `×` to unlink, plus dropdowns over every other task to add a new parent or child. Cycle attempts are rejected server-side with a clear message.
  - **Status action row** (→ triage / → ready / → running / block / unblock / complete / archive) with confirm prompts for destructive transitions. For cards in the **Triage** column the row also exposes two LLM-driven actions: **⚗ Decompose** fans the task out into a graph of child tasks routed to specialist profiles by description, and **✨ Specify** does a single-task spec rewrite. Decompose falls back to specify-style promotion when the LLM decides the task doesn't benefit from fan-out, so it's a strict superset. Both are reachable from the CLI (`hermes kanban decompose <id>` / `specify <id>` / `--all`), from any gateway platform (`/kanban decompose <id>`), and programmatically via `POST /api/plugins/kanban/tasks/:id/decompose` and `…/specify`. Configure the models under `auxiliary.kanban_decomposer` and `auxiliary.triage_specifier` in `config.yaml`.
  - Result section (also markdown-rendered), comment thread with Enter-to-submit, the last 20 events.
- **Toolbar filters** — free-text search, tenant dropdown (defaults to `dashboard.kanban.default_tenant` from `config.yaml`), assignee dropdown, "show archived" toggle, "lanes by profile" toggle, and a **Nudge dispatcher** button so you don't have to wait for the next 60 s tick.

Visually the target is the familiar Linear / Fusion layout: dark theme, column headers with counts, coloured status dots, pill chips for priority and tenant. The plugin reads only theme CSS vars (`--color-*`, `--radius`, `--font-mono`, ...), so it reskins automatically with whichever dashboard theme is active.

### Auto vs Manual orchestration

The kanban board has two ways to handle a task you drop into the Triage column:

**Auto (default)** — `kanban.auto_decompose: true`. The gateway-embedded dispatcher runs the **decomposer** on each tick, capped by `kanban.auto_decompose_per_tick` (default 3 tasks per tick) so a bulk-load of triage tasks doesn't burst-spend the auxiliary LLM. The decomposer uses the built-in decomposition prompt plus the `auxiliary.kanban_decomposer` model path, reads your installed profiles + their descriptions, and asks the LLM to produce a JSON task graph: which tasks to spawn, who they go to, and which depend on which. The original triage task becomes the parent of every leaf in the graph, so it stays alive until the whole graph completes - and then promotes back to `ready` so its assignee (`kanban.orchestrator_profile`, else the assignee the task already had, else the active default profile) can judge completion and add more tasks if the work isn't done. This is the "drop a one-liner, walk away" flow.

A completed built-in fan-out is recorded atomically with its child graph. Moving
that root back to Triage does not create another graph; ordinary prerequisite
links do not prevent a task's first decomposition. The completion marker survives
event retention until the task is deleted. This is not semantic deduplication of
independently created manual graphs, nor a repair for previously pruned history.

When a new task omits its tenant, creation inherits the first nonempty tenant
among its parents, in supplied order. An explicit tenant (including the worker's
active tenant passed by tools) wins. Boards remain the hard isolation boundary.

**Manual** — `kanban.auto_decompose: false`. Triage tasks stay in triage until you act. Click the **⚗ Decompose** button on a card, run `hermes kanban decompose <id>` (or `--all`), or use `/kanban decompose <id>` from a chat. This matches the pre-decomposer behavior of the board, useful when you want full control over what runs when.

**Important boundary:** Manual mode disables only the built-in Triage decomposer. It does not prevent a profile from calling `kanban_create`, and it does not disable creator-session wake-ups. With `kanban.auto_subscribe_on_create: true`, a task's terminal event resumes the originating agent with a synthetic status turn so it can inspect the handoff and decide whether genuinely new follow-up work is needed. Set `auto_subscribe_on_create: false` when task completion should remain passive. For provenance, built-in decomposer children use `created_by=auto-decomposer`; tasks created by a resumed profile carry that profile name instead.

Flip between the two modes from the **Orchestration: Auto/Manual** pill at the top of the kanban page (emerald = Auto, muted gray = Manual), or by editing `config.yaml` directly. Both modes coexist with `hermes kanban specify` — that's still available as a single-task spec rewrite when you don't want fan-out.

The decomposer's routing decisions depend on profile descriptions, which is a per-profile labeling primitive you set with `hermes profile create --description "..."`, `hermes profile describe <name> --text "..."`, `hermes profile describe <name> --auto` (LLM-generates from the profile's installed skills + model), or the dashboard's per-profile editor in the expanded **Orchestration settings** panel. Profiles without a description still appear in the roster — they're routable by name, just less precisely. The decomposer NEVER lands a child task with `assignee=None`: when the LLM picks an unknown profile, the child gets routed to `kanban.default_assignee`, else the root task's assignee (if it names an existing profile), else the active default profile.

`kanban.orchestrator_profile` does not load that profile's prompt, skills, or custom logic into the decomposition call. It controls who owns the root/orchestration task after fan-out. To change the decomposer's model/provider, configure `auxiliary.kanban_decomposer`. To use a profile's custom task-splitting logic instead of the built-in decomposer, switch to Manual mode and have that profile create or decompose tasks explicitly.

Config knobs (all under `kanban:` in `~/.hermes/config.yaml`):

| Key | Default | Purpose |
|---|---|---|
| `auto_decompose` | `true` | Dispatcher auto-runs the built-in decomposer for Triage tasks every tick. It does not gate profile-driven `kanban_create` calls or creator wake turns. |
| `auto_decompose_per_tick` | `3` | Cap on decompositions per dispatcher tick. Excess defers to the next tick. |
| `auto_decompose_min_age_seconds` | `120` | `kanban.auto_decompose_min_age_seconds`: the dispatcher's auto-decompose sweep skips triage cards younger than this, so a creator can `kanban_schedule` a card straight from triage before it is specified. Triage is not a hold. Manual `decompose`/`specify` ignore it; `0` disables the wait. |
| `orchestrator_profile` | `""` | Profile assigned to the root/orchestration task after decomposition. Empty = the root task keeps its own assignee, else the active default profile. |
| `default_assignee` | `""` | Where a child task lands when the LLM picks an unknown profile. Empty = fall back to the root task's assignee, else the active default. |
| `auto_subscribe_on_create` | `true` | When `kanban_create` runs inside a persistent gateway/TUI session, terminal events resume that originating agent with a synthetic status turn. Set to `false` for passive completion or to require explicit `kanban_notify-subscribe` calls. Independent of `auto_decompose`. |
| `notify_in_gateway` | `true` | Poll and deliver Kanban subscriptions from this gateway. Set to `false` on profiles that own no notification subscriptions to stop the idle five-second notifier poll. Independent of `dispatch_in_gateway`; non-dispatch gateways may still own profile-specific delivery adapters. |
| `done_sub_retention_days` | `30` | Notify subscriptions survive `done` (reopen-safe) and are removed on `archived`. The notifier GC purges subscriptions whose task has been `done` or `blocked` with no new events for this many days, bounding sub-table growth on boards that never archive. `0` disables the sweep. |

And the two auxiliary LLM slots:

| Key | Purpose |
|---|---|
| `auxiliary.kanban_decomposer` | Model that produces the task graph (called by Decompose). Set `provider`/`model` to override the main chat model. |
| `auxiliary.profile_describer` | Model that auto-generates profile descriptions (called by `hermes profile describe --auto`). |

### Architecture

The GUI is strictly a **read-through-the-DB + write-through-kanban_db** layer with no domain logic of its own:

<!-- ascii-guard-ignore -->
```
┌────────────────────────┐      WebSocket (tails task_events)
│   React SPA (plugin)   │ ◀──────────────────────────────────┐
│   HTML5 drag-and-drop  │                                    │
└──────────┬─────────────┘                                    │
           │ REST over fetchJSON                              │
           ▼                                                  │
┌────────────────────────┐     writes call kanban_db.*        │
│  FastAPI router        │     directly — same code path      │
│  plugins/kanban/       │     the CLI /kanban verbs use      │
│  dashboard/plugin_api.py                                    │
└──────────┬─────────────┘                                    │
           │                                                  │
           ▼                                                  │
┌────────────────────────┐                                    │
│  ~/.hermes/kanban.db   │ ───── append task_events ──────────┘
│  (WAL, shared)         │
└────────────────────────┘
```
<!-- ascii-guard-ignore-end -->

### REST surface

All routes are mounted under `/api/plugins/kanban/` and protected by the dashboard's ephemeral session token:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/board?tenant=<name>&include_archived=…` | Full board grouped by status column, plus tenants + assignees for filter dropdowns |
| `GET` | `/tasks/:id` | Task + comments + events + links |
| `POST` | `/tasks` | Create (wraps `kanban_db.create_task`, accepts `triage: bool` and `parents: [id, …]`) |
| `PATCH` | `/tasks/:id` | Status / assignee / priority / title / body / result |
| `POST` | `/tasks/bulk` | Apply the same patch (status / archive / assignee / priority) to every id in `ids`. Per-id failures reported without aborting siblings |
| `POST` | `/tasks/:id/comments` | Append a comment |
| `POST` | `/tasks/:id/specify` | Run the triage specifier — auxiliary LLM fleshes out the task body and promotes it from `triage` to `todo`. Returns `{ok, task_id, reason, new_title}`; `ok=false` with a human-readable reason on "not in triage" / no aux client / LLM error is a 200, not a 4xx |
| `POST` | `/tasks/:id/decompose` | Run the kanban decomposer — auxiliary LLM produces a task graph and the helper atomically creates the children + links the root + flips `triage → todo`. Returns `{ok, task_id, reason, fanout, child_ids, new_title}`. Same 200-on-LLM-error convention as `/specify`. |
| `GET` | `/profiles` | List installed profiles with their descriptions (consumed by the dashboard's profile-description editor and the orchestrator picker). |
| `PATCH` | `/profiles/:name` | Set or clear a profile's description (user-authored — `description_auto: false`). Returns `{ok, profile, description}`. |
| `POST` | `/profiles/:name/describe-auto` | Generate a description for a profile via `auxiliary.profile_describer`. Persists with `description_auto: true` so the dashboard can surface a "review" badge. |
| `GET` | `/orchestration` | Read the kanban orchestration settings (`orchestrator_profile`, `default_assignee`, `auto_decompose`) plus the *resolved* effective values after fallbacks. |
| `PUT` | `/orchestration` | Update one or more of the three orchestration keys in `config.yaml`. Validates that non-empty profile names actually exist. |
| `POST` | `/links` | Add a dependency (`parent_id` → `child_id`) |
| `DELETE` | `/links?parent_id=…&child_id=…` | Remove a dependency |
| `POST` | `/dispatch?max=…&dry_run=…` | Nudge the dispatcher — skip the 60 s wait |
| `GET` | `/config` | Read `dashboard.kanban` preferences from `config.yaml` — `default_tenant`, `lane_by_profile`, `include_archived_by_default`, `render_markdown` |
| `WS` | `/events?since=<event_id>` | Live stream of `task_events` rows. Without `since` the stream starts at the board's current tail (the `/board` snapshot already holds the past); pass `since=<latest_event_id>` to catch up from there, or `since=0` to replay history |

Every handler is a thin wrapper — the plugin is ~700 lines of Python (router + WebSocket tail + bulk batcher + config reader) and adds no new business logic. A tiny `_conn()` helper auto-initializes `kanban.db` on every read and write, so a fresh install works whether the user opened the dashboard first, hit the REST API directly, or ran `hermes kanban init`.

### Dashboard config

Any of these keys under `dashboard.kanban` in `~/.hermes/config.yaml` changes the tab's defaults — the plugin reads them at load time via `GET /config`:

```yaml
dashboard:
  kanban:
    default_tenant: acme              # preselects the tenant filter
    lane_by_profile: true             # default for the "lanes by profile" toggle
    include_archived_by_default: false
    render_markdown: true             # set false for plain <pre> rendering
```

Each key is optional and falls back to the shown default.

### Security model

The dashboard's HTTP auth middleware [explicitly skips `/api/plugins/`](./extending-the-dashboard#backend-api-routes) — plugin routes are unauthenticated by design because the dashboard binds to localhost by default. That means the kanban REST surface is reachable from any process on the host.

The WebSocket takes one additional step: it requires the dashboard's ephemeral session token as a `?token=…` query parameter (browsers can't set `Authorization` on an upgrade request), matching the pattern used by the in-browser PTY bridge.

If you run `hermes dashboard --host 0.0.0.0`, every plugin route — kanban included — becomes reachable from the network. **Don't do that on a shared host.** The board contains task bodies, comments, and workspace paths; an attacker reaching these routes gets read access to your entire collaboration surface and can also create / reassign / archive tasks.

Tasks in `~/.hermes/kanban.db` are profile-agnostic on purpose (that's the coordination primitive). If you open the dashboard with `hermes -p <profile> dashboard`, the board still shows tasks created by any other profile on the host. Same user owns all profiles, but this is worth knowing if multiple personas coexist.

### Live updates

`task_events` is an append-only SQLite table with a monotonic `id`. The WebSocket endpoint holds each client's last-seen event id and pushes new rows as they land. When a burst of events arrives, the frontend reloads the (very cheap) board endpoint — simpler and more correct than trying to patch local state from every event kind. WAL mode means the read loop never blocks the dispatcher's `BEGIN IMMEDIATE` claim transactions.

### Extending it

The plugin uses the standard Hermes dashboard plugin contract — see [Extending the Dashboard](./extending-the-dashboard) for the full manifest reference, shell slots, page-scoped slots, and the Plugin SDK. Extra columns, tenant-filtered layouts, or full `tab.override` replacements are all expressible without forking this plugin. The dashboard has no per-card slot; on the desktop board, plugins add per-card labels through the `KANBAN_CARD_AREAS.badges` contribution area (see [Desktop Plugin SDK](../../developer-guide/desktop-plugin-sdk.md#kanban-card-badges)).

To disable without removing: add `dashboard.plugins.kanban.enabled: false` to `config.yaml` (or delete `plugins/kanban/dashboard/manifest.json`).

### Scope boundary

The GUI is deliberately thin. Everything the plugin does is reachable from the CLI; the plugin just makes it comfortable for humans. Auto-assignment, budgets, governance gates, and org-chart views remain user-space — a router profile, another plugin, or a reuse of `tools/approval.py` — exactly as listed in the out-of-scope section of the design spec.

## CLI command reference

This is the surface **you** (or scripts, cron, the dashboard) use to drive the board. Workers running inside the dispatcher use the `kanban_*` [tool surface](#how-workers-interact-with-the-board) for the same operations — the CLI here and the tools there both route through `kanban_db`, so the two surfaces agree by construction.

```
hermes kanban init                                     # create kanban.db + print daemon hint
hermes kanban create "<title>" [--body ...] [--assignee <profile>]
                                [--parent <id>]... [--tenant <name>]
                                [--workspace scratch|worktree|worktree:<path>|dir:<path>]
                                [--branch <name>]
                                [--priority N] [--triage] [--idempotency-key KEY]
                                [--max-runtime 30m|2h|1d|<seconds>]
                                [--max-retries N]
                                [--goal] [--goal-max-turns N]
                                [--review-required]
                                [--reasoning-effort low|medium|high|none]
                                [--skill <name>]...
                                [--json]
hermes kanban list [--mine] [--assignee P] [--status S] [--tenant T] [--archived]
        [--workflow-template-id <id>] [--current-step-key <key>]
        [--sort completed-desc|created|created-desc|priority|priority-desc|status|assignee|title|updated]
        [--json]
hermes kanban show <id> [--json]
hermes kanban assign <id> <profile>                    # or 'none' to unassign
hermes kanban reassign <id>... <profile>               # bulk re-assign tasks to a profile
hermes kanban edit <id> [--title ...] [--body ...]     # edit task title / body / priority in place
        [--priority N]
        [--review-required on|off]                     # 'off' is operator-only (see below)
hermes kanban promote <id>...                          # move todo/blocked/triage tasks to ready (recovery)
hermes kanban schedule <id> --at <ISO8601>             # set/clear a task's scheduled_at start time
hermes kanban diagnostics [--json]                     # board health snapshot (alias: diag)
hermes kanban link <parent_id> <child_id>
hermes kanban unlink <parent_id> <child_id>
hermes kanban claim <id> [--ttl SECONDS]
hermes kanban comment <id> "<text>" [--author NAME]

# Bulk verbs — accept multiple ids:
hermes kanban complete <id>... [--result "..."] [--force]
hermes kanban block <id> "<reason>" [--ids <id>...]
hermes kanban unblock <id>...
hermes kanban archive <id>...

hermes kanban request-review <id> [--summary "..."] [--metadata JSON] [--reviewer PROFILE]
hermes kanban request-changes <id> "<required changes>"               # active reviewer -> implementer
hermes kanban reopen-review  <id>... [--reason "..."]                 # changes requested: 'review' -> ready/todo
hermes kanban resume         <id> --reason "..."                     # operator-only: one run past an active_pr guard hold

hermes kanban tail <id>                                # follow a single task's event stream
hermes kanban watch [--assignee P] [--tenant T]        # live stream ALL events to the terminal
        [--kinds completed,blocked,…] [--interval SECS]
hermes kanban heartbeat <id> [--note "..."]            # worker liveness signal for long ops
hermes kanban runs <id> [--json]                       # attempt history (one row per run)
hermes kanban assignees [--json]                       # profiles on disk + per-assignee task counts
hermes kanban dispatch [--dry-run] [--max N]           # one-shot pass
        [--failure-limit N] [--json]
hermes kanban daemon --force                           # DEPRECATED — standalone dispatcher (use `hermes gateway start` instead)
        [--failure-limit N] [--pidfile PATH] [-v]
hermes kanban stats [--json]                           # per-status + per-assignee counts
hermes kanban log <id> [--tail BYTES]                  # worker log from ~/.hermes/kanban/logs/
hermes kanban notify-subscribe <id>                    # gateway bridge hook (used by /kanban in the gateway)
        --platform <name> --chat-id <id> [--thread-id <id>] [--user-id <id>]
        [--chat-type dm|group|channel|thread] [--delivery-mode notify|notify+wake|wake]
hermes kanban notify-list [<id>] [--json]
hermes kanban notify-unsubscribe <id>
        --platform <name> --chat-id <id> [--thread-id <id>]
hermes kanban context <id>                             # what a worker sees
hermes kanban specify [<id> | --all] [--tenant T]      # flesh out a triage-column idea
        [--author NAME] [--json]                       #   into a full spec and promote to todo
hermes kanban gc [--event-retention-days N] [--worktree-retention-days N] # workspaces + old events + old logs
        [--log-retention-days N]                       #   (negative N is rejected; 0 disables that sweep)
```

A card created with `--review-required` (tool: `review_required=true`) cannot be
completed by its implementer, even with `--force`: the implementer calls
`kanban_request_review`, and a reviewer that claims the review run approves it.
Only an operator (outside a kanban worker run: neither `HERMES_KANBAN_TASK` nor
`HERMES_KANBAN_WORKER_TASK` is set) may approve a card pending in `review`
without claiming it, or turn the requirement off with
`hermes kanban edit <id> --review-required off`. Every kanban worker run,
dispatched orchestrator workers included, is refused both.

Worktree GC also covers done, failed, and cancelled tasks. After
`kanban.worktree_retention_days` (default 7, overridden by the GC flag), clean
worktrees may be removed even with unpushed commits: their HEAD and history are
first retained in the repository at `refs/hermes/kanban-archive/<task-id>/<commit>`.
Zero disables this age-based archival; legacy clean, fully-pushed cleanup remains.
For a sub-day window set `kanban.worktree_retention_hours` to a non-negative number
of hours (fractions allowed, e.g. `3` or `1.5`); when set it takes precedence over
`worktree_retention_days`, and `0` disables age-based archival. Unset or `null` (the
default) keeps the days behaviour. An invalid value (a boolean, a string, NaN,
infinity or a negative number) disables age-based archival and logs a warning; it
never falls back to the days key. Remove the key to return to the days window.
Dirty trees, locked trees, active tasks, active children and live shared workspaces
are preserved. Custom branches remain; generated `wt/` branches are pruned only
when their tip still matches the archived commit.

`kanban.scratch_gc_preserve` pins scratch trees explicitly: a list of task ids
(default `[]`) whose workspaces no cleanup path touches — completion cleanup,
deferred parent cleanup, retention GC and its archive pass, custody GC for evicted
cards, custody receipt discovery and the worker disk-pressure reclaimer. A tree is
pinned when its task id is listed or any component of its path is a listed id.
A value that is not a list of task-id strings, or a config that cannot be read,
keeps every scratch tree and logs a warning.

```yaml
kanban:
  scratch_gc_preserve: [t_01a1a7927a933768, t_01a19fc2faa99ef8]
```

On a Board worker, a scratch tree whose card was evicted from the local board is
reclaimed through a custody receipt and the Board's terminal evidence. A Board row
`archived` without `completed_at` uses the newest of its update time and its last
ended run as the retention clock. A `<root>/t_<16 hex>` tree with neither a local
row nor a receipt (an eviction before receipts existed) gets a receipt only when the
Board row is a scratch task and the Board shows a claim of it by this machine's
hostname whose runs have all ended; otherwise it stays `owner_unverified`.

CLI worktree startup maintenance applies the same policy across the current home's
boards. Registered `t_<hex>` worktrees with no known owning workspace are not
silently deleted: they receive JSON tombstones in
`<hermes-home>/archive/kanban-orphans/` for manual review. Unreadable boards abort
the startup task sweep rather than treating their workspaces as orphans.

To recover archived committed work, run these commands in the original repository:

```bash
git for-each-ref refs/hermes/kanban-archive/
git worktree add -b recovered-task ../recovered-task refs/hermes/kanban-archive/<task-id>/<commit>
```

Recovery refs do not expire automatically and survive ordinary `git gc`. They
are local references, not remote backups; retain the original repository.

All commands are also available as a slash command in the interactive CLI and in the messaging gateway (see [`/kanban` slash command](#kanban-slash-command) below).

`--max-retries` is a per-task circuit-breaker override for the dispatcher. `--max-retries 1` blocks the task on the first non-successful attempt, while `--max-retries 3` allows two retries and blocks on the third failure. Omit it to use `kanban.failure_limit` from `config.yaml`, then the built-in default.

### Concurrency, scheduling, and child promotion config

| Config key | Default | What it does |
|------------|---------|--------------|
| `kanban.max_in_progress` | unset (unlimited) | Caps the number of simultaneously running tasks. When the board already has N running, the dispatcher skips spawning more — useful for slow workers (local LLMs, resource-constrained hosts) so they finish what they have before more pile up and time out. Invalid or below-1 values log a warning and behave as unlimited. |
| `kanban.max_in_progress_per_profile` | unset (unlimited) | Per-profile variant of `max_in_progress` — caps how many tasks any single assignee profile may run concurrently. Useful when one profile is slow or rate-limited but others should keep flowing. Applies alongside the board-wide `max_in_progress`; both must allow a spawn for it to proceed. |
| `kanban.dispatch_profiles` | unset (any existing profile) | Per-home claim allowlist for boards shared across Hermes homes. When the key is present, this home's dispatcher only claims cards whose assignee is listed — fail-closed: an empty list, `null` or a bare `dispatch_profiles:` claims nothing, and a config read that fails logs a warning and claims nothing; other assignees land in `skipped_nonspawnable`. Only omitting the key means "any existing profile". `hermes kanban diagnostics` prints the resolved value for this home (`any`, the listed names, or `none (fail-closed: …)`). See [Shared boards across homes](#shared-boards-across-homes). |
| `kanban.auto_promote_children` | `true` | After `decompose_triage_task()` produces children with no parent-blocker dependencies, they're automatically promoted to `ready` so the dispatcher can pick them up. Set to `false` to require manual review — children stay in `todo` until you promote them. |
| `kanban.default_workdir` | unset | Board-level default working directory applied to new tasks when neither `--workspace` nor the task itself overrides it. Per-task `workspace:` still wins. |

```yaml
kanban:
  max_in_progress: 2
  auto_promote_children: false
  default_workdir: ~/work/active-project
```

### Scheduled task starts (`scheduled_at`)

Set `scheduled_at` on a task to delay dispatch until a specific time. The dispatcher skips ready tasks whose `scheduled_at` is in the future and picks them up on the first tick after that timestamp.

```bash
hermes kanban create "nightly backup audit" \
  --assignee ops --scheduled-at "2026-06-01T03:00:00Z"
```

### Respawn guard

The dispatcher refuses to re-spawn a ready task when it hit an auth error (401, invalid API key, unauthorized, billing) on the previous run (`blocker_auth`), or completed a run successfully within the guard window (`recent_success`), or a recent task comment links to a GitHub PR (`active_pr`). Two cooldowns hold a card without ever counting against it: `rate_limit_cooldown` after a quota-wall requeue and `infrastructure_cooldown` after the host refused to place the worker (no restart-safe systemd scope — see [Workers and systemd cgroups](#workers-and-systemd-cgroups)); both share the `HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS` window (default 300 s). Quota / rate-limit / 429 text in `last_failure_error` always takes the `rate_limit_cooldown` path, even when a later non-failure run superseded the `rate_limited` one; once the cooldown elapses the stamp is cleared and the card retries. A quota wall is never `blocker_auth` and never escalates to a human block kind, and a legacy guard-hold block on a quota error is released automatically once another card completes after it. This prevents repeat worker storms on the same bug or task while a human catches up. See the `respawn_guarded` row in the [event reference](#event-reference).

## Project lanes

Lanes split worker time between projects. A card's lane is its `tenant`; the installation-root `config.yaml` names the lanes under `kanban.admission.lanes`. With no `lanes` mapping nothing changes.

```yaml
kanban:
  admission:
    lane_default: other          # lane of a card nothing else places (must be a configured lane)
    lane_shadow: false           # true = classify and report only: nothing is held, capped or reordered
    agent_priority_cap: 100      # stored priority ceiling for cards the user did not create
    lanes:
      babybear: {share: 35, describe: "Baby Bear app and firmware"}
      hermes:
        share: 20
        gated: true              # cards filed by other cards or by detectors start held in triage
        admit_per_day: 50
        cards_per_day: 50        # optional; new cards wait at the gate after this many admissions today
        usd_per_day: 40          # optional; new cards wait at the gate after this much run spend today
        match: {creators: [self-heal], key_prefixes: ["ci-main-watch:"], repos: [hermes-agent], self_work: true}
        gate_exempt: ['independent review']
        never_admit: ['lifecycle guard']
      other: {share: 10}
```

- **Classification** happens once, at creation: the user's explicit `tenant`, then a lane whose `match` fits (creator name, idempotency-key prefix, repository directory name, or a declared `self_work`), then a tenant an agent passed (workers pass their own card's), the parent's or the creating card's lane, then `lane_default` with a `lane_unclassified` event. Every decision is a `lane_assigned` event with its source.
- **Shares are targets, not ceilings.** Under contention each free slot goes to the lane running the fewest cards relative to its `share`; inside a lane the admission score decides. A lane with nothing ready lends its share. Land-turn owners, `urgent`/`capacity` cards and alarm cards go first, in score order, ahead of every lane pick. `paused: true` stops a lane's cards from being dispatched.
- **The gate.** In a `gated` lane a card filed by another card, or by a `match` creator or key prefix, starts in `triage` with `block_kind = lane_gate` until it is admitted. The user's own cards (a human shell, a slash command the person typed, or the dashboard, with no worker card behind the call), kernel recovery cards, alarm-keyed and `urgent` cards, and `gate_exempt` titles under an admitted creating card are not held. While lanes are enforced (not in shadow mode), `hermes kanban create` from an agent's terminal stores `created_by: agent` when it would have stored `user` or `dashboard`. A held card that a runnable (todo, ready, running or review) card of another lane is linked to wait on is admitted at once, outside the budget, unless its title is `never_admit`. While the gate is enforced, the stale-triage sweep, `specify` and `decompose` (the auto-decomposer and `--force` included) leave held cards alone; with lanes removed or in shadow they treat them as ordinary triage, so a rollback strands nothing.
- **Shadow mode.** `lane_shadow: true` is the step before enforcement: cards are classified and `hermes kanban lanes` reports them, and each `lane_assigned` event records `would_gate`, but the gate, the cap, the lane ordering and `paused` do nothing, and `created_by` is stored as given. The only change to a card is its `tenant`.
- **Priority cap.** `agent_priority_cap` bounds the stored priority of a card the user did not create, at creation. Later edits are not capped, and the planner may set 0 to 1000.
- **Limits.** Worker nodes order foreign pickup with their own board copy, so lane order is exact only on the board that holds the cards; `paused` applies where `lanes` is configured. A worker that parks on a held follow-up card waits until that card is admitted.

`usd_per_day` counts only `usage.cost_usd` reported on worker-result receipts; runs reporting tokens but no cost add zero, not an inferred price. Daily caps reserve admissions, not running slots: lowering a limit does not interrupt admitted work. Review/landing titles in `gate_exempt` under an admitted creator remain exempt. Daily-limit-only waits in ungated lanes are reconsidered by dispatcher readiness maintenance (including after local midnight); ordinary gated lanes still require admission, and agent/planner admission checks both daily limits. A person's explicit admission remains an override and counts toward `cards_per_day` even when outside the planner budget.

Flow uses the latest scheduled event's `awaiting` record, current review/rework events, and bounded read-only GitHub PR observations (a 30-second wall-clock budget per report, concurrent reads cached per unique PR). Flow observes native non-author GitHub approvals or commit-bound COMMENT reviews starting with `VERDICT: APPROVE` (optionally bold, case-insensitive), the fleet's shared-login review format. The latest explicit verdict supersedes older verdicts from that login; bots, old-head approvals and ordinary prose do not approve. This display signal is not ship authorization: independent reviewer-session custody is still verified by ship. Missing checks or exhausted lookup budget never establish green; absent evidence stays `landing_unclassified`. A merged PR stays in deploy until there is explicit live readback. Deployment systems can persist `metadata.flow_pr: {url: <PR URL>, merged: true, deployed: true, verified: false}` on a task event to report live verification (`test`); this is observation only, not authority to merge or declare success. All three flags are strict booleans and the URL must match the card's PR. The command does not infer installation from merge time.

`hermes kanban lanes` (default `show`) prints target against actual share of run-hours over `--hours` (default 6), running, ready, held and open cards per lane; on a synced board it reads the fleet replica unless `--local`. `lanes flow --json [--window 1h|6h|24h|7d]` lists every open card with step, reason and source plus per-lane done/run-hours/cost and today's cap usage. `held` and `unclassified` list cards; `assign <id> <lane>`, `admit <id>`, `pin <id> [--off]` and `set <lane> [--share N] [--admit-per-day N] [--cards-per-day N] [--usd-per-day N] [--pause|--resume]` (always written to the installation-root config) are the controls; a person's `admit` does not spend the daily budget (an agent's does); `promote` and `unblock` also release a held card and are not budgeted; `explain <id>` says which lane a card is in and why it waits. Unset `cards_per_day` / `usd_per_day` means unlimited. Over the cap, new cards wait at the gate with reason `limit:cards_per_day` or `limit:usd_per_day`; both reset at local midnight.

`hermes kanban lanes apply <file|->` applies a planner's JSON action list. Each action is validated on its own and refused with a reason when it breaks a limit: `admit` (at most `admit_per_day` per local day, never a `never_admit` title), `merge` (a held card into an open card of the same lane; it stays held), `reprioritize` (0 to 1000, never a pinned card), `relane` (only a card placed by inheritance or by default, never into or out of a gated lane) and `note`. Every action except `note` needs a one-line `reason`; one plan applies at most 50 actions.

The stuck detector distinguishes intentional `llm_pacing` waits and `worker_load_pressure` from unexplained zero-spawn ticks. Expected waits log `kanban dispatch held` at INFO, with provider running/limit and the next real reset (or the condition rechecked next tick); foreign `labels_mismatch` rows do not turn a fully explained local wait into a stall. A refresh deadline alone cannot justify a pacing hold: an idle zero-limit pool without a future reset remains actionable. Within one dispatcher tenure, a continuously observed card waiting for 15 minutes (or 15 minutes beyond its first real reset) emits `kanban admission prolonged` at WARNING, even if other cards start. This reports lack of admission for that card, not lack of progress by running workers. Warnings repeat at most every five minutes; a new dispatcher tenure starts a new observation window.

To see why a ready card is not spawning, run `hermes kanban dispatch --dry-run` — the output lists `Guarded (<reason>): <task id>` per held card (and `respawn_guarded`, `rate_limited`, `skipped_locked`, `memory_pressure` with `--json`). The gateway's and the standalone daemon's "dispatcher stuck" warning also names what the last tick held back, e.g. `Last tick held back: active_pr=1`.

`recent_success` and `active_pr` hold the **ready** lane only — they are the inputs to a review handoff, not signals against one. To have a reviewer, closer or other recovery profile pick up a card whose PR is already open, either move it to the review lane with `hermes kanban request-review <id>` (accepted from `ready` as well as `running`; the review-lane spawn is not subject to either guard) or hand the ready card to that profile with `hermes kanban assign <id> <profile>`: a handoff recorded *after* the PR comment — an operator reassign, a reviewer's changes-requested verdict, or a review reopen — lifts `active_pr` for the profile now named on the card, because that PR is exactly what it must work on. Only a change to a *different* profile counts: re-assigning the same profile, unassigning, or the dispatcher's own `kanban.default_assignee` fill-in does not lift the guard, so the assignee that opened the PR is still not re-spawned against it after a crash, reclaim or no-op reassign, and a newer PR comment posted after the handoff guards again. A deliberate re-queue after a success (drag `done→ready`, `unblock`, re-promotion) also lifts `recent_success`, so a manual re-run is never silently held for the whole window.

### Plugin start gate (`kanban_start_gate` hook)

Right before claiming a card the dispatcher asks the `kanban_start_gate` plugin hook (kwargs `task_id`, `lane`, `assignee`, `board`). A callback that returns `{"hold": True, "reason": "<word>"}` keeps the card `ready` and unclaimed for that tick; the tick records it under `respawn_guarded` with reason `start_gate:<word>`, and `hermes kanban dispatch --dry-run` lists it as `Guarded (start_gate:<word>)`. The card is asked again on the next tick and starts as soon as no callback holds it. The hold is recorded as a `dispatch_skipped` skip (reason `start_gate`), so wait notices and `hermes kanban diagnostics` (`held_in_ready`) name it; it is not a respawn-guard hold, so nothing is counted against the card and it is never auto-blocked. The dispatcher-stuck detector treats a gated queue as an expected hold, and a machine whose gate holds imports no new foreign cards at pickup. No subscriber, a failing callback, a plugin-discovery fault or any other return starts the card. Callbacks run inline in the tick for every candidate card and must answer from a cached read. The hermes-mods throughput plugin uses it to stop starting cards while the fleet's token rate is over the token throttle's cap, so worker slots are not filled with runs that only wait for model calls.

### LLM quota pacing (`kanban.llm_pacing`)

Before claiming a ready or review card, the dispatcher checks the OAuth pool of the provider the card's next run would use (for a `class:*` selector, every provider the class may route to; the card waits only when all of them hold it). It reads each account's account-wide usage windows (`five_hour` and `seven_day` from `/api/oauth/usage`; Codex `Session`/`Weekly`) at most every `read_interval_seconds`, measures each window's burn rate from its recent samples, and compares the pool's burn with the rate that would bring every account with headroom to `target_percent` exactly at its window's reset. An account whose window has not started yet (the endpoint reports `null` for an idle `five_hour`) counts as a fresh full window; an account read successfully with both windows `null` (idle or brand-new) counts as two. A failed usage read adds no headroom.

- **open** / **paced**: the pool is on or behind pace / ahead of pace. Neither holds a start: how many cards run is decided by worker slots, and the token rate by a token throttle (the hermes-mods throughput plugin, not part of core). The verdict is stored and shown only. The per-host start limit (`pace_starts`, `deferrable_share`) has been removed.
- **held**: every readable account is past its reserve (`reserve_five_hour_percent` / `reserve_seven_day_percent`), so no Kanban claim starts until the earliest reset frees one; the remaining headroom is left to chats, cron and notices.

A start waits only while its pool is **at reserve** (`v2.pools.<provider>.at_reserve` in `llm_pacing.json`, written fresh by a dispatcher on the same home; a missing or stale `v2` holds nothing). A held card stays `ready` and unclaimed. The dispatcher records an `llm_paced` event (providers, state, limit 0) and a `dispatch_skipped` row with reason `llm_paced`. Queue position, the chat-wait and parent-unclaimed notices, and `hermes kanban diagnostics` (`held_in_ready`) name the hold instead of "skip reason unrecorded". Raising the card's priority does not lift it. If no account can be read, the governor fails open, and mandatory model pins and ungoverned providers always start. Only Kanban dispatch consults it.

A **held** pool whose reset is more than `reroute_after_s` away (or unknown) moves the route instead of holding the card for days: a `review_unpinned` reviewer, or a non-mandatory concrete pin runs on another eligible route of the same routing class whose provider is neither held nor walled (the same eligibility as the quota-wall reroutes). The route reason is `review_unpinned+paced:<provider>` or `pinned+paced:<provider>`; the card row never changes, and the governor then judges the new provider as usual. Queue notes and stall notices name the move (`next run rerouted: pinned+paced:openai-codex -> anthropic/<model>`). A **paced** pool never moves a route, and mandatory pins, `class:*` selectors and `require_model: true` cards keep theirs. The decision reads only the pace stored in `llm_pacing.json`; it makes no usage read. Unpinned default-tier cards are placed by ratio routing (below) instead; one that still resolves onto a held pool (ratio routing off or failing open) is moved at claim time by the governor to a same-class pool with room (`default+paced:<provider>` in its `run_route` receipt), or held.

**Ratio routing.** An unpinned default-tier card (no model or provider override, not a review run, no `require_model: true` line) starts on the governed pool furthest below its planned budget share instead of always on `kanban.default_model`. The pools are the providers of the `model_routing.routes` class level of `kanban.default_model`: the level a `class:<level>` default names, else the one level that lists the concrete default model (registry order; each runs `ratio_route_models.<pool>` when that is one of its routes in the level, else its first route). A pick is always a concrete model and provider, also under a class default. A pool is eligible when its `v2` entry, read within the last 20 minutes (its own `read_at`), has `ratio` above 0, is not `at_reserve` and has a known `rate_tokens_per_s`, its provider is enabled, this host holds a live credential that can serve the model, and no provider wall stands. Targets are the eligible pools' ratios renormalized to 1; the share is each pool's part of this host's default-tier starts on the board over the last six hours (`run_route` receipts). The pool with the largest target minus share wins (ties: registry order). The route reason is `default+ratio:<pool>` (a later wall, episode or pace move appends its own suffix), and the `model_tier` and `run_route` receipts carry a `ratio` block (`pool`, `model`, `target`, `share`, `deficit`, `starts`). Queue notes name the pick (`next run on anthropic (budget share): anthropic/<model>`). It keeps today's route when `ratio_routing` is `false`, the `v2` document is missing, malformed or older than 20 minutes, no pool is eligible, a concrete default model is in no class level (or in two), or the choice fails; under a class default the kept route is the `class:<level>` selector itself. Start counts are read once per dispatcher tick and each start in the tick is added, so a card's preview, governor check and launch agree and the next card sees the previous start.

| Config key | Default | What it does |
|------------|---------|--------------|
| `kanban.llm_pacing.enabled` | `true` | `false` restores ungoverned claims. |
| `kanban.llm_pacing.target_percent` | `95` | Usage each window may reach by its reset. |
| `kanban.llm_pacing.reserve_five_hour_percent` | `80` | Past this, an account's five-hour window counts as reserved for non-Kanban callers. |
| `kanban.llm_pacing.reserve_seven_day_percent` | `85` | The same for the weekly window. |
| `kanban.llm_pacing.providers` | `["anthropic", "openai-codex", "xai-oauth"]` | Governed providers; cards on other providers always start. |
| `kanban.llm_pacing.read_interval_seconds` | `300` | Minimum seconds between usage reads per Hermes home (shared through `<kanban home>/llm_pacing.json`). |
| `kanban.llm_pacing.rate_window_seconds` | `3600` | Lookback for burn rates. |
| `kanban.llm_pacing.min_rate_span_seconds` | `600` | Shortest sample span a burn slope is measured over. |
| `kanban.llm_pacing.margin_seconds` | `10800` | Capacity model: each account is planned to reach its reserve this long before its weekly reset. |
| `kanban.llm_pacing.history_days` | `8` | Retention of `<kanban home>/llm_pacing_history.sqlite`. |
| `kanban.llm_pacing.tpp_min_delta_percent` | `5` | A tokens-per-percent estimate needs at least this summed percent delta (and two hours). |
| `kanban.llm_pacing.tpp_window_seconds` | `86400` | Lookback of the tokens-per-percent estimate. |
| `kanban.llm_pacing.tpp_half_life_seconds` | `43200` | Half-life of the estimate's smoothing. |
| `kanban.llm_pacing.tokens_per_percent` | `{}` | Seeds, `{provider: {seven_day: N, five_hour: N}}`, used until a rate is measured. |
| `kanban.llm_pacing.budget_publish` | `false` | This host publishes the capacity budget to the token throttle (read by the hermes-mods throughput plugin; set on the hub only). |
| `kanban.llm_pacing.reroute_after_s` | `3600` | A held pool whose reset is further away than this moves `review_unpinned` and non-mandatory pinned routes to an unheld same-class peer. |
| `kanban.llm_pacing.ratio_routing` | `true` | Start unpinned default-tier cards on the eligible pool furthest below its `v2` budget share (`false`: always `kanban.default_model`). |
| `kanban.llm_pacing.ratio_route_models` | `{}` | `{pool: model}`: the route each pool runs for ratio routing; must be one of that pool's routes in the default model's class level, else its first route is used. |

#### Capacity model (`llm_pacing.json` `v2`)

Each refresh also plans how many tokens per second every governed pool can spend so that each account reaches its reserve `margin_seconds` before its weekly reset. Core only computes and stores the plan; the hermes-mods throughput plugin enforces it as a token-rate cap. `llm_pacing.json` keeps its per-provider keys (`read_at`, `pace`, `samples`) for one release and adds `"version": 2` and `"v2"`:

- `computed_at`, `margin_s`, `global_rate_tokens_per_s` (the sum over pools whose rate is known), `events` (the newest 50) and `curve.<provider>` (`[t, capacity, planned_spend]`: cumulative weekly capacity with a step at every reset, and the current plan's spend).
- `pools.<provider>`: `read_at`, `accounts` (`id` is a truncated SHA-256 of the account label, never the label), `tpp_7d` / `tpp_5h` (tokens per percent) with `tpp_source` / `tpp_5h_source` (`measured`, `seed` or `unknown`), `plan_size_assumed_equal` (one exchange rate per pool: tokens cannot be attributed to accounts), `remaining_tokens`, `exhaust_plan_at`, `rate_tokens_per_s` (`null` while the exchange rate is unknown: a throttle passes the pool), `rate_cap_5h`, `at_reserve`, `until` and `ratio` (the pool's share of the global rate).

An account's rate is its weekly remaining (`(reserve - used) x tpp`) over the time to `reset - margin` (at least 600 s), capped by its five-hour remaining over the time to that reset (at least 60 s; Codex and xAI have no five-hour window, so no cap). The exchange rate is the fleet's token delta on the provider (each machine's `tokens_by_provider`, else its `tokens_by_model` by model prefix) over the accounts' summed percent delta across consecutive reads in the last `tpp_window_seconds`, skipping intervals that cross a reset or an event; it is smoothed and clamped to 0.5x-2x per update. Anthropic's model-scoped weekly windows (`seven_day_opus`, `seven_day_sonnet`) are kept in the history only and never change capacity. Events: `account_added` (not read in 24 h), `account_removed` (absent from 3 successful reads), `reset` and `reset_early`.

### Dependency-review repair continuation

An owning worker can link independent review cards as parents with `kanban_link`,
then call `kanban_block(kind="dependency")`. If its comments already identify the
PR, the authenticated park captures the current run, parent IDs, and PR URLs.
After all parents finish, the `active_pr` guard permits one continuation of
those same PRs. A repair requires each review's latest claimed completed run
to carry:

```json
{"verdict": "changes_required", "pr": "https://github.com/owner/repo/pull/123", "reviewed_sha": "0123456789abcdef0123456789abcdef01234567"}
```

An exact-head approval is the landing continuation, not a second implementation
and not merge or CI approval. The run metadata must use `approve` / `approved`
/ `APPROVE` and a full 40-character `head` or `reviewed_sha`. If it names `pr`,
that URL is the only one authorized; if it does not, the checkpoint's captured
URLs are the binding. A short head is not exact.

The evidence must come from the review card's latest, actually claimed completed
run, finishing at or after the park. Reviewer comments or guard notices repeating the same
PR after the park do not invalidate it. A new PR, a review that names a different
PR, a changed parent set, an unclaimed completion, an unrelated dependency, a
non-exact approval, or malformed evidence does not authorize retry.
The next implementation claim consumes the checkpoint: a subsequent crash/reclaim
stays guarded. Same-owner assignment remains a no-op for this guard.

No new recovery tool is needed for this worker-owned path. Existing legacy parks
without the authenticated checkpoint fail closed; `kanban_continue` only accepts
an actively owned running task and cannot recover an already-held ready card.
Such legacy holds still require an explicit operator handoff. Do not manufacture
comments, edit database rows, or use timed scheduling as a review authorization.

### Drag-to-delete and bulk delete (dashboard)

The dashboard exposes a **trash drop zone** on the kanban page — drag any card into it to delete the task (cascades through `task_events`, child links, and subscriptions). A confirmation prompt protects against accidents. Bulk delete is also reachable via `DELETE /api/plugins/kanban/tasks` with a JSON body `{"ids": ["t_abc", "t_def", ...]}`.

### Worker visibility endpoints

The dashboard plugin API now exposes these read-only endpoints (plus a run-control verb) for external monitors:

| Endpoint | Returns |
|----------|---------|
| `GET /api/plugins/kanban/workers/active` | Currently spawned workers with PID, profile, task id, started-at, last heartbeat |
| `GET /api/plugins/kanban/runs/{id}` | Single-run detail — task id, status, started/ended, exit code, log path |
| `POST /api/plugins/kanban/runs/{run_id}/terminate` | Terminate a reclaimable run — stops the worker and frees the task for re-dispatch |
| `GET /api/plugins/kanban/inspect` | Combined dispatcher snapshot — backlog, in-progress count vs. `max_in_progress`, recent events |

All of these are gated by the same dashboard plugin auth as the rest of the kanban plugin API.

### Kanban Swarm topology helper

`hermes kanban swarm` creates a durable **Kanban Swarm v1** graph in one shot: a completed root/blackboard card, N parallel worker cards, a verifier card gated on all workers, and a synthesizer card gated on the verifier. Shared swarm context (the "blackboard") is stored as structured JSON comments on the root card so any worker can read it.

```bash
hermes kanban swarm "Design a multi-region failover plan" \
  --workers researcher,architect,sre \
  --verifier reviewer --synthesizer writer
```

The resulting graph is committed atomically: dispatchers and dashboard readers see either no new swarm or the complete topology, never a partially linked root/worker/verifier graph. It then dispatches normally — workers run in parallel, the verifier wakes after they all finish, and the synthesizer wakes after the verifier marks the work clean.

## `/kanban` slash command {#kanban-slash-command}

Every `hermes kanban <action>` verb is also reachable as `/kanban <action>` — from inside an interactive `hermes chat` session **and** from any gateway platform (Telegram, Discord, Slack, WhatsApp, Signal, Matrix, Mattermost, email, SMS). Both surfaces call the exact same `hermes_cli.kanban.run_slash()` entry point that reuses the `hermes kanban` argparse tree, so the argument surface, flags, and output format are identical across CLI, `/kanban`, and `hermes kanban`. You don't have to leave the chat to drive the board.

```
/kanban list
/kanban show t_abcd
/kanban create "write launch post" --assignee writer --parent t_research
/kanban comment t_abcd "looks good, ship it"
/kanban unblock t_abcd
/kanban dispatch --max 3
/kanban specify t_abcd                  # flesh out a triage one-liner into a real spec
/kanban specify --all --tenant engineering  # sweep every triage task in one tenant
```

Quote multi-word arguments the same way you would on a shell — `run_slash` parses the rest of the line with `shlex.split`, so `"..."` and `'...'` both work.

### Mid-run usage: `/kanban` bypasses the running-agent guard

The gateway normally queues slash commands and user messages while an agent is still thinking — that's what stops you from accidentally starting a second turn while the first is in flight. **`/kanban` is explicitly exempted from this guard.** The board lives in `~/.hermes/kanban.db`, not in the running agent's state, so reads (`list`, `show`, `context`, `tail`, `watch`, `stats`, `runs`) and writes (`comment`, `unblock`, `block`, `assign`, `archive`, `create`, `link`, …) all go through immediately, even mid-turn.

This is the whole point of the separation:

- A worker blocks waiting on a peer → you send `/kanban unblock t_abcd` from your phone and the dispatcher picks the peer up on its next tick. The blocked worker isn't interrupted — it just stops being blocked.
- You spot a card that needs human context → `/kanban comment t_xyz "use the 2026 schema, not 2025"` lands on the task thread and the *next* run of that task will read it in `kanban_show()`.
- You want to know what your fleet is doing without stopping the orchestrator → `/kanban list --mine` or `/kanban stats` inspects the board without touching your main conversation.

### Auto-subscribe on `/kanban create` (gateway only)

When you create a task from the gateway with `/kanban create "…"`, the originating chat (platform + chat id + thread id) is automatically subscribed to that task's terminal events (`completed`, `blocked`, `gave_up`, `crashed`, `timed_out`). You'll get one message back per terminal event — including the first line of the worker's result summary on `completed` — without having to poll or remember the task id.

```
you> /kanban create "transcribe today's podcast" --assignee transcriber
bot> Created t_9fc1a3  (ready, assignee=transcriber)
     (subscribed — you'll be notified when t_9fc1a3 completes or blocks)

… ~8 minutes later …

bot> ✓ t_9fc1a3 completed by transcriber
     transcribed 42 minutes, saved to podcast/2026-05-04.md
```

Subscriptions survive a task reaching `done` — completion is reversible (a reviewer or controller can reopen a done task), so the origin session keeps getting notified through reopen cycles. They auto-remove on `archived` (the irreversible end state). On boards that never archive, a GC sweep purges subscriptions for tasks that have sat in `done` or `blocked` with no new activity for `kanban.done_sub_retention_days` days (default 30; set 0 to disable), so stale rows don't accumulate forever. If you script a create with `--json` (machine output) the auto-subscribe is skipped — the assumption is that scripted callers want to manage subscriptions explicitly via `/kanban notify-subscribe`.

Dispatcher workers creating tasks through `kanban_create` or `hermes kanban create`
copy the owning task's durable notification subscriptions even without `parents`
dependency links. Destinations, route anchors, and delivery modes are preserved;
a passive subscription is not upgraded to a wake by auto-subscribe. This copies
existing subscriptions independently of `auto_subscribe_on_create`, which controls
adding the current conversation as a new destination. No destination is invented
for a bare CLI session or a worker whose owning task has no subscriptions.

For `kanban_create`, session lineage resolves in this order: explicit `session_id`,
the owning worker task's durable session, request-scoped API origin, then the
current session. A candidate id is stamped only when it has a row in the active
profile's `state.db` `sessions` table; an id that resolves nowhere (a session whose
row was never written, or an id inherited from another process) leaves
`session_id` NULL rather than pointing at a session no database can resolve.
Built-in decomposition also inherits its root's durable session. Session lineage is not itself a notification destination: changing
`session_id` does not replace existing subscriptions; use `notify-subscribe` and
`notify-unsubscribe` to change where events are delivered.

A chat-originated auto-subscribe is created in `notify+wake` mode: on a terminal event the destination agent both receives the passive message **and** takes a real turn, so it can read the board context and reply in its own voice. See [Delivery modes](#delivery-modes) below.

### Output truncation in messaging

Gateway platforms have practical message-length caps. If `/kanban list`, `/kanban show`, or `/kanban tail` produce more than ~3800 characters of output, the response is truncated with a `… (truncated; use \`hermes kanban …\` in your terminal for full output)` footer. The CLI surface has no such cap.

### Autocomplete

In the interactive CLI, typing `/kanban ` and hitting Tab cycles through the built-in subcommand list (`list`, `ls`, `show`, `create`, `assign`, `link`, `unlink`, `claim`, `comment`, `complete`, `block`, `unblock`, `archive`, `tail`, `dispatch`, `context`, `init`, `gc`). The remaining verbs listed in the CLI reference above (`watch`, `stats`, `runs`, `log`, `assignees`, `heartbeat`, `notify-subscribe`, `notify-list`, `notify-unsubscribe`, `daemon`) also work — they're just not in the autocomplete hint list yet.

## Collaboration patterns

The board supports these eight patterns without any new primitives:

| Pattern | Shape | Example |
|---|---|---|
| **P1 Fan-out** | N siblings, same role | "research 5 angles in parallel" |
| **P2 Pipeline** | role chain: scout → editor → writer | daily brief assembly |
| **P3 Voting / quorum** | N siblings + 1 aggregator | 3 researchers → 1 reviewer picks |
| **P4 Long-running journal** | same profile + shared dir + cron | Obsidian vault |
| **P5 Human-in-the-loop** | worker blocks → user comments → unblock | ambiguous decisions |
| **P6 `@mention`** | inline routing from prose | `@reviewer look at this` |
| **P7 Thread-scoped workspace** | `/kanban here` in a thread | per-project gateway threads |
| **P8 Fleet farming** | one profile, N subjects | 50 social accounts |
| **P9 Triage specifier** | rough idea → `triage` → `hermes kanban specify` expands body → `todo` | "turn this one-liner into a spec'd task" |

## Handing context to follow-up cards (the parent link)

A parent link is not just a scheduling gate — it is the context handoff channel from a **completed** card to a new one. When you create a card with `--parent <done-card-id>`, two things happen:

1. **It's immediately eligible.** `create_task` sets status by parent state: a child whose parents are all `done` is created directly in `ready` — no waiting, no manual promotion. (Children of still-open parents sit in `todo` until `recompute_ready` promotes them when the last parent finishes.)
2. **The parent's handoff rides along.** The worker context assembled for the child (`build_worker_context`, what `kanban_show()` returns) contains a `## Parent task results` section with each parent's completion `summary` and `metadata`, verbatim:

```
## Parent task results
### t_77c26979 (completed just now)
Added exponential backoff with jitter to the retry helper.
_metadata_: `{"changed_files": ["hermes_cli/retry.py", "tests/test_retry.py"], "decisions": ["capped backoff at 60s", "jitter = full"]}`
```

This is why the pattern for follow-up work on a finished card is **a new child card, not reopening the done card**. Completed cards are immutable history — their context flows forward through the parent link. Same-card rework (retry loops on a failing card) is a different mechanism: prior attempts on the *same* card surface as "prior attempts" in that card's own context.

A worktree or branch alone is not a substitute: repo state tells the follow-up worker *what* the code looks like, but not *why* — the decisions, tests run, and files touched live in the parent's structured handoff, not in git. Evidence that didn't exist when the parent completed (e.g. a CI log that failed later) belongs in the new card's **body**.

```bash
# Implementation card t_impl is done. CI fails two hours later.
hermes kanban create "Fix CI failure from t_impl: test_retry flakes on 3.11" \
    --assignee coder \
    --parent t_impl \
    --body "$(cat <<'EOF'
CI run #4812 failed after t_impl merged.
Log excerpt: FAILED tests/test_retry.py::test_backoff_jitter - TimeoutError
Acceptance: tests/test_retry.py green on 3.11 and 3.12 in CI.
Use a fresh worktree/branch; do not force-push the original branch.
EOF
)"
```

The remediation worker spawns with the original card's summary and metadata (changed files, decisions) already in context, plus the fresh evidence you put in the body.

### Reconciling colliding worker branches

In engineering pipelines (P1/P2 with worktrees), two workers' branches can
conflict when merged. Don't let either worker self-adjudicate — the colliding
agent lacks its peer's context and reliably overwrites the other side or
abandons its own. Instead, create a reconciliation card assigned to a **third,
neutral profile** with **both** conflicted cards linked as parents: the parent
links carry both sides' completion summaries into the reconciler's context, so
it receives both diffs *and* both intents. The bundled
[`agent-merge-conflict-arbiter` optional skill](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/autonomous-ai-agents/agent-merge-conflict-arbiter/SKILL.md)
gives that worker the full procedure: classify each conflicted hunk, resolve
impartially, verify, and hand back a summary naming every decision.

### Collision hotspots in parallel campaigns

In wide campaigns some files become collision magnets: many workers each add a
little to the same file, nobody owns keeping it small, and it turns into the
site of constant merge conflicts. The mitigation is a comment convention, not a
new primitive. A worker that notices its diff keeps colliding with siblings in
one file — or that a file it touches keeps appearing in other cards' recent
comments — should not silently pile on. Instead it leaves a comment on its own
card with a recognizable prefix:

```
hotspot: hermes_cli/kanban_db.py — third conflicting edit to the dispatch loop this wave
```

and repeats the flag in its completion `metadata`. Orchestrators (or humans
reviewing the board) who see **two or more `hotspot:` comments naming the same
path** should create a dedicated refactor/decomposition card for that file
**before** queuing more work that touches it — splitting the magnet file is
cheaper than reconciling every future collision it would cause. For conflicts
that have *already* happened, use the reconciliation-card pattern above with
the `agent-merge-conflict-arbiter` optional skill; hotspot flagging is the upstream fix that keeps
the reconciler from becoming a standing lane.

## Multi-tenant usage

When one specialist fleet serves multiple businesses, tag each task with a tenant:

```bash
hermes kanban create "monthly report" \
    --assignee researcher \
    --tenant business-a \
    --workspace dir:~/tenants/business-a/data/
```

Workers receive `$HERMES_TENANT` and namespace their memory writes by prefix. The board, the dispatcher, and the profile definitions are all shared; only the data is scoped.

## Desktop notifications

The Desktop app's Kanban plugin surfaces the same terminal events natively — no gateway platform required. While the Kanban board's live event socket is connected, each `completed`, `blocked`, `gave_up`, `crashed`, `timed_out`, or routed-to-triage (`block_loop_detected`) event raises an in-app toast with the worker's handoff (summary, block reason, or error) and an "Open Kanban" action. When you're away from the Hermes window, the same event also fires a native OS notification (gated by **Settings ▸ Notifications ▸ Plugin notifications**), so a task hitting a blocker while you're in another app still reaches you.

Coverage window: desktop notifications ride the live event stream, so they fire only while the app is running with the Kanban plugin enabled. Events that land while the app is closed are not replayed as notifications on next launch — use a gateway subscription (below) for delivery that must survive the app being closed.

## Gateway notifications

When you run `/kanban create …` from the gateway (Telegram, Discord, Slack, etc.), the originating chat is automatically subscribed to the new task. The gateway's background notifier polls `task_events` every few seconds and delivers one message per terminal event (`completed`, `blocked`, `gave_up`, `crashed`, `timed_out`) to that chat. Completed tasks also send the first line of the worker's `--result` so you see the outcome without having to `/kanban show`.

You can manage subscriptions explicitly from the CLI — useful when a script / cron job wants to notify a chat it didn't originate from:

```bash
hermes kanban notify-subscribe t_abcd \
    --platform telegram --chat-id 12345678 --thread-id 7 \
    --chat-type group --delivery-mode notify+wake
hermes kanban notify-list
hermes kanban notify-unsubscribe t_abcd \
    --platform telegram --chat-id 12345678 --thread-id 7
```

A subscription removes itself automatically once the task reaches `done` or `archived`; no cleanup needed.

When an agent runs `notify-subscribe` from inside a gateway chat (through its terminal tool) and the
target is that same chat (and topic), a missing `--user-id` is filled with the sender of the turn. In groups with
`group_sessions_per_user` (the default), that keeps the wake in the participant's own session instead
of a second, shared one. Subscriptions to any other chat or topic never take the caller's id, and
neither does one given an explicit `--user-id-alt`.

**Discord threads under `profile_routes`:** when the gateway multiplexes profiles and routes a *channel*
to a profile, a subscription created from the CLI for a *thread* in that channel needs the thread's route
anchors, or the notifier cannot match it to any route and skips it (logged once at WARNING). Pass them
explicitly — `--parent-chat-id <channel id>` and, for Discord, `--guild-id <guild id>`:

```bash
hermes kanban notify-subscribe t_abcd \
    --platform discord --chat-id <thread id> --thread-id <thread id> --chat-type thread \
    --parent-chat-id <channel id> --guild-id <guild id> --delivery-mode notify+wake
```

Subscriptions created from inside the chat (`/kanban create`, `kanban_create`) record these anchors automatically.
A Telegram forum topic, or a Slack DM thread, lives inside its own chat (`chat_id` is the group or DM, not
the thread), so it needs no `--parent-chat-id`: the notifier matches it against the route that names that chat.
That needs a stated `--chat-type` (`dm`, `group` or `channel`; the gateway records it for you). A row without one
may store the thread's own id as `chat_id`, so it still needs `--parent-chat-id` to reach a channel route.

### Delivery modes

`--delivery-mode` controls **how** the notifier reacts to a terminal event. Every subscription is in one of three modes (`notify` is the default and the original behavior):

| Mode | Passive message | Wakes the agent | Use it when |
|------|-----------------|-----------------|-------------|
| `notify` | yes | no | You just want a heads-up message in the chat (default). |
| `notify+wake` | yes | yes | You also want the destination agent to take a real turn — read the board context and reply in its own voice. Chat-originated auto-subscribes use this. |
| `wake` | no | yes | You only want the agent to act on the event, with no separate ping. |

For `notify+wake`, delivery completes only once the wake is admitted to the adapter's turn queue as well as the passive ping being sent. Missing handlers, rejected routes, and full queues are retried on later notifier ticks without expiring the subscription. Sent pings are checkpointed separately in SQLite, so a rejected wake does not repeat an already checkpointed ping. `notify` remains passive and never starts a turn. Admission is not a guarantee of model execution or a successful reply; normal turn gates still apply. This is not exactly-once delivery: a process crash between a send and its checkpoint can repeat the ping, and the existing claim-before-delivery cursor is not a crash-recoverable queue.

A "wake" forges a synthetic inbound message to the destination gateway agent so it takes a normal turn (reads the comment + result, reasons, replies) instead of getting a one-line passive notification. It only fires when the notifier runs inside a live gateway process; otherwise a `notify+wake` subscription still delivers its passive message, while a `wake`-only subscription does nothing in that process.

**Which events wake.** The ones that return a task outcome or require orchestration attention: `completed`, `blocked`, `gave_up`, `crashed`, `timed_out`, `review_requested` (a worker finished the implementation and handed off via `kanban_request_review`) and `block_loop_detected` (the task was routed to `triage` after repeated blocks). `status`, `archived` and `unblocked` are delivered but never wake — they are bookkeeping transitions, not attention signals. When a `completed` or `review_requested` event carries a summary, that handoff rides the wake turn, so the woken agent sees what the worker actually did.

`--chat-type` (`dm` | `group` | `channel` | `thread`) records the originating chat's type so a woken turn resolves the operator's **real** session: `build_session_key` keys groups, channels, and threads differently from DMs, so an inaccurate `chat_type` would route the wake into a separate, context-less session. The `/kanban` auto-subscribe and slash-command paths capture this automatically — you only set it by hand when subscribing a chat from a script or cron. Omit it to leave an existing subscription unchanged (new subscriptions default to `dm`).

### Multi-profile setups: delivery is profile-owned

In a one-gateway-per-profile deployment (one dispatcher, separate gateway
processes for `writer`, `admin`, etc. — see the [multi-gateway
guide](./kanban-multi-gateway.md)),
dispatch and delivery have separate owners:

- **Dispatch stays single-owner.** Exactly one gateway keeps
  `kanban.dispatch_in_gateway: true` and runs the dispatcher; every other
  gateway sets it to `false`.
- **Notification delivery is profile-owned.** Every gateway — including
  non-dispatch ones — runs the notifier and polls only subscriptions stamped
  with a profile whose platform adapters it hosts. A task created from the
  `writer` profile's Telegram gets its `completed`/`blocked` message delivered
  by the `writer` gateway, even though the `default` gateway did the
  dispatching.
- **Multiplex profiles pinned by `gateway.profile_routes`** can use the primary
  adapter when the subscription's persisted platform, chat, thread, scope and
  parent-channel anchors resolve to that exact served profile through
  `gateway.profile_routes` and the profile holds no adapter of its own for the
  subscription's platform. A connected secondary adapter for that platform
  remains authoritative; adapters the profile runs on *other* platforms do not
  block delivery (the shared bot is the only credential serving the pinned
  chat, for inbound turns and notifications alike). Unmatched, reassigned,
  disabled or ambiguous routes remain undelivered and retryable. Old rows
  missing required routing anchors are not guessed into a profile. Wake turns keep
  the destination profile's runtime scope and the authorized transport.
- **Stateless (`api_server`) subscriptions** carry a raw session id, which no
  `profile_routes` entry can anchor (there is no chat/thread/guild discriminator
  on a session). A served profile is authorized exactly when the subscription's
  session lives in that profile's own `state.db` — the shared listener mirrors
  `/p/<profile>/`, so the session store is the ownership proof, never the
  platform. The wake then runs in that profile's runtime scope and resumes that
  exact session in-process, so no second listener and no secondary
  `API_SERVER_KEY` is involved. A session that is unknown, stamped for another
  profile, or owned by an unserved profile stays undelivered and retryable, and
  the default profile's own `api_server` subscriptions are unaffected.
- **Legacy subscriptions** created before profile stamping (no
  `notifier_profile` on the row) are delivered only by the gateway that holds
  the actual dispatcher singleton lock, so two gateways never race for them.

Duplicate delivery across gateways is prevented by the atomic per-event claim
in the board DB. No relays, credential sharing, or extra dispatchers are
needed — each profile gateway simply delivers through its own adapters.

## Runs — one row per attempt

Canonical workers execute through the assigned profile's gateway owner. The local
transport carries the dispatcher's exact board database path, including custom
paths and shared boards assigned to another profile; it does not create a separate
transcript writer. The owner validates the task/run/claim and freezes its policy.

If the owner or managed worker disappears with an **unknown** admission while the
interpreter the owner bound to the attempt (its `worker_bound` event) may still be
running, the card keeps its run and claim: expired claims, stale heartbeats and
runtime limits do not reexecute that ambiguous attempt, and an unavailable owner
ledger also pauses reclaim. Once that bound interpreter is itself dead, nothing can
still be running the attempt, so the crash sweep reclaims it like any dead worker:
the run closes as crashed and the card returns to `ready` (or `review`) under the
normal crash budget, which can re-execute effects the lost attempt already had.
A run that never bound an interpreter is released the same way once its owner has
settled the admission (a lost card attempt's admission settles `interrupted`):
when every worker pid recorded for the run is dead, or, with none recorded, once
its claim has expired, the run closes as crashed (reclaimed if its claim had already
lapsed) and the card returns to `ready`
under the same crash budget. Resolving an unknown admission explicitly
(`prompt.resolve_unknown` with its admission ID and execution generation) settles
it the same way, so it can lead to another attempt. To avoid that, inspect the
effects and complete or block the card first; unblock a blocked card only when a
new attempt is intended. Confirmed timeouts and provider rate-limit results retain their ordinary
retry/failure-accounting behavior.

A task is a logical unit of work; a **run** is one attempt to execute it. When the dispatcher claims a ready task it creates a row in `task_runs` and points `tasks.current_run_id` at it. When that attempt ends — completed, blocked, crashed, timed out, spawn-failed, reclaimed — the run row closes with an `outcome` and the task's pointer clears. A task that's been attempted three times has three `task_runs` rows.

Why two tables instead of just mutating the task: you need **full attempt history** for real-world postmortems ("the second reviewer attempt got to approve, the third merged"), and you need a clean place to hang per-attempt metadata — which files changed, which tests ran, which findings a reviewer noted. Those are run facts, not task facts.

Runs are also where **structured handoff** lives. When a worker completes a task (via `kanban_complete(...)`) it can pass:

- `summary` (tool param) / `--summary` (CLI) — human handoff; goes on the run; downstream children see it in their `build_worker_context`.
- `metadata` (tool param) / `--metadata` (CLI) — free-form JSON dict on the run; children see it serialized alongside the summary.
- `result` (tool param) / `--result` (CLI) — short log line that goes on the task row (legacy field, kept for back-compat).

Downstream children read the most recent completed run's summary + metadata for each parent. Retrying workers read the prior attempts on their own task (outcome, summary, error) so they don't repeat a path that already failed.

```
# What a worker actually does — a tool call, from inside the agent loop:
kanban_complete(
    summary="implemented token bucket, keys on user_id with IP fallback, all tests pass",
    metadata={"changed_files": ["limiter.py", "tests/test_limiter.py"], "tests_run": 14},
    result="rate limiter shipped",
)
```

The same handoff is reachable from the CLI when you (the human) need to close out a task a worker can't — e.g. a task that was abandoned, or one you marked done manually from the dashboard:

```bash
hermes kanban complete t_abcd \
    --result "rate limiter shipped" \
    --summary "implemented token bucket, keys on user_id with IP fallback, all tests pass" \
    --metadata '{"changed_files": ["limiter.py", "tests/test_limiter.py"], "tests_run": 14}'

# Review the attempt history on a retried task:
hermes kanban runs t_abcd
#   #  OUTCOME       PROFILE           ELAPSED  STARTED
#   1  blocked       worker               12s  2026-04-27 14:02
#        → BLOCKED: need decision on rate-limit key
#   2  completed     worker                8m   2026-04-27 15:18
#        → implemented token bucket, keys on user_id with IP fallback
```

Runs are exposed on the dashboard (Run History section in the drawer, one coloured row per attempt) and on the REST API (`GET /api/plugins/kanban/tasks/:id` returns a `runs[]` array). `PATCH /api/plugins/kanban/tasks/:id` with `{status: "done", summary, metadata}` forwards both to the kernel, so the dashboard's "mark done" button is CLI-equivalent. `task_events` rows carry the `run_id` they belong to so the UI can group them by attempt, and the `completed` event embeds the first-line summary in its payload (capped at 400 chars) so gateway notifiers can render structured handoffs without a second SQL round-trip.

**Bulk close caveat.** `hermes kanban complete a b c --summary X` is refused — structured handoff is per-run, so copy-pasting the same summary to N tasks is almost always wrong. Bulk close *without* `--summary` / `--metadata` still works for the common "I finished a pile of admin tasks" case.

**Dependency refusal on complete names the parents.** `kanban_complete` / `hermes kanban complete` / the dashboard's "mark done" and "request review" actions (single and bulk) on a card whose direct parent is not `done`/`archived` (a parent reopened mid-run, or an edge that predates the running-child refusal) reports `unsatisfied parent dependencies: t_… (todo)` instead of the generic "unknown id, stale run, or already terminal" text; the card stays in-flight. `kanban_show` lists the same parents under `unsatisfied_parents`, and `hermes kanban show` / `hermes kanban diagnostics` / the dashboard raise a `running_with_open_parents` warning on a running card in that state. Finish the parent or `hermes kanban unlink <parent> <child>`; there is no force path through the dependency gate.

**Live-claim guard on complete.** A `running` task whose worker holds a live claim is only completed by that worker (`kanban_complete` from inside the run) or by an explicit operator override: `hermes kanban complete <id> --force` and the dashboard's "mark done" action. A claim-less `hermes kanban complete <id>` or an orchestrator session's `kanban_complete` is refused with a pointer to `--force` / `hermes kanban reclaim`, so a second session can no longer close a live worker's run underneath it. Completing `ready`, `blocked` or `review` cards without a claim is unchanged.

**Reclaimed runs from status changes.** If you drag a running task off `running` in the dashboard (back to `ready`, or straight to `todo`), or archive a task that was still running, the in-flight run closes with `outcome='reclaimed'` rather than being orphaned. The `task_runs` row is always in a terminal state when `tasks.current_run_id` is `NULL`, and vice versa — that invariant holds across CLI, dashboard, dispatcher, and notifier.

**Synthetic runs for never-claimed completions.** Completing or blocking a task that was never claimed (e.g. a human closes a `ready` task from the dashboard with a summary, or a CLI user runs `hermes kanban complete <ready-task> --summary X`) would otherwise drop the handoff. Instead the kernel inserts a zero-duration run row (`started_at == ended_at`) carrying the summary / metadata / reason so attempt history stays complete. The `completed` / `blocked` event's `run_id` points at that row.

**Live drawer refresh.** When the dashboard's WebSocket event stream reports new events for the task the user is currently viewing, the drawer reloads itself (via a per-task event counter threaded into its `useEffect` dependency list). Closing and reopening is no longer required to see a run's new row or updated outcome.

### Forward compatibility

Two nullable columns on `tasks` are reserved for v2 workflow routing: `workflow_template_id` (which template this task belongs to) and `current_step_key` (which step in that template is active). The v1 kernel ignores them for routing but lets clients write them, so a v2 release can add the routing machinery without another schema migration.

## Event reference

Every transition appends a row to `task_events`. Each row carries an optional `run_id` so UIs can group events by attempt. Kinds group into three clusters so filtering is easy (`hermes kanban watch --kinds completed,gave_up,timed_out`):

**Lifecycle** (what changed about the task as a logical unit):

| Kind | Payload | When |
|---|---|---|
| `created` | `{assignee, status, parents, tenant}` | Task inserted. `run_id` is `NULL`. |
| `promoted` | — | `todo → ready` because all parents hit `done`. `run_id` is `NULL`. |
| `claimed` | `{lock, expires, run_id}` | Dispatcher atomically claimed a `ready` task for spawn. |
| `completed` | `{result_len, summary?}` | Worker wrote `--result` / `--summary` and task hit `done`. `summary` is the first-line handoff (400-char cap); full version lives on the run row. If `complete_task` is called on a never-claimed task with handoff fields, a zero-duration run is synthesized so `run_id` still points at something. |
| `blocked` | `{reason, kind, recurrences}` | Worker or human flipped the task to `blocked`. `kind` is the typed block reason (`needs_input`, `capability`, `transient`, or `null` for a generic block); `recurrences` is the unblock-loop counter. A `kind=dependency` block with no incomplete parent lands here as `needs_input` (payload adds `requested_kind: dependency`, `rekind_reason: no_open_parent`) because `todo` would only get it re-promoted and respawned on the next dispatch tick. Synthesizes a zero-duration run when called on a never-claimed task with `--reason`. |
| `dependency_wait` | `{reason, kind}` or `{reason: parent_not_done, demoted: true, parent}` | Worker blocked with `kind=dependency` while at least one parent is still open — the task is only waiting on another task, so it routes to `todo` (parent-gated, auto-promoted) instead of `blocked` and no recurrence is counted. No human needed. Also emitted when `link`/`kanban_link` puts a `ready` child under a parent that is not `done`: the child drops back to `todo` and this event records why (the `ready → running` claim re-checks parents, so nothing can run it until the parent completes or the link is removed with `hermes kanban unlink`). |
| `block_loop_detected` | `{reason, kind, recurrences, limit}` | A task was unblocked and re-blocked for the same reason `BLOCK_RECURRENCE_LIMIT` times (default 2). Instead of landing in `blocked` again — where a cron would keep unblocking it — it routes to `triage` for orchestration attention, breaking the unblock↔re-block loop. |
| `unblocked` | — | `blocked → ready` (or `todo` if parents are still open), either manually or via `/unblock`. Resets the dispatcher's `consecutive_failures` but deliberately preserves `block_recurrences` so the loop breaker keeps its memory. `run_id` is `NULL`. |
| `archived` | — | Hidden from the default board. If the task was still running, carries the `run_id` of the run that was reclaimed as a side effect. |

**Edits** (human-driven changes that aren't transitions):

| Kind | Payload | When |
|---|---|---|
| `assigned` | `{assignee}` | Assignee changed (including unassignment). |
| `edited` | `{fields}` | Title or body updated. |
| `reprioritized` | `{priority}` | Priority changed. |
| `status` | `{status}` | Dashboard drag-drop wrote a status directly (e.g. `todo → ready`). Carries the `run_id` of the run that was reclaimed when dragging off `running`; otherwise `run_id` is NULL. |

**Worker telemetry** (about the execution process, not the logical task):

| Kind | Payload | When |
|---|---|---|
| `spawned` | `{pid}` | Dispatcher successfully started a worker process. |
| `worker_registered` | `{pid, started_at}` | The dispatcher died after starting the worker but before recording its pid, so the worker recorded it itself before its first model call. Liveness checks then see it and an expired claim is extended instead of spawning a second worker. A worker whose run was reclaimed before it got that far exits without working the card. |
| `heartbeat` | `{note?}` | Worker called `hermes kanban heartbeat $TASK` to signal liveness during long operations. |
| `reclaimed` | `{stale_lock}` | Claim TTL expired without a completion; task goes back to `ready`. An automatic reclaim counts as one non-successful attempt toward the `gave_up` breaker (a claim that never spawned a worker would otherwise loop claim → reclaim → claim forever); an operator `reclaim` resets the counter instead. |
| `restart_resume` | `{prior_run_id, prior_session_id, resumed_session_id?, copied_messages?, interrupted_calls?, fallback?, messages?, tokens?, threshold?}` | The owner admitted a run whose previous run was lost to a gateway restart (`owner_restart_lost`). With `resumed_session_id` the lost run's transcript was copied into this run's session; with `fallback` (`prior_session_missing`, `prior_session_empty`, `prior_session_unreadable`, `prior_session_rotated`, `foreign_home`, `foreign_profile`, `over_compaction_threshold`, `threshold_unknown`, `plan_failed`, `seed_failed`) the run started fresh with the transcript tail. |
| `crashed` | `{pid, claimer, exit_kind?, exit_code?, worker_output?}` | Worker PID no longer alive but TTL hadn't expired yet. `worker_output` is the tail of the worker's own log (its final response or the rendered provider error, chrome stripped, ≤ 400 chars) and is also appended to the task's `last_failure_error`, so the board shows *why* instead of only the exit code. |
| `timed_out` | `{pid, elapsed_seconds, limit_seconds, sigkill}` | `max_runtime_seconds` exceeded; dispatcher SIGTERM'd (then SIGKILL'd after 5 s grace) and re-queued. |
| `stale` | `{elapsed_seconds, last_heartbeat_at, heartbeat_age_seconds, timeout_seconds, pid, terminated}` | Task ran longer than `kanban.dispatch_stale_timeout_seconds` (default 4 h) AND no `kanban_heartbeat` arrived in the last hour. Dispatcher SIGTERM'd the host-local worker (if any), reset the task to `ready` for re-dispatch. Does NOT tick the failure counter (stale is dispatcher-side absence detection, not a worker fault). Workers running long operations should call `kanban_heartbeat` at least once an hour to avoid this. |
| `reconciled` | `{reason, claim_lock, claim_expires, worker_pid}` | Orphaned-card reconciliation: the card was `running` with broken claim bookkeeping (`claim_lock` or `claim_expires` NULL — crash mid-claim, manual SQL, DB restore) and no live worker, so none of the TTL/crash/stale paths could ever recover it. The dispatcher requeued it to `ready` with an explanatory comment. Gated by `kanban.reconcile_orphans` in config.yaml (default `true`). |
| `respawn_guarded` | `{reason}` | Dispatcher refused to re-spawn this ready task this tick. Reasons: `infrastructure_cooldown` (the host refused the last spawn — no restart-safe systemd scope — and the cooldown has not elapsed; never counted against the card), `rate_limit_cooldown` (the last failure was a quota wall / rate limit / 429; same cooldown, never counted), `blocker_auth` (last failure was an auth error — a person must fix credentials), `recent_success` (a completed run happened in the last hour — wait for review before re-running), `active_pr` (a GitHub PR URL appears in a recent comment — a prior worker already opened a PR). The task stays in `ready`; the next tick gets another chance to spawn. If the underlying condition persists, the normal `consecutive_failures` circuit breaker will auto-block via `gave_up` after `failure_limit` failures. |
| `spawn_failed` | `{error, failures}` | One spawn attempt failed (missing PATH, workspace unmountable, …). Counter increments; task returns to `ready` for retry. |
| `skipped_nonspawnable` | `{assignee}` | Dispatcher refused to spawn because the assignee profile does not exist in this home (or is not in `kanban.dispatch_profiles`). Written once per card — repeated only when another event landed in between — so `show`/`tail` name the missing profile without a row per tick. The card stays in `ready`; reassign it or install the profile. |
| `protocol_violation` | `{pid, claimer, exit_code, protocol_violation, worker_output?}` | Worker exited successfully while the task was still `running`, usually because it answered without a terminal board call (`kanban_complete`, `kanban_request_review` or `kanban_block`). Emitted on every violation (the payload's `protocol_violation: true` marker is copied into the run metadata and feeds the violation-only retry budget). Below the budget — up to `_PROTOCOL_VIOLATION_FAILURE_LIMIT` (default 3) *consecutive* violations, per-task `max_retries` overriding — the task simply returns to `ready` for another attempt; when the streak reaches the bound the dispatcher also emits `gave_up` and auto-blocks. `worker_output` carries the worker's own last printed text (usually its explanation of why it stopped), also folded into `last_failure_error` and shown to the retry worker as the prior-attempt error. |
| `gave_up` | `{failures, effective_limit, limit_source, error, terminal_provider?}` | Circuit breaker fired after N consecutive non-successful attempts. Task auto-blocks with the last error. The effective limit resolves as task `max_retries`, then dispatcher `failure_limit` / `kanban.failure_limit`, then the built-in default. `terminal_provider: true` means the worker exited `78` on a provider error a retry cannot fix (credential revoked, model gone) — including a startup credential failure that explicitly requires re-login (`hermes auth` / `setup`), which exits `78` before the first turn; other startup failures exit `1` — and the breaker fired on that first attempt, sticky, regardless of the limit. |

`hermes kanban tail <id>` shows these for a single task. `hermes kanban watch` streams them board-wide.

## Out of scope

Kanban is deliberately single-host. `~/.hermes/kanban.db` is a local SQLite file and the dispatcher spawns workers on the same machine. Running a shared board across two hosts is not supported — there's no coordination primitive for "worker X on host A, worker Y on host B," and the crash-detection path assumes PIDs are host-local. If you need multi-host, run an independent board per host and use `delegate_task` / a message queue to bridge them.
