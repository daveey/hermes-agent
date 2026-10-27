# Update admission protocol, version 1

This is a prerequisite for an external safe updater, not an updater or a deployment.
It covers cooperating Desktop/TUI turn reservations and normal/review Kanban claims
on POSIX. It does not by itself drain messaging gateways, cron, CLI, or old backends.
Windows advertises `supported: false` and retains its existing admission behavior.

## Capability and standalone interface

The stdlib-only capability probe runs without importing the live backend or using
its managed environment:

```
python3 -S <checkout>/hermes_cli/admission_fence.py capability
python3 -S <checkout>/hermes_cli/admission_fence.py hold --home <root>
python3 -S <checkout>/hermes_cli/admission_fence.py state --home <root>
python3 -S <checkout>/hermes_cli/admission_fence.py release --home <root>
```

Capability version 1 identifies an installation-root scope. The root is the result of
`get_default_hermes_root(home=<profile-home>)`: named profiles share their root fence;
independent homes need independent fences. Board storage overrides do not change the
installation whose dispatcher claims work. Every process consuming changed code must
be covered; a capability probe of files does NOT prove already-running processes loaded
that version. The first installation requires operator-owned quiescence/bootstrap.

## Locking and updater order

1. Serialize updaters separately (the existing apply lock). Acquire
   `<root>/core-update/admission.lock` with exclusive flock. Never delete or replace
   this lock file: its inode is the cross-process rendezvous.
2. Create `<root>/core-update/holds/hermes-sync` while holding that lock, then release
   the lock. The standalone `hold` action does this and returns reservation state.
   Keep the hold for the entire drain, install and verification. A crashed updater
   leaves the marker in place; only explicit recovery removes it.
3. Read reservation state under admission.lock BEFORE reading other busy ledgers.
   `reservations` counts pending Desktop reservations and detached workers through
   cleanup. Locked files count as busy regardless of age. Unlocked files are stale
   crash remnants and can be removed. An unreadable state or lock timeout is unknown,
   never zero. The state is deliberately not an overall `idle` verdict.
4. After that snapshot, obtain fresh existing Desktop/backend idle proofs and scan
   all Kanban boards for running claims/runs, including committed claims that have
   not published worker PIDs yet. Do not reuse a DB snapshot opened before the hold.
   Independently stop/drain admission sources outside this protocol before changing
   their shared code. Never hold admission.lock while making backend RPCs or obtaining
   other busy-ledger locks.
5. Only after every covered producer is drained, install and verify. Explicitly
   remove the hold under admission.lock once safe. Do not auto-clear it on age/PID.

The Desktop reservation composes the process retirement fence with the filesystem
reservation without holding either lock while acquiring the other. The turn-publication
reservation is retained across the history-locked running claim, session registration,
agent build, wait, worker dispatch and compute completion. Each executing stage retains
its own reference until cleanup. An already admitted turn can hand off under the hold;
a new turn cannot. New reservations bound lock contention to 50ms, rather than waiting
for the updater's 10-second timeout while a backend session-map lock is held. Queued
user prompts retain their envelopes and retry through the fence with one 250ms timer
per session; new direct submissions report a retryable paused-or-busy error. Pending
unpublished turns are included in the file count.

This is not a whole-process execution lock. Session-create/resume prewarm builds,
idle RPC handlers, notification polling, and retry timers can execute without a turn
reservation. The updater MUST independently quiesce/stop these backend processes
and suppress supervisor respawn before mutating their source or environment; zero
reservations does not authorize a live-tree swap. Desktop SSH-isolated `serve` and
stdio TUI turn submissions both route through this same lifecycle. A source probe
is never live attestation: unknown/old consumers require the operator bootstrap,
with verified process exit/restart against the merged revision before using the fence.

Kanban acquires the SQLite write transaction BEFORE trying admission.lock without
waiting, so database contention cannot monopolize admission. Accepted claims hold
admission.lock through the actual SQL commit (including retry/rollback). Existing
transactions are rejected. Successful commit publishes both task and
run; hooks run after release. Held admissions return no claim and log the pause reason.
An updater waiting for the lock cannot overtake an in-progress claim commit.

Use local POSIX filesystems with reliable flock semantics. Reservation descriptors are
close-on-exec; an explicit fork that retains descriptors can conservatively extend busy
state until the fork exits. This protocol never expires a live lock to manufacture idle.
