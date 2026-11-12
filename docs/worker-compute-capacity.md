# Worker compute capacity — rollout status and recovery

This implementation is opt-in and is **not enabled by committing it**. Enrollment
must install/read back the parent/user CPU quotas, bootstrap all account execution
boards and map surviving legacy scopes before setting `kanban.worker_resources.enabled`.
The final slot cap requires the two-worker one-hour normal-work acceptance window;
the provisional four-slot design is not a measured recommendation.

## Interrupted board retirement

Normal board archive/delete takes account and installation admission fences,
verifies there are no running rows, and publishes a durable idle proof before
moving files. Detached jobs still occupy capacity through their cgroups.

If the remover crashes after moving the database but before retiring its account
registration, admission remains closed. An operator can recover the exact original
absolute database path using the installed interpreter:

```
python -m hermes_cli.worker_board_retirement /absolute/original/kanban.db
```

Recovery takes the account admission fence and requires a matching durable idle
proof and an absent source. It does not search for similar boards or discard
arbitrary missing registrations. A subsequent claim invalidates any old proof
under the account fence before committing, even if worker resource policy has
since been disabled. Manual database deletion is not a supported lifecycle.

A missing database with no proof cannot be recovered by this command. Restore the
original database from a trusted backup, verify/drain its work, and use normal
board removal. Never delete a registration merely to make admission green.

## Independent sustained-load observer

`python -m hermes_cli.worker_load_observer` performs one sample. Enrollment must
schedule it every 30 seconds in a **system** oneshot service with `User=<worker>`,
`Slice=system.slice`, and `TimeoutStartSec=20s` (including publication). Do not put
it in the user manager: the user CPU backstop can starve it. The timer and service
are still rollout work; this module alone does not install or enable them.

The durable observer outbox fires after load/core is strictly above 2 for 600
seconds. Gaps over 90 seconds, reversed clocks and unreadable load reset continuity
as unknown. A pending alert survives load recovery and retries with the same
idempotency key, including after an uncertain successful create or archived alert.
A recovered valid load at or below 2 rearms the next incident. Evidence failures
never suppress load observations. Process evidence reports sanitized comm/PID,
interval CPU, mapped card, and active uncontained same-UID processes. These are
**not proven escapes**: gateways and user shells also legitimately live outside
the compute pool. No argv, environment, execution DB path or cgroup path is sent.
Corrupt observer state fails loudly; operators must preserve it for diagnosis
rather than silently declaring a continuous observation window.

## Execution ownership

Worker identities are `(resolved absolute execution database path, task id)`.
Board labels cannot distinguish two installations and are not ownership proof.
Admission counts the prospective owner union, allowing a parked owner to resume
at the cap when its populated slice already reserves that slot. A new owner must
fit. Load pressure still refuses both kinds of claim. Existing slug-based owner
maps and legacy flat scopes require explicit rollout migration before enabling.

After independently confirming the exact execution DB/task provenance and setting
and reading back each surviving flat scope's CPU quota (at most two cores) and
CPUWeight (at most 10), register it with:

```
python -m hermes_cli.worker_legacy_scopes register --scope old.scope --database /absolute/kanban.db --task t_id
```

Registration takes admission fences, verifies the task exists (including parked
or terminal owners with surviving jobs), and binds the scope to its boot ID and
cgroup device/inode. It does not infer provenance from a name, move processes,
change their quotas, or signal them. Census resolves this binding inside the
identity-fenced population sample; replacement during the sample refuses it.
Unmapped or recreated scopes remain separate unknown reservations. Corrupt records
for existing scopes fail closed. Multiple verified jobs of one owner reserve one
slot, as for managed slices, but legacy flat scopes have **individual** two-core
limits, not a shared per-card two-core limit. The aggregate pool and user backstop
must cover all of them before enabling; report this transitional limitation until
legacy jobs drain. Jobs outside the pool need separate explicit containment and
readback and are not made accounted-for by this command.

## Pressure contention

Hysteresis writers wait up to 250 ms for the pressure lock, then refuse as unknown.
The successful waiter rereads state and samples current load under the lock. The
final gate independently retains its overall lock/lease time budget. A timeout
never clears the saved brake.
