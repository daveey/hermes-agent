# Agent-owned non-human containment

Containment stops work without declaring it successful. A contained task remains
an unsatisfied prerequisite: dependency edges stay open and consumers remain
gated. It does not request a human, dispatch a reviewer, schedule a timer or
submit the task to the triage decomposer.

## Supported entry points

An admitted Kanban agent can use either the agent tools or the CLI:

```text
hermes kanban contain [TASK] --authority INDEPENDENT_TASK --summary HANDOFF
hermes kanban contain [TASK] --authority INDEPENDENT_TASK --producer PRODUCER --summary HANDOFF
hermes kanban resume-contained TASK --episode UUIDV7 --kind authority
hermes kanban resume-contained TASK --episode UUIDV7 --kind producer
```

`kanban_contain` and `kanban_resume_contained` expose the same arguments and shared
service. Neither interface accepts caller-supplied actor or run identity. CLI
invocations must run in the actual dispatcher's admitted task/run/session context;
setting an informational worker-task variable alone does not confer permission.

Omitting `TASK` contains the admitted task. An admitted ancestor may instead
contain an unclaimed TODO descendant without first dispatching that descendant.
The authority and optional producer must be independent same-board tasks, not the
target or its descendants. Current leases and graph scope are rechecked at the
mutation boundary. A successful result with `terminal_for_run: true` means stop
this agent run, not mark the task complete.

## Exact resumption event

A successful stop returns an exact `episode` UUIDv7 and a frozen predicate. Only
an authenticated `containment_released` event bound to that episode and predicate
can release the containment. Its evidence must be either:

- A currently live admitted run of the nominated independent authority; or
- A canonical producer completion event and completed run that are both genuinely
  new relative to the frozen baseline. Completing the run already in flight at
  the stop boundary is not new producer readiness.

Generic unblock, promote, completion, review, ordinary row synchronization and
elapsed time do not constitute release evidence. Resumption preserves all edges,
never restores review, and retains an independent spend hold. Otherwise the task
returns to TODO behind unsatisfied prerequisites or READY when eligible.

## Terminal non-success archive

The ordinary archive operation can close a contained task without resuming it.
Every unreleased episode must have its exact attested stop, and each frozen
authority must be archived, or done with no producer nominated by that episode.
A live authority, a missing authority, or a done authority with a producer refuses
the entire archive. The target must still be unclaimed triage with no human ask
or pending deletion.

Each episode receives an immutable, authenticated `containment_closed` receipt
bound to the frozen predicate and authority identity. This is **not a release**:
the task remains held, its episode marker and dependency edges remain, and its
dependents remain unsatisfied. It cannot dispatch, resume, unblock or be deleted.
Ordinary archived tasks without containment keep their existing dependency behavior.

Enrolled boards additionally require `terminal_archive: 1` in the containment
capability advertisement. Core sends the ordinary canonical `archive` operation,
recovers uncertain delivery with the same request ID, and reads back the exact
closure, archived audit event and task rows (targeted context plus per-UID evidence,
never a board snapshot) before projecting archived locally. Audit `via` / `event_extra`
are persisted in the durable request as Board `archive_metadata`; a retry with
different metadata is refused, and the canonical `archived` event must match it exactly.
Missing or unauthenticated closures leave a replica in triage. A generic local
archive precondition cannot be carried atomically by the ordinary Board operation and
is refused before any network call. The one supported scope is the operator
`creator_archive` (`CreatorScope`): the creator identity is immutable and checked
locally, and the Board itself atomically requires the unclaimed triage state. Existing local stop history without durable stop attestation
also refuses terminal archive; upgrading alone does not authenticate old evidence.

## Local and enrolled boards

A genuinely local board uses one SQLite transaction for stop/release, its durable
receipt and run closure. Persisted Board membership prevents an enrolled board
from falling back to local authority when configuration changes or networking
fails.

An enrolled board first requires authenticated GET-only
`/v2/boards/<board>/containment/capability`: protocol `1`, receipt envelopes `[1]`,
task marker `containment_episode`, and resume predicates `["authority", "producer"]`.
Missing, unreadable or incompatible advertisements refuse before mutation, never
falling back locally. It also requires containment context protocol version 1 and
matching canonical containment operations. Preparation reads only
lifecycle coordinates and a producer baseline. The Board rechecks authorization;
replica fields, a claimed origin or an ordinary sync-applying flag never grant
release authority. Upgrade and verify both components before live application.

Canonical event receipts are cached host-locally after authenticated readback.
SQLite guards do no network I/O. A server-owned task episode marker and durable
observations keep the task held across task-before-history, cleared-marker-before-
release and restart ordering. Stale queued lifecycle edits are stripped without
reusing an old operation identity with changed bytes.

## Uncertain delivery

The exact request bytes and operation identity are committed before network I/O.
An uncertain stop is held locally before any canonical reply. CLI/tool errors
with code `containment_delivery_unknown` return `request_uid` and `episode`.
Retry the same admitted call; do not invent a new request or conclude that no stop
occurred. A live nominated authority can settle the original uncertain request
through `resume-contained` with that exact episode before authorizing release.
Recovery never substitutes the authority's lease into the original stop request.
A definite authenticated rejection removes only the provisional request hold;
it cannot clear a distinct containment episode.

These checks protect supported agent and synchronization paths. They are not an
OS sandbox against a process that possesses the enrolled gateway credential and
can execute arbitrary trusted code.
