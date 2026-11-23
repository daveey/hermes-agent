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

## Boundaries requiring disposition before activation

- A lost INTEGER-PK singleton cannot be reconstructed by the present refill
  algorithm. Index-derived loss counts alone cannot prove its survival. A
  surviving sync journal anchor plus lost sender state causes `missing journal
  state`; this is a fault, not a recoverable allocator inferred from a backup.
- Loss/recreation of the **sync anchor** is different: public push selects strict
  transport from `sync_state.journal_id`. This audit does not establish a safe
  legacy fallback after that anchor is lost or recreated. The supported attach /
  recovery / admission chain must be disposed before activation; do not infer it
  from nofill coverage or passing integrity checks.
- `_build` does not invoke `mark_restore_required`; the helper's existence is not
  evidence of a wired restore lifecycle. Normal strict push does inspect/reconcile
  the serving slot before issue, but that does not prove the lost-anchor case.
- Missing deferred, command, omission or lineage custody needs authoritative
  reconciliation. This change prevents stale refill; it does not promise complete
  recovery or turn missing metadata into known history.
- Ordinary sync status counts are not an allocator/restore/deferred-custody
  readiness proof. No live status or no-pending acceptance is claimed here.

These boundaries are recorded for the managing owner's post-B disposition. They
are not permission to begin C, certify original captures, merge, deploy or replay.
