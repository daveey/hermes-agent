# Frozen local-policy configuration storage: reader-compatible stage

This change is **not enabled in production**. New receipts stay inline unless
`sessions.intern_policy_configs` is exactly boolean `true` in the creator's trusted
frozen configuration. It does not resolve the historical Kanban WAL pin or attribute
a read-pool denial. The disabled writer switch and finite conversion tool implement
the next storage stage; deployment alone is not rollout authorization.

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

## Rollout gates (production activation remains external)

1. Land reader, consumer, retirement and recovery support through independent
   exact-head review, required CI and canonical ship. Leave writes inline.
2. Independently verify the installed revision AND every process that can read
   local policies. Old in-memory readers cannot decode this format. Deployment
   of files alone is insufficient. Do not force a restart under another
   experiment's custody; obtain the existing owner's ordinary custody return.
3. Only after that custody check, enable the disabled creator switch through
   `hermes config set sessions.intern_policy_configs true` for the intended home.
   This is not enabled by installing the feature. Existing receipts keep their
   format; branches inherit their parent's format. The finite conversion tool
   below changes old receipts only through the existing transaction owner.
4. Before rollback to an old reader, disable the creator switch, stop new ref-only
   producers through the existing rollout owner, and hydrate all ref receipts
   through the new owner. Revisit busy/oversize owners. Independently verify zero
   ref-only receipts before allowing old code. Do not simply flip binaries.
5. Measure production table/index dbstat before/after and the original WAL/pool
   acceptance window only after authorized activation. VACUUM is not required
   by this representation and must not stall a live backend.

## One finite conversion batch

After compatible code is installed, inspect without opening a writer:

```sh
hermes-core-python -m hermes_state_policy_rollout --db "$HERMES_HOME/state.db" --direction intern
```

After the reader/process custody check, explicitly apply one batch:

```sh
hermes-core-python -m hermes_state_policy_rollout --db "$HERMES_HOME/state.db" --direction intern --apply --readers-compatible
```

`--readers-compatible` records the caller's operational acknowledgement; it is
**not** automatic compatibility discovery or authority to override another owner.
Use the JSON `after` as `--after <value>` to resume, or start again to revisit skipped
owners. Defaults: eight keys, 1 MiB payload budget; hard ceilings: 32 keys and 4 MiB.
The budget charges inspected receipt bytes and conservatively estimates hydrated
JSON escaping before fetching a shared blob. It does not bound SQLite page/index
I/O, lock wait time, or SessionDB initialization. Dry-run is metadata-only: it does
not validate payloads or predict hydrated rollback size. Output contains identifiers
and counts only, never frozen configuration or request contents.

Rollback batches use `--direction inline --apply` after disabling/quiescing new
interned writes. Queued/started/unknown admissions, workers, leases and compression
locks are skipped. Malformed custody aborts and rolls back the entire batch. No
session revision, route, policy value or admission is changed. There is no scheduler,
schema-init conversion, manual checkpoint, TRUNCATE or VACUUM in the tool; ordinary
SessionDB transaction and close-time maintenance remain unchanged.

`more=false` and `converted=0` are **not downgrade readiness**: skipped owners,
concurrent creations, and earlier keys can still hold refs. No auto-activation or
old-reader downgrade is performed. Open gates remain process compatibility,
production activation/conversion, physical before/after measurements and the
independently unresolved WAL/pool incident. Disposable tests are not production
reclaim evidence.
