# Pending replay provenance storage — prerequisite only

This implements only the nullable pending-custody storage and sticky-taint part
of the approved Board processed-prefix design (§8), not capture certification,
migration, sequencing or an exactly-once sender:

<https://github.com/daveey/hermes-hub/blob/74bf783d249947ecefd0dc069cbcd8789316e9fa/docs/designs/POST-COLLECTION-DESIGN.md>

The reviewed bootstrap amendment is retained on hermes-hub main. Implementation
and overall live acceptance remain owned by `t_01a1fee0783d8a08` and recovery owner
`t_01a1ef099b996747` respectively.

## What this stage changes

The normal sync schema installer adds nullable `capture_protocol`,
`original_op_id`, `original_created_at`, and `identity_state` to the existing
`kanban_sync_outbox` and `kanban_sync_parent_envelopes` records. Existing rows and
all production insert sites in this stage remain NULL/UNKNOWN. Nothing backfills
an original ID/time from a current row, UUID, attempts counter or frozen envelope.
The columns are deleted with pending custody; there is no extra operation table,
per-operation receipt ledger, retention change, new transport or new writer.

SQLite guards in the caller's existing write transaction:

- reject changes to the original capture tuple, including NULL-to-value retrofit;
- reject changing `identity_state` except to the sticky `ambiguous` state;
- taint an in-place outbox ID/time change;
- conservatively taint changes to a frozen outbox operation's target, operation,
  fields, base version, HLC, status origin or stored wire;
- taint synthetic-parent identity/wire changes.

Taint targets the changed SQLite `rowid`, not key equality: ordinary TEXT primary
keys here are nullable, and changing one to NULL must still taint exactly that row,
without touching unrelated NULL-key custody.

Freezing a previously unfrozen outbox operation and ordinary attempts/error
bookkeeping do not taint it. Unexposed coalescing may change fields while
preserving the capture-time lower bound. `wire_json != NULL` is a conservative
taint trigger, **not** certified network-exposure evidence: a freeze before any
send can over-taint. Parent caches have no certified unexposed state; any rewrite
is conservatively ambiguous. Trigger names intentionally do not match the
removable `kanban_sync_%` capture triggers, so detaching/reinstalling sync cannot
clear pending metadata guards.

These guards are not a hostile-SQL sandbox. Trusted code can INSERT, REPLACE or
delete custody. An ordinary delete/reinsert defaults back to UNKNOWN, never
original. NULL/UNKNOWN and `ambiguous` are both ineligible for automatic absence
classification. A future certification writer must audit every insertion,
update, delete, replacement and lineage path before stamping any original.
A future trigger-body change must version/replace these trigger definitions;
`CREATE TRIGGER IF NOT EXISTS` is not a body-upgrade mechanism.

## Backup-derived parent recovery

A table index can prove the identity of an unreadable pending parent, but cannot
prove that an older backup retains the current wire or its latest exposure/taint
history. The supported database rebuild therefore preserves available backup wire
and original ID/time columns while setting a refilled parent's `identity_state`
to `ambiguous`. It does not certify the restored payload, settle the obligation,
or authorize transmission or migration. Current readable salvage remains an exact
copy; outbox backup refill remains prohibited. Older parent schemas without a
provenance column remain UNKNOWN, not retroactively certified. The recreated
immutable guards prevent promoting the recovered ambiguity back to `original`.

`tests/hermes_cli/test_kanban_recovery_capture.py` covers real table-page damage
through `_build`, backup refill, saved-envelope readback and sticky ambiguity,
plus older backup/target schemas and unrelated business-row fill controls.

## Required subsequent gates — none is supplied here

1. Drain old writers; persist/enforce minimum-writer admission at launcher/database
   boundaries before enabling audited original capture. Existing legacy writers,
   SQL mutation guards and server STRICT refusal alone do not satisfy this gate.
2. Audit every producer and custody-deletion site, not just trigger capture. In
   the examined core, named additional sites include `_queue_restore`, compat
   repair, divergence, failure-release, retain transport, initial import/resnapshot,
   synthetic parents, reserved-key parent/child supersessions and containment
   rekey. The transient retry path also rekeys and clears wire. This list is an
   audit starting point, **not a certified complete insertion-site inventory**.
3. Record immutable, complete principal/epoch/clone exposure history before I/O;
   preserve prior exposures and sticky ambiguity across re-enrollment and repair.
   A newly frozen legacy envelope cannot supply missing historical provenance.
4. Establish/read back current and every former-principal Board barriers. Raw
   receipt observations alone certify neither completeness nor historical binding.
5. Implement exact known-final/fallback classification, atomic custody transfer,
   rollback revalidation and whole-slot readiness. UNKNOWN holds the entire slot;
   no DRAINING/FAULT issuance, including independent cards in that slot.
6. Implement the immutable W=1 allocator/reconciliation/sender, custody-safe repair,
   bounded fault handling and all cross-repo release tests. Keep the seventh test
   unchanged until the actual strict-transport fix; no expected-failure tally is a
   safety acceptance result.
7. Deploy the receiver first, verify its serving-DO capability, then deploy the
   sender through the normal updater. No production original stamping, live-data
   migration or replay is activated by this schema stage.

Until all applicable gates pass, legacy compatibility remains explicitly unsafe
for post-collection replay. The sender still uses its existing legacy route.
Rollback must preserve Board fencing and follow the approved design §9; these
nullable columns are not permission to downgrade an activated protected journal.

## Tests

`tests/hermes_cli/test_kanban_sync_capture_storage.py` exercises actual SQLite
schema upgrade, real capture-trigger defaults, unknown/ambiguous guards,
conservative frozen-payload taint, real containment rekey, transaction rollback,
parent-cache removal and sync detach/reinstall. Synthetic `capture1` tuples in
these tests model a future audited writer only; no production code stamps them.
The focused module was run assertion-RED on unchanged core before implementation.
Run through `scripts/run_tests.sh`, together with the existing envelope, sync,
schema-owner and repair consumers. The complete processed-prefix test plan is
still a separate open acceptance obligation.
