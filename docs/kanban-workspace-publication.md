# Git-workspace custody at Kanban handoff

## Problem and evidence-backed cause chain

Observed: resumed cards require their previous host because implementation commits,
uncommitted patches and checkpoint files were retained only on its disk. This is
not evidence that every host marker is wrong: live runtime evidence remains local.

1. Runs can close successfully without publishing their implementation. Before this
   change, `kanban_continuation.continue_task` stored checkpoint metadata and called
   `_end_run`; `kanban_db_schedule.schedule_task` and `kanban_db.block_task` similarly
   changed status and ended the run. `_end_run` only updated SQLite run/task rows.
   None of those paths inspected Git publication.
2. A checkpoint was treated as durable continuation state even when it pointed to
   a different, unpublished worktree outside the card's scratch workspace. The
   scratch/worktree distinction describes the launch directory, not all artifacts
   owned by the run. A branch name alone cannot reconstruct dirty files or a
   commit the remote does not have.
3. Recovery retained the filesystem rather than transporting source identity.
   `kanban_db_workspace._ensure_git_worktree` reused a local branch or created it
   from local HEAD; `_restore_repo_for_missing_worktree` searched local repositories
   for that branch. Neither fetched a published checkpoint branch on another host.
4. Movement therefore deliberately refused every `dir`/`worktree` card in
   `kanban_move.check` with `workspace_bound`. Removing that guard alone would
   permit silent loss or execution of a different branch/HEAD.
5. **Root (happened):** lifecycle persistence and workspace custody had no shared
   publication contract. Host binding was a safety containment, not source transport.
   **Root (not caught):** tests explicitly pinned unconditional worktree movement
   refusal (`test_workspace_bound_cards_never_move`), while lifecycle tests exercised
   SQLite/checkpoint transitions without a real unpublished Git implementation.
   New real-Git regressions failed on the old code with “DID NOT RAISE”.

Recurrence: earlier published-worktree routing work introduced a narrow *creation
text* exemption (`kanban_published_worktree`); it did not alter park lifecycle,
move, pickup or workspace reconstruction. This fix does not claim that exemption
failed to deploy or that runtime observations are portable.

## Countermeasure

Before voluntary continue, block, schedule or review handoff of an active run:

- Inspect the declared Git workspace, and existing linked-worktree roots explicitly
  named in the checkpoint/reason/summary, metadata `worktree`/`worktrees`, or the last
  three saved run checkpoints. Do not recursively scan or upload scratch artifacts.
- Require a clean worktree and an exact live `origin` branch SHA match, not a stale
  remote-tracking ref. Probe outside the transition write transaction, sharing one
  five-second budget across roots. Recheck local branch/HEAD/cleanliness inside the
  transition, sharing a one-second local fence budget.
- Otherwise refuse with `unpublished_workspace`, unless the checkpoint/reason has
  a nonempty line `UNPUBLISHED_WORK_REASON=<why the work must remain on this host>`.
  The exception records explicit custody; it does not authorize relocation.
- Save server-derived `workspace_publications` receipts on the closed run. A
  caller-provided receipt cannot override the observation. `workspace_publication`
  is the first receipt for compact readers; the plural field is authoritative for
  multiple roots. Involuntary/unguarded endings record unverified custody without
  a remote probe and without preventing run recovery.
  Kernel goal-loop budget, non-finalization and unachievable parks are involuntary;
  a kernel-only call scope preserves their existing transition/ownership checks.
- Carry forward the newest unresolved receipt per path. An exactly published
  receipt releases historical custody (including older unpublished receipts and
  checkpoint mentions); a current explicit reference or declared workspace is
  still checked anew. Removing an unpublished root does not release custody.

No staging, automatic commit, push, credential grant, host-marker removal or move
exception is performed. In particular, copied databases, ignored build outputs,
unregistered/unmentioned worktrees and non-Git scratch scripts are **not** made
portable. Publication receipts are not portability grants. A hard process/host
crash cannot execute a publication step; subsequent lifecycle recovery can record
retained custody only.

## Verification

`tests/hermes_cli/test_kanban_workspace_publication.py` uses real Git repositories
and disposable bare origins. It covers unpublished/dirty/untracked work, deleted
remote branches with stale tracking refs, successful exact publication, explicit
retention reasons, final-local-state races, crash audit, and the scratch + external
linked-worktree pattern through current checkpoint, metadata and prior checkpoint.
A plain clone mentioned in scratch text is not inferred to be owned, even if it
contains unpublished commits. Linked-worktree mentions are checked regardless of
prose such as "read-only"; declared Git workspaces are always checked. Existing
placement refusals remain. Recovery regressions in `test_kanban_publication_recovery.py`
cover kernel stops, release after publication, caller receipt stripping and malformed paths.

DB entrypoints are exercised directly. CLI/tool transport, natural deployed
recovery and cross-worker reconstruction are separate acceptance, not inferred
from these unit/integration tests.

## Follow-up: branch-backed portable source manifest

Persist a versioned `workspace_source` JSON field on tasks and in Board replication:
`{version:1, repository:<canonical credential-free URL>, branch:<full refs/heads/...>,
commit:<full object id>, published_at:<owner timestamp>, manifest_revision:<CAS>,
artifacts:<explicit durable attachment references>}`. A path is host-local derived
state, never source identity. Do not infer the repo from its basename or branch name.
Keep source claims and publication receipts server-owned and bind them to the run
UID, owning machine, task revision and approved repository.

Binding points to change together:

1. Creation/enrollment: `kanban_db_creation`, `kanban_worker_placement` and
   `kanban_placement` persist/validate the source contract and preserve independent
   host markers, profile tools and explicit host requirements.
2. Movement/reservation: `kanban_move.check`, worker-placement reevaluation,
   `kanban_runs_on_capabilities` and Board policy permit a worktree only with a
   current published manifest and a capable destination. Dirty/unpushed/unknown
   custody and plain `dir` workspaces remain host-bound.
3. Pickup: `kanban_worker_node` plus Board row transport validate the manifest
   before admitting execution. Exclude foreign live workspaces; do not reuse an
   identical pathname as proof. Preserve lease/CAS/single-writer authority.
4. Resolver: `kanban_db_workspace.resolve_worktree_for_dispatch`,
   `_resolve_worktree_workspace` and `_ensure_git_worktree` locate an explicitly
   registered clone of the same repository on the destination, fetch the named
   branch, verify exact commit, and materialize that task's own local worktree.
   Missing source, remote drift, occupied/dirty paths, wrong repo, detached or
   diverged local branches must fail closed without resetting or overwriting.
5. Park/handoff: reuse the custody guard, add an authorized secret-scanned push of
   already committed task source only (never arbitrary staging/auto-commit), and
   attach a separately durable evidence manifest. Publish-before-release must be
   idempotent and revalidated against run/task generation; push failure retains
   host custody and records the reason.

Acceptance: isolated two-clone/two-host fixture resumes exact branch+SHA after the
original worktree is removed; includes scratch-owned external worktrees and
attachments. Real normal-ingress worker execution records hostname, Board holder,
source identity and tested SHA. Inject rejected pushes, absent credentials, remote
branch deletion/drift, stale tracking refs, dirty/untracked/ignored private evidence,
concurrent park/edit/claim, foreign leases, repo URL rewrites and active destination
worktrees. Each must refuse without losing local state or admitting a second writer.
Test create, move, scheduled continuation, review handoff, timeout and crash recovery;
verify old mixed-version clients fail closed. Ship source schema/Board/core through
normal exact-head review+CI, then prove actual cross-worker claim and reconstruction.
