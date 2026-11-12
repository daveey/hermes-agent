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

## Pressure contention

Hysteresis writers wait up to 250 ms for the pressure lock, then refuse as unknown.
The successful waiter rereads state and samples current load under the lock. The
final gate independently retains its overall lock/lease time budget. A timeout
never clears the saved brake.
