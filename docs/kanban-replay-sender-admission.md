# Sender admission: local exclusion is not migration readiness

This is the dormant C sender slice. It does not activate certification, install
writer gates on a live board, or establish that old processes have drained.

## Public entry and detach

`kanban_sync_client.push` exports the replay sender's public entry. Its callers
include the ordinary sync round and direct retain, source-artifact, scope-renewal
and priority publication paths. The round lock alone does not serialize those
direct callers' HTTP sessions.

The public entry now acquires a distinct, nonblocking per-board sender lock before
transport selection and holds it through all session, send, settlement and ACK
work. A round may hold its own round lock and then acquire this sender lock.
Detach acquires round then sender before changing persisted mode and removing
capture triggers. Contention raises `SyncRoundBusy`; it neither waits on network
I/O nor mutates custody. A preopened connection attempting to send after detach
commits is refused before any peer call. Nonpersistent boards cannot send.

These are two different locks, not recursive admission. No process-local bypass,
runtime write hook, SQLite authorizer or reentrant writer reservation was added.
Older code that does not acquire the new lock remains an external-drain problem.

## Evidence

`test_kanban_replay_sender_process` spawns an independent process using the real
production Hub and a loopback HTTP peer. The peer accepts a session, or accepts an
operation, and withholds its HTTP response. While that child remains alive in the
exchange, a competing public sender and detach are each refused, with no competing
peer calls and an unchanged current database image. Releasing the response lets
the original child complete; a later round-held push and detach both progress.
The peer is controlled test infrastructure, not proof of the deployed receiver.

`test_kanban_sync_writer_process` preopens a separate process's SQLite connection,
then activates the existing gate in a disposable fixture. Missing and lower
revision functions refuse writes, preserving the database image; a matching
revision proceeds. This proves database downgrade refusal, not that a fleet's old
processes or already-read network bytes have drained.

The existing stale-session takeover control now invokes the internal strict
engine for its deliberately unfenced predecessor and the public entry for its
successor. Its fake peer injects the stale-session refusal; it is not independent
receiver-implementation proof. Exact pending-wire identity and settlement checks
remain intact.

## Remaining C acceptance

- Implement/prove the migration owner's old-process and in-flight drainage gate,
  including an admission revision that distinguishes the complete sender contract
  from previously installed revision-1 cores. The revision-1 fixture alone is not
  readiness and cannot revoke bytes held in an older process's memory.
- Deliver due unresolved deferred diagnostics to the existing owner with a durable
  delivery receipt and episode/evidence deduplication across restart. Current
  `episodes.diagnostics` only lists due rows. Courier exit 3 means queued, not
  delivered. Discover and reuse a supported delivery/readback contract before
  wiring this consumer; do not invent a successful response or business resolution.
- Preserve C's dormant activation boundary. D's aggregate fault/throughput and
  condition-mutation audit, fresh independent exact-head review, required CI and
  canonical ship still follow C. Receiver, integration, rollout and live proof
  remain with their existing ordered owners.
