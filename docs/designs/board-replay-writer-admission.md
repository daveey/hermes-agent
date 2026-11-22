# Board replay: dormant local writer admission

This is a prerequisite of the receiver-approved post-collection design (§8), not
processed-prefix activation, original-capture certification, or a new writer.
Production only registers a connection-local `replay_writer_revision()` function
on canonical Board connections. **No production path activates a gate yet.**

## Contract

`kanban_sync_writer_gate.activate(conn)` requires the caller's write transaction.
It atomically installs the singleton `replay_writer_gate1(id=1, minimum=1)` and
INSERT/UPDATE/DELETE guards on every existing non-SQLite table in that Board.
An inner savepoint prevents partial installation even if the caller catches the
error and commits its outer transaction. The gate is local constant-size schema
state, not a per-operation ledger. Nothing changes retention or ingress authority.

Revision 1 means **writer-admission support only**. It does not mean this writer
implements certified original capture or complete pre-network exposure history.
The later complete provenance build must use its own higher admission revision;
it must not treat this revision as sufficient to certify captures. NULL/UNKNOWN
historical records remain NULL/UNKNOWN, including newly inserted rows in this
intermediate build.

An unsupported old/raw connection cannot prepare a mutation after activation:
the guard's connection-local function is missing. A compatible but lower-revision
connection is refused by the durable minimum. Reads still work. SQLite invalidates
cached statements when the gate's schema is installed; pre-opened connections do
not keep a mutation bypass. These are DML fences, not a sandbox against someone
who can deliberately drop schema or forge a function. Manual DDL, restore and
repair must follow the spec's rollback/revalidation rules.

The minimum cannot decrease or be removed, including by INSERT OR REPLACE with
recursive triggers off. Sync detach/drop/reinstall uses `kanban_sync_%` names;
it cannot remove the independent `replay_writer1_*` guards. A compatible reopened
connection can continue writing. New tables created by a later compatible schema
pass need guard installation in that same transaction before their admission is
relied on; this revision guards the tables present at activation only.

## Mandatory activation prerequisites (not supplied by this slice)

- Ship and prove the actual receiver atomic-receipt/floor capability first.
- Complete the audited capture, exposure and pending-custody implementation; raise
  its writer revision above this admission-only revision.
- Stop and drain every old writer and already-authenticated/in-flight network send.
  A DB guard cannot retract packets or bytes held in another process's memory.
- Under the existing schema owner/write boundary, activate the minimum and the
  complete provenance mode together. No caller is authorized by this document.
- Preserve the gate across detach and rollback. Do not lower or remove it to run
  an old core. Restore/repair incidents require the spec's explicit revalidation.

## Pending-custody audit map

This source map guides the next provenance slice; **it is not a completed
semantic certification audit**. In particular dynamic SQL, callers' authority,
lineage settlement and all delete/reinsert histories still require semantic
review and behavioral tests before any site stamps `original`.

| Source | Custody mutations requiring audit |
| --- | --- |
| `kanban_db_sync` | `_queue_rows` gap re-emission; `_outbox_insert` and `_insert_trigger` capture; `_apply_one` echo retirement |
| `kanban_sync_envelope` | `outbound` freeze/clock update; `parent` synthetic insert; `retire_parent` and `prune_parents` deletion |
| `kanban_sync_status_origin` | `bind` status-origin amendment before freeze |
| `kanban_containment_outbox` | `strip_lifecycle` deletion or in-place fresh-ID replacement |
| `kanban_sync_client` | `push` attempts, accepted deletion, rejection and fresh-ID transient retry; `import_board` deletion/re-emission; `repair_missing_children`; `retire_lost_placeholder_links` |
| `kanban_sync_compat` | `rejected_parent`, `enqueue_retry`, `_drop_virtual` replacement/deletion |
| `kanban_sync_omissions` | `_queue_restore` restoration after capability observation; omission lineage remap/reconciliation |
| `kanban_sync_divergence` | `repair_divergence` run/task re-emission |
| `kanban_failure_release` | `queue` lease-release transport insertion |
| `kanban_retain_transport` | `reserve`, `record_result`, `confirm` retained transport/custody |
| `kanban_retain_safety` | `recover` retirement/recovery |
| `kanban_sync_reserved_key` | `_write_receipt`, `_queue_children`; preserved original operations and current-ID lineage remap |
| `kanban_sync_dependency_delete` | `settle` rejected deletion retirement |
| `kanban_sync_rejections` | `resolve_rejections` deletion and repair |
| `kanban_sync_capture_storage` | immutable descriptor and sticky taint triggers, including nullable-key row identity |

Also inspect generic/dynamic custody helpers and full-file copy/rebuild/recovery
paths (`kanban_db_recover`, resync/quarantine/partition repair and pending helpers).
Endpoint provenance matters: `/ops` is the floor-classifiable endpoint; retain,
lease and other control transports cannot gain that proof merely by sharing the
outbox. Reserved-key replacement receipt settlement never settles its preserved
originals. No source filename or new attempts counter certifies a historical row.

## Tests

Real file-backed SQLite tests cover inactive old-core compatibility; pre-opened
and cached old statements; business, state, outbox and parent mutation refusal;
unchanged database dumps on refusal; compatible cold reopen; lower-version
refusal; detach/reinstall; replacement/downgrade/removal; and real DDL failure
rollback with both propagating and caught exceptions. Live activation, complete
provenance, migration readiness and network-drain proof remain separate gates.
