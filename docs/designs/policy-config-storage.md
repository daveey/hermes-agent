# Frozen local-policy configuration storage: reader-compatible stage

This change is **not enabled in production**. New receipts stay inline. It does not
resolve the historical Kanban WAL pin or attribute a read-pool denial. It is a
readers-first schema foundation for eliminating measured exact-byte duplication.

## Evidence and causal boundary

An incident probe counted 389,839,654 UTF-8 config bytes across 17,396 receipts;
557 distinct byte strings totalled 12,863,516 bytes. Deduplication can remove
376,976,138 duplicate payload bytes. These are logical payload bytes, not a
claim about physical file reclamation. Messages and FTS dominate the original
16 GB store and are untouched. Session retention already exists; the change
must not be described as discovering absent retention.

Why repeated metadata grows: creation freezes a complete policy configuration
per session, including repeated automation launches. The receipt is the restore
and exact-retry authority, so deleting it early changes custody. Exact-byte
config interning removes duplication without changing its lifetime or values.
The original format had no shared-storage representation. Tests previously
assumed an inline config and therefore did not exercise ref-only comparisons,
branching, last-reference deletion, or recovery with independently missing rows.

## Representation and invariants

`state_meta` holds versioned blob and per-owner reference keys. `config_json`
becomes `config_ref_v1` only when the transaction owner explicitly passes
`intern=True`; the default remains inline. All other policy values, including
`request_json`, are unchanged. Hashing uses exact UTF-8 bytes, not parsed JSON.
Reads verify the hash. Standalone receipt reads join the receipt and blob in one
statement: two SELECTs on a pooled autocommit connection are not one snapshot.
Mutations retain the existing receipt format; branches acquire their own ref.
Retirement removes the blob only after its last owner reference is removed.
No helper commits the caller's transaction; savepoints roll back partial stores.

Recovery copies receipt/blob/ref rows independently, including in salvage mode.
It must not trust copied refs: a skipped ref could make deletion of another
owner remove a still-needed blob. Finalization rebuilds refs from the surviving
validated receipts before returning a verified output. Missing/corrupt blobs
fail recovery closed, retaining the separate output for inspection. This is not
live-store repair and never substitutes config defaults for lost frozen bytes.

## Rollout gates (not implemented activation)

1. Land reader, consumer, retirement and recovery support through independent
   exact-head review, required CI and canonical ship. Leave writes inline.
2. Independently verify the installed revision AND every process that can read
   local policies. Old in-memory readers cannot decode this format. Deployment
   of files alone is insufficient. Do not force a restart under another
   experiment's custody; obtain the existing owner's ordinary custody return.
3. A later activation change must supply an explicit disabled-by-default switch
   at the creation writer and a bounded, resumable conversion through the
   existing owner transaction. No schema-init migration or periodic sweeper.
   Preserve unknown/live admissions, logical/physical targets, exact retries,
   quarantine and malformed-receipt refusal semantics.
4. Before rollback to an old reader, drain incompatible readers/writers and
   hydrate all ref receipts through the new owner, storing them inline. Verify
   zero ref-only receipts before allowing old code. Do not simply flip binaries.
5. Measure production table/index dbstat before/after and the original WAL/pool
   acceptance window only after authorized activation. VACUUM is not required
   by this representation and must not stall a live backend.

Open gates: mutation/clause proof and complete consumer coverage, activation
switch, bounded conversion/rollback tooling, process compatibility evidence,
production measurements, and the independently unresolved WAL/pool incident.
Do not present a passing disposable-fixture dbstat comparison as production
reclaim or this compatibility stage as a shipped producer remediation.
