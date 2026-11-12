# Failed worker-result reconciliation

The dispatcher and a pre-armed observer share the same transactional close and
failure-accounting path. The observer reads only the Kanban board and OS process
identity, not the owner session database. This addresses a stalled owner loop
without signalling the worker or the owner.

## Eligibility and safety

A modern failed result records the bound process identity, a writer sequence and
a snapshot of preceding bound/result event identities. Synced boards use event
UIDs; unsynced snapshots use content fingerprints, never replica-local row IDs.
A complete snapshot permits a final failure to supersede an earlier successful
turn or restart checkpoint. It cannot hide a later unseen receipt or rebind.
Every historical bound interpreter, as well as the outer worker, must be proved
dead. A still-occupied recycled PID or unreadable birth is a veto. Linux zombies
count as exited only with the exact saved birth checked around the state read.

Closure compares the exact current task/run/claim/PID/birth under the board write
transaction. Closure, counters and final events commit together; competing
observers and dispatcher ticks cannot charge the same local run twice. The
source result event is immutable. Its time is recorded separately from the
reconciliation time. Quota, storage refusal and owner restart remain neutral
according to the existing policy; their metadata uses the same causal selection.

Legacy launches without valid two-process witnesses retain the existing
reclaimer policy. Ambiguous modern evidence remains fail-closed, including TTL
reclaim. Such a hold requires investigation; it is not proof of a live worker.

## Custody and timing boundary

On Linux, the writer starts a transient user-manager service outside the worker
cgroup and waits for the observer's board-open acknowledgment before committing
the result. The service uses `Restart=on-failure`, a 500ms restart delay and a
500ms read-only observation interval. Board connections use a 250ms busy timeout
without creating or migrating a board.

### Board generation safety

The observer pins the board file's device/inode under a separate short generation
lock when it opens (retrying contention for up to two seconds). Every poll checks,
lock-free, that this inode remains at its path; archive, deletion or replacement
ends observation. Closure takes only the generation lock, revalidates the inode,
and retains the lock through finalization. Native removal takes dispatch then
generation, both non-blocking, before changing the current pointer or moving files.
An observer never takes the dispatch lock and holds no generation lock while idle.
A dispatcher stalled holding its tick lock therefore does not delay independent
closure. If removal wins first, revalidation refuses closure on the stale handle.
The observer never closes a run in an archived board or on a recreated board.

The real-child regression and separate-cgroup natural-teardown test require
closure within **five seconds after both processes exit**, with the owner
performing no dispatcher ticks. This is an exercised healthy-observer deadline,
not a hard real-time guarantee across OS starvation, a stopped user manager,
repeated observer crashes, unavailable storage or indefinite write contention.

A total arm failure preserves the original result and records
`worker_result_observer_failed`; it does **not** establish independent custody.
In that case a healthy dispatcher can reconcile it, but this implementation does
not promise bounded closure while that dispatcher is also stalled. Non-Linux
launching is detached best effort, not demonstrated restart supervision. Missing
birth evidence and old legacy launches are outside the bounded observer contract.

## Verification boundaries

The tests cover both entry paths, real-child death, outer/bound live vetoes,
recycled/unknown identities, historical binding liveness, competing finalizers,
rollback of accounting, causal selection with reordered local IDs, readiness,
failed arm, idle read-only polling, explicit board forwarding, and deleted-target
startup. Sync tests use the actual SQLite capture/client against an **in-memory
Board contract double**, including grant/echo/release and duplicate sweeps. They
are not a deployed Board or a network-partition/lease-expiry arbitration proof.

Forced observer restart, Mac/Windows behavior, a production owner-lock fault,
and production deployment are not established by these fixtures. Merely reading
`Restart=on-failure` is not a successful restart test. The live hub's loaded
`code_sha` must be verified separately after landing.

## Owner-only restart before bootstrap

An owner-only witnessed restart (recorded before the worker bootstrap binds its
own process) can be closed by the dispatcher once the outer worker and every bound
process are dead, but it does **not** arm an observer. Its guarantee is
dispatcher-only: while the dispatcher is stalled there is no independent bound.
