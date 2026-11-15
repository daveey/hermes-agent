# Bounded core workspace-source v2 spike (local only)

Base: `82c3b418880f426ee695811cca713506ad1d22c0`.
Board counterpart: `dc6d3badb974202c4b43d9b2cbad5e157faa92a4`,
`docs/workspace-source-v2-spike.md` and `worker/src/workspace_source.ts`.
No PR, push, deployment, artifact implementation, placement change or pin cleanup.

## Exercised core path

`hermes_cli.kanban_source_registration` exposes explicit Python helpers:

- `register_initial(conn, task_id, source_path)` requires a live local primary scratch
  run and a linked Git worktree registered with a configured clone. This bounded
  spike accepts only the exact task-owned branch `wt/<task_id>` and refuses paths
  occupied by another active task. It does not accept the clone itself. No creator
  condition is imposed. The Board transport performs the authoritative lease check.
- Before calling the real `kanban_source_board.attach` transport with `mode:initial`,
  `manifest_revision:0`, `source_protocol:2`, it commits an `AttachedRun` observation.
  The existing local-only journal gains machine and protocol columns, migrating old
  observations to protocol 1 with unknown machine, never upgrading them implicitly.
  Pending observations survive an unanswered RPC and can be replayed from a new
  database connection. Replay preserves the original pre-attach observation.
- `recover_local` locates the exact `(task_id, run_uid, machine)` predecessor from
  the journal. Missing, foreign or old-protocol records fail closed. It never sorts
  runs or uses `custody.attached_run_uid`, which is null after ending.
- `resume_premanifest(conn, task_id, grant_reply, custody)` accepts only same-holder
  retained protocol-2 revision-zero custody, validates the separately returned current
  live lease against the local current run/task/machine, then uses the grant's exact
  `source_predecessor`. It records the successor before a protocol-2 `mode:resume`
  attach. The successor test opens a new DB connection after the predecessor ends.
- Neither helper changes the task's scratch `workspace_path`; the journal's source
  `path` remains separate. Initial registration is mid-run: even a clean initial
  attachment cannot emit `abort:pre_spawn`. A clean pre-spawn successor still can.

## Evidence and limits

Logs: `../evidence/core-spike/` (outside the clone).

- `red-initial-run1.log`: baseline helper absent, expected assertion failure.
- `green-initial-run1.log`: first wire/journal slice passed.
- `red-successor-abort-run1.log`: missing successor helper and initial incorrectly
  eligible for pre-spawn restore (2 expected failures / 5 passes).
- `green-successor-run1.log`: helper plus existing launch/publish suites passed.
- `red-occupied-run1.log`: another active task's source path was not refused.
- `green-final-run1.log`: runner invocation error: nonexistent preflight test file;
  corrected to the existing receipt suite, no result claimed for that attempt.
- `green-final-run2.log`: 138 tests passed across registration, launch, publish,
  receipt suites; subsequent final evidence names/hashes are in verification logs.

These tests use real Git linked worktrees, real task/run storage, the real source
journal and source transport serialization. The HTTP Board boundary is a transport
seam double; successor end/grant/custody observations are fixture-provided according
 to the sibling contract, not obtained from a running Board. This is NOT proof of
core-to-Board integration, daemon restart/process recovery, or deployed migration.

## Missing production wiring (intentional)

No CLI command, tool, dispatcher route, automatic discovery/adoption, or worker launch
is added. Existing grant parsing/admission still negotiates protocol 1 and does not
carry the new predecessor through production dispatch; per-card protocol negotiation
and an authenticated end-to-end grant consumer remain required. The helper accepts
a checked raw grant reply only for this spike. It does not resume an OS process or
claim that a predecessor hint proves cleanliness or process ownership. Filesystem
revalidation and RPC are not atomic against concurrent external Git mutation.

No scratch artifact manifest schema or publication path is implemented. Existing v1
artifact-empty manifests and publish behavior remain unchanged; later acceptance for
artifacts is preserved, not dropped. Exact-delta independent review belongs to the
supervisor after this commit; this is feasibility evidence, not rollout approval.
