# Shared blocker recovery

Shared walls are derived from run history. Repeating a wall does not postpone
its worker probe: the interval starts at the latest probe start, or the oldest
assertion still behind the wall when no probe has started.

`kanban.shared_blocker_checks` maps canonical blocker ids to shell commands.
The built-in `github-rest-quota` check is
`python3 <Hermes home>/bin/github_quota.py --reserve-ok`. Install the companion quota
helper with that flag before relying on this check. Override a command with an
empty string to disable it. Commands run as the dispatcher account, outside
SQLite write transactions, with a ten-second timeout. Configure only trusted,
read-only commands; their bounded output tail is retained in task events.

For an open wall with a home-owned carrier card, the dispatcher schedules a
check every 120 seconds, including while an agent probe is running. Exit 0
records recovery and wakes held cards in the same maintenance pass. Exit 1
means the wall still stands. Three consecutive errors fall back to the normal
worker probe interval. Reassertions don't reset this error budget or postpone
checks. A command change or a new episode after recovery resets the budget.
The check scheduler cannot promise a two-minute wall-clock bound if the
machine/dispatcher itself is stopped or its maintenance loop is delayed.

`kanban.shared_blocker_aliases` adds one-hop `alias: canonical-id` mappings.
Keys use the input slug function and length limit. Collisions select the
lexicographically smallest original string key with a valid nonempty target,
regardless of mapping order. Non-string keys, invalid/empty targets and empty
input slugs are ignored.
Targets should be canonical slugs, not other aliases. Built-in GitHub REST/core
quota spellings always collapse to `github-rest-quota`; config cannot split
that family. Historical run and event payloads are normalized on read.

Workers with a registered check are told to run it when their next action
needs that capability, rather than blindly repeat context. A GitHub quota
assertion from a run known to have started before the last verified recovery
cannot reopen the wall after that recovery. It remains eligible for release.
An active-run snapshot handles local same-second claims; UUIDv7 claim-initiation
milliseconds also identify earlier remote claims not yet synced at recovery.
This is wall-clock evidence, not distributed causal order: clock skew, missing
precision and exact ties retain the legacy timestamp/snapshot fallback. A new
run after the recovery can report a new outage. Other blocker families retain
their existing assertion semantics.

Check events use a fingerprint of portable run identities, not local integer
row ids. This fences results racing new assertions and survives synced id
remapping and import order. Recovery is not inferred from a failed command,
and commands never run under the board's writer lock.

## Operator unblock verdict

An explicit unblock is ordered against assertions using the event UUIDv7's
millisecond timestamp, falling back to `created_at` for legacy or non-v7
identities. Only exact time ties use local event ids; delayed replication of an
older assertion cannot revoke a newer release. UUID random suffixes are not
clocks. This is wall-clock ordering, not protection against gateway clock skew.

For unregistered walls a running sibling still holds the card; otherwise a
release newer than every assertion bypasses the probe interval. Registered walls
retain their own-card release rule, before the sibling check.

`hermes kanban unblock` prints the shared-blocker verdict after the transition;
`kanban_unblock` returns it as `shared_blocker_verdict`. It either says
`released: claimable on the next tick`, or names the blocking sibling or the
probe interval's due time in PT. Reporting uses the same read-only decision as
the dispatcher guard, without clearing the wall or electing another probe.
This is a snapshot of the shared-blocker gate only: dependencies, placement,
capacity and later assertions can still delay actual dispatch.
