# Board consumer extraction contract (WIP, not activation-ready)

Consumer owner: t_01a2053d33aee6a6. Coordinator: t_01a20518eff1c5e4.
This is an extraction handoff, **not** the exhaustive Legacy schema fixture manifest
or a source-adapter parity certificate. Those gates remain open.

## Canonical pure board projection

`plugins.kanban.dashboard.board_read.project_captured(rows, include_archived,
*, diagnostics_per_task, ages, assignees, now) -> dict`

The existing legacy `project()` now captures its inputs and invokes this exact
function. No competing serializer or board reader was introduced.

Required inputs:

- `rows['cards']`: ordered `(Task, task_fields, extra_fields)` tuples. Task is the
  canonical parsed task; `task_fields` is its exact `dataclasses.asdict` result.
  `extra_fields` is the existing materializer's dictionary, not a new schema:
  `latest_summary` (nullable preview <=200 chars), `current_run_started_at`
  (nullable epoch), `link_counts` (`parents`, `children` integer counts),
  `comment_count` (integer), `progress` (null or `{done,total}`), and
  `managing_session_id` (nullable string).
- `rows['done_total']`: complete filtered done count, before done-window trimming.
- `rows['tenants']`: canonical globally ordered distinct nonnull tenant list.
- `rows['latest_event_id']`: canonical maximum event ID including bookkeeping.
- `diagnostics_per_task`: canonical diagnostic lists keyed by task ID. Omit a key
  only for an authoritatively empty diagnostic result, never failed capture.
- `ages`: complete task-ID map of canonical `task_age` projections, with keys
  `created_age_seconds`, `started_age_seconds`, `time_to_complete_seconds`.
  Legacy catches malformed-row age failures as the same three null values.
  Owner must use a captured clock and preserve that exact calculation/fallback.
- `assignees`: canonical sorted union of configured profile names and task
  assignees. This is an explicit input; the pure reducer never scans profiles.
- `now`: captured wall-clock epoch; output is `int(now)`.

Output remains `{columns,tenants,assignees,latest_event_id,now}`. Columns follow
`BOARD_COLUMNS`; archived is appended only when requested. Each column has
`name,tasks`; done additionally has `total,omitted`. Unknown status is bucketed
into todo. Existing task ordering is retained, except done is sorted by
`completed_at DESC NULLS LAST,id DESC`. Long body is truncated to 200 characters
with `body_truncated:true`; otherwise the flag is absent. Nonempty diagnostics
add `diagnostics` and canonical warnings `{count,kinds,latest_at,highest_severity}`;
empty diagnostics add neither. Returned mutable containers do not alias captured
card/age/diagnostic/roster inputs.

This seam is the **local board projection only**. `materialize`, diagnostic capture,
age capture, and `board_shadow.for_board` still have source reads. The owner must
supply source-complete captured inputs, shared graph/membership/count indexes,
and canonical shadow projection; never call legacy materialize per consumer.

## Canonical pure catalogs

`plugins.kanban.dashboard.board_catalog_projection.projects(rows) -> dict`
accepts ordered `Project.to_dict()` values captured from `list_projects` with
`include_archived=False`. Output has exactly `projects: list`, whose rows have
`id,slug,name,primary_path,icon,color` (strings). Falsey optional path/icon/color
normalize to empty string; no output field is absent or nullable. Empty capture
is `{"projects": []}`. Capture failure is source health, not empty success.

`plugins.kanban.dashboard.board_catalog_projection.boards(rows, current, *,
counts, workspace_kinds, project_names) -> dict` accepts ordered canonical board
metadata dictionaries, current slug, slug-keyed complete count/workspace maps,
and id-keyed project names including archived projects. Preserves every metadata
field and ordering; adds/overwrites `is_current:bool`, `counts:dict[str,int]`,
`total:int` (all statuses except archived), `default_workspace_kind:str`,
`project_id:str|null`, `project_name:str|null`. Unknown project id gives null name;
missing count/workspace input raises rather than inventing authoritative emptiness.
Output keys are exactly `boards,current`. Nested mutable output does not alias
inputs. Canonical legacy catalog routes invoke these same pure functions after
capturing their existing source inputs. No extra reader or store is registered.
`test_kanban_board_catalog_projection.py` freezes these catalog field shapes and
null/absent rules under runtime file/DB/network/process/clock tripwires; existing
catalog route tests remain the integration regression gate. Board metadata itself
is extensible and this is not its exhaustive source-schema manifest.

## Route and lifecycle contract

View names: `board`, `boards`, `projects`, `tree`, `project_sessions`.
Board identity belongs to authorized Scope; board selector retains tenant,
include_archived, workflow_template_id, current_step_key, done_limit.
`boards` retains include_archived; `projects` has empty selector.
Tree selectors retain surface (`profiles` or `native`), profile, preview_limit,
session_limit. Native project_sessions retains surface, profile, project_id,
session_limit. Native zero/default coercions remain those of methods_config.

Startup without a facts binding calls `plugin_api.start_legacy()` and retains its
closeable prewarm lifetime. With a binding, startup calls
`plugin_api.bind_facts(ctx, *, provider, stop_legacy, hot_selectors, legacy_paths,
lifecycle=None)`
even in participating legacy mode. Paths are already authorized
lexical DB paths, not request-time discovery. The provider returns BoardViews
also in participating legacy mode, so actual reads remain leased. None is only
unbound/nonparticipating legacy; missing facts binding is Unavailable.

Activation withdraws route admission, drains actual readers, stops/joins board
prewarm, fences paths and retires rows observers/snapshot publishers. Rollback
reopens legacy paths and prewarm idempotently. REST tree leases its inner
coalesced worker; /board leases its synchronous get_board reader, so cancelled
HTTP waiters cannot hide still-running readers. Both native methods lease inside
existing authorization. Readiness is publication-driven; close fences late
readiness and demand. Failed transition generations remain fenced against older
callbacks, while same-generation retry/rollback remains possible.

Request-local immutable ContextVars pin provider selection across fast lookup and
legacy leases, including detached coalesced work and current-board aliases.
Native pinning remains inside authorization. New requests resolve afresh.

## Real host integration milestone

Consumer history is rebased onto host `91d0d2fdf51adffe4102c6afc443eff0fd43cec7`.
`BoardViews` and `bind_facts` accept an explicit startup-only `lifecycle` providing
`bind_consumer(name, scope, callback)` and `report_ready(name, scope, version=1,
ready=bool)`. When omitted, the original combined v1 interface remains compatible.
The real `ReplicaV1` deliberately has no release authority; pass it for reads and
subscriptions, its handle for demand, and the coordinator's authority separately.
Do not pass the test lifecycle in production or infer activation readiness from
this consumer milestone.

`test_kanban_board_replica.py` uses the real FactStore, FactHost Unix socket,
acquire/ReplicaHandle and publication callbacks, with a test-only release driver.
It exercises all five view names through exported board/catalog/REST/native
adapters, scope rejection, selector separation, health failure, deletion and
recovery, idempotent rollback, plus frozen canonical board graph/delete payload
parity. Request-thread DB/file/network/process guards leave background IPC
running. Native authorization is preserved; its memory-only scope integration
remains a coordinator gate. Source data are published explicitly by fixtures:
this is real-host transport parity, not proof of automatic source invalidation.

## Plain local-board capture (coordinator integration)

`board_fact_capture.capture(conn, **board_selector)` requires a caller-owned SQL
read transaction and captures canonical `materialize` rows, diagnostics, ages and
profile roster. It discards Task/EventHistory and memo objects, retaining only
plain card fields/extras and explicit projection inputs. `freeze(capture)` is
valid; `board_fact_capture.project(frozen)` calls the same `board_read.project_fields`
serializer used by legacy `project_captured`, with no clock/source access.

This is NOT an operational source registration: callers must provide authorized
context, short-lived read-only connection, auxiliary-worker custody, capture
budgets and refresh scheduling. SQL fields share a transaction; external diagnostic
config/replica/roster reads do NOT form an atomic cross-source snapshot. Shadow
merging, catalogs, demand registration and serve activation remain outstanding.
Tests exercise real links/comments/delete mutations, frozen output detachment and
an interleaved committed writer between canonical SQL reads; old and new captures
remain separate. Existing board route/cache/shadow tests retain legacy behavior.

## Outstanding gates / extraction work

- Canonical pure shadow and REST/native tree
  extractions and exact callable signatures; keep discovery/mutations outside.
- Exhaustive LegacyCard/Detail/Wake and nested tree field/type/nullability fixture
  manifest. The field inventory above is intentionally not that certificate.
- Producer host startup/authorized-memory registry, actual source capture and
  global five-consumer/all-profile/aggregate lifecycle composition. Global
  annotation boot-thread join occurs after both annotation and tree admission
  close. Per-scope fake activation does not prove that composition.
- Source mutation parity: deletes, filters, old/new project membership, graph
  edits/new links, remote-parent-only changes, disappearance, unknown vs empty;
  compare full and delta under frozen clocks. Publishing captured legacy bytes
  proves transport parity only.
- Required exact-head independent review, CI, canonical inert landing after
  producer compatibility, and coordinator-owned joint live acceptance.

No live service was signalled and no individual consumer was activated.
