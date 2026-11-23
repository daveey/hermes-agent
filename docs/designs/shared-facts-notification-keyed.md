# Keyed remote-notice discovery integration

`kanban_remote_notices.collect(..., keys=...)` is an additive path; legacy callers
are unchanged. For a process wake batch, construct ONE
`kanban_remote_notices_keyed.WakeBatch(replica, ledger, now, profile, keys)` and
pass it as `keys` to every affected chat's collector. Do not pass raw key lists
separately for each chat: that repeats the discovery reads. Batch reads coalesce
identities, use one short-lived read-only SQLite transaction and indexed
`gw = ? AND key IN (...)` queries of at most 500 keys. Delivery observation and
claim/ACK writes remain in the existing ledger. Observation lookup requires the
existing canonical expression index; absent index/baseline is incomplete, never
a new baseline or a source scan.

A batch pins replica, ledger, profile and observation time; mismatched use is
refused. It expires after 30 monotonic seconds starting before capture. Captured
JSON is detached from consumer mutations. Raw keys remain useful for single-chat
callers/tests, not a process-wide batch scheduler.

Legacy two-component replica keys cannot safely resolve supersession with an
indexed exact-key read. They are withheld and mark the batch incomplete until
shared reconciliation. Never restore the suffix scan from the initial draft.
Canonical qualified keys keep the existing durable claim loop, including partial
committed receipts on errors, per-link ownership, ACK/release and late handling.

## Still required before activation

* Wire real dirty identities into the loaded notification scheduler, not its
  current wake-all callback, and reuse the batch across affected chats.
* ONE 30-second owning-process reconciler must capture/discover once and dispatch
  claims; do not call full `collect` independently for every chat. Its existing
  whole-index source read is not yet the approved bounded set-based reconciler.
* Propagate incomplete/retry obligations through the executor without reintroducing
  per-chat source polling. Missing baseline/legacy-key coverage must be reconciled.
* Authorized startup, per-consumer switches, legacy drain and links startup remain
  pending; this primitive alone must not disable any existing consumer.

Tests exercise real SQLite/ledger claims, ACK/release, source-read tripwires,
coalescing/paging, whole/keyed payload parity, shared two-chat batch, scope/time
refusal, legacy-key deferral and EXPLAIN QUERY PLAN for observation index usage.
This is not live startup/parity, independent release review, CI or ship evidence.
