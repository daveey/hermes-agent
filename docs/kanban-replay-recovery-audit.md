# Replay sender: protected recovery audit (B4)

This is a **backup-refill audit**, not activation, successful lost-journal recovery,
minimum-writer admission, receiver migration or live proof. Capture certification
remains dormant. An index proves an identity, not the current value of mutable
custody stored under that identity.

## Recovery mechanism

`kanban_db_recover._build` salvages readable current rows, then optionally calls
`_fill_from_backup`, then recreates indexes/triggers. Refill requires an unreadable
rowid still listed by a full source index; every surviving listed index key must
match the older backup. Cleanly absent rows and rows without index evidence are
not refill candidates. Trigger protection installed **after** the copy cannot
establish that the copied history is current.

The audit executes the actual schema installers in SQLite and reads full-index
metadata. It covers the eight replay tables, seven ordinary sync tables, three
reserved-key lineage tables, dependency-recovery receipts, two integrated command
tables, and three adjacent receipt/protect tables. The latter are listed to make
the boundary explicit, not to broaden the commissioned outbox transport.

## Inventory (24 tables)

“Indexed” below means the real DDL supplies an independent full index usable by
recovery, usually the TEXT/composite PRIMARY KEY autoindex. INTEGER PRIMARY KEY
singletons alias rowid and supply no independent index.

| Table | Refill route | Decision |
|---|---|---|
| `kanban_sync_outbox` | Indexed, explicitly excluded | Existing prohibition unchanged |
| `kanban_sync_state` | Indexed, explicitly excluded | Existing prohibition unchanged |
| `kanban_replay_sender1` | No independent index | No current-schema refill route; no speculative edit |
| `kanban_replay_observation_cursor1` | No independent index | Same |
| `kanban_replay_episode_clock1` | No independent index | Same |
| `kanban_replay_existence_fault1` | No independent index | Same |
| `kanban_replay_deferred1` | Indexed task/origin | Newly prohibited: older status/HLC/capture instance is not current custody |
| `kanban_replay_observation1` | Indexed task/origin | Newly prohibited: old membership/fingerprint is not a current observation |
| `kanban_replay_observation_reads1` | Indexed task | Newly prohibited: an older in-flight read token is not a current reservation |
| `kanban_replay_episode1` | Indexed task/origin | Newly prohibited: old elapsed age/sample token is not current diagnostic history |
| `kanban_failure_commands` | Indexed run UID | Newly prohibited: old delivery/receipt/release state is not current command custody |
| `kanban_actionability_commands` | Indexed op ID and semantic key | Newly prohibited: old delivery/receipt state is not current command custody |
| `kanban_sync_status_origins` | Indexed task ID | Newly prohibited: same status string does not prove the same capture occurrence |
| `kanban_sync_omissions` | Indexed original ID and row | Newly prohibited: old field hold/restoration pointer is not current custody |
| `kanban_sync_key_supersessions` | Indexed receipt and task | Newly prohibited: old repair stage/parent lineage is not current |
| `kanban_sync_key_supersession_ops` | Indexed original op ID | Newly prohibited: do not manufacture missing authoritative original bytes from an older copy |
| `kanban_sync_key_supersession_children` | Indexed replacement op ID | Newly prohibited: do not reconstruct a partial older lineage |
| `kanban_sync_parent_envelopes` | Indexed task ID | Existing backup-derived ambiguity retained; available wire is not certified current |
| `kanban_sync_dependency_recoveries` | Indexed receipt and link UID | Existing immutable receipt policy retained; UPDATE/DELETE forbidden and settlement revalidates its exact saved snapshot |
| `kanban_sync_remote_parents` | Indexed parent ID | Existing legacy prerequisite cache policy unchanged; not a processed-prefix verdict or allocator authority |
| `kanban_sync_tombstones` | Indexed table/row | Existing deletion-index policy unchanged; creates no outbox operation or final replay verdict by itself |
| `kanban_protect_commands` | Indexed request and board/request | Refill remains possible; direct non-outbox `/ops` scope was explicitly excluded from this sender gate; **not certified safe here** |
| `kanban_protect_scan` | Indexed board | Same excluded direct courier scope, not a replay sender cursor |
| `kanban_notify_receipts` | All fields form indexed key | Existing delivery receipt policy unchanged; not journal custody or a replay application verdict |

Totals from the executed schema inventory: 13 prohibited (two existing, eleven
new), four without a current-schema listing route, seven retaining their existing
refill behavior. This is not a declaration that every adjacent local Board table
or excluded direct courier is replay-safe.

## Why these rows matter

Production strict push creates deferred references and observation reservations;
observation publication and diagnostics consume their persisted fingerprints and
elapsed age. Restoring older episode/observation rows can pair stale fingerprints
with previously accumulated diagnostic age, even though later sampling refuses to
add time across a restart. Command producers use existing-command/first-wins
checks and confirmations inspect stored delivery state; an older command under an
unchanged indexed key can change those local decisions without recreating its
outbox. Status-origin descriptors can attach an old origin to a same-named status
on a new unseen-task snapshot. Omission and reserved-key consumers likewise depend
on mutable pointers/stages and exact retained bytes, not only their indexed IDs.

The policy change is only the existing refill exclusion set. Readable current
rows remain byte-preserving salvage. Missing protocol custody remains **reported
loss**, not a fabricated reconstruction, absence verdict, new original capture or
permission to send. Normal backup fill of unrelated business rows and the existing
ambiguous-parent fallback remain tested and unchanged.

## Executed storage proof

`test_kanban_recovery_protocol.py` executes real schema installers, writes indexed
rows, takes an older backup, changes non-key custody, damages a real middle table
leaf, and calls production `_build`. Before the change all eleven table cases
assertion-fail because stale rows are actually refilled; the four real singleton
schema/salvage controls pass. After the change the damaged rows stay missing,
known-loss accounting matches the destroyed leaf, and every readable current row
retains its complete stored tuple. This is controlled storage-boundary proof,
**not** an end-to-end command or rollout claim. No runtime write hook, SQLite
authorizer or reentrancy mechanism is used.

Each new exclusion has an individually executed deletion mutant: its table's
actual stale-refill assertion fails. Existing recovery tests retain unrelated
business-row refill and parent ambiguity controls. Tests run through
`scripts/run_tests.sh`, with file retries disabled.

## Lost-anchor admission (B5)

`_build` now writes `kanban_replay_recovery_loss1` in the same transaction as
salvage, before the rebuilt image can be installed. This independent singleton
records recovery and the protocol loss report even if the sync anchor or sender
singleton was unreadable. It is also written for a not-yet-synced database, so
later sync installation cannot turn recovered unknown history into a clean legacy
image. It has no independent index/refill route and is not a per-operation ledger.
Readable prior rebuild reports are retained; unreadable prior history stays loss.

Public sender admission checks the marker, the restore flag and any existing
journal binding **before** selecting legacy or performing sender network I/O.
Missing/recreated anchors with surviving journal custody become durably
restore-required. Every rebuilt database is held, even if no protocol loss was
index-countable or the anchor survived intact. Clearing the ordinary flag alone
cannot clear the independent rebuild hold. `RestoreRequired` is isolated as a
per-board round error, so an affected board does not crash sync for other boards.
The gate installs no journal, certifies no original capture and enables no mode.

`test_kanban_replay_admission_loss.py` exercises actual table and singleton page
damage through `_build` and reopen, NULL/recreated anchors, all eleven indexed
custody-loss classes, later sync installation and the real sync-round boundary.
Lost command/deferred/omission/lineage rows remain explicitly reported LOSS, not
absent work that can be silently retired or reconstructed. Never-bound,
unrecovered legacy databases retain their existing transport.

## Remaining recovery and rollout boundaries

- Authoritative reconciliation tooling for incomplete/lost journals is not
  implemented here. The independent rebuild hold has no automatic clear path;
  replay's existing intact-image reconciliation helper is not sufficient to
  certify completeness of a rebuilt image.
- Complete loss of all local evidence cannot be diagnosed from surviving local
  metadata. Recovery of that case remains outside this bounded sender change.
- These checks fence the commissioned sender and its normal sync round. They do
  not certify the excluded direct non-outbox protect commands/scan or promise
  that all unrelated read-only reconciliation interfaces are disabled.
- Ordinary sync status counts are not allocator/restore/deferred readiness or
  live no-pending proof. Old-writer drain/minimum-writer admission stays dormant
  and must be proved separately before final independent review and landing.
- No activation, receiver migration, integration, deployment or live proof is
  established by B4/B5. No runtime write hook, SQLite authorizer or reentrancy is
  introduced.
