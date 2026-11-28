# Quiet inventory / audit lock contention

Problem: the preserved Mac cohort recorded a 5.423s inventory, including 4.784s
of fresh path resolution, while the serving loop waited at `Attestation._audit`'s
ledger lock. A mature standard check still refused on plugin reviews; no admission
or production recovery is claimed by this patch.

Why 1: requested inventories held the same RLock needed by import/exec/native audit
ingress. Evidence: `InventoryObservation` and `_audit`; the original new concurrency
test failed with `unrelated audit ingress queued behind filesystem resolution`.
Why 2: path resolution was performed inside that critical section although it does
not read or mutate ledger ownership/dependency state. Evidence: `_module_file`'s
resolve/owner/classify boundary and the retained incident phase counters.
Root (happened): slow external filesystem work and ledger mutation shared one long
critical section. This patch moves only fresh resolution outside it. Ownership,
classification, module bookkeeping, settlement and plugin verification remain
locked. A new/current module snapshot is taken after reacquisition: only the same
module object and filename can use its per-call prepared path; deltas resolve via
the original locked path. Nested readers retain the original lock discipline.
Root (not caught): existing tests covered optional logging lock inversion and
inventory expiry, but did not require ordinary audit ingress to progress while
fresh module filenames resolve. The new test fails on the unmodified parent.
Recurrence: adjacent observer-lock fixes (0ff9e514fe, 8a955dceb6) did not remove
mandatory resolution from the ledger lock; this addresses that distinct boundary.

Countermeasures:
- symptom/root: fresh per-call resolution outside the outer ledger critical section;
  no cross-call path cache, new trust category, review bypass, raised limit or changed
  deadline. Source/classification and audit evidence remain fail-closed.
- detection: `test_import_audit_progresses_while_inventory_resolves` has authentic
  RED-before-product and GREEN-after-product execution. Additional tests cover
  changed/replaced/added/removed modules, symlink retargeting on all public readers,
  nested locks and resolver exceptions. The native grant fixture exercises the real
  authenticated local socket, TicketStore mint and redemption, with explicitly
  controlled audit ingress at the mint boundary (not a global installed-audit proof).

Limits: this is a lock-contention correction, not a claim to remove the inventory's
own total resolution cost or satisfy the normal two-second quiet preflight. Native
hashing and pending-code settlement can still hold the lock. Real supported-boot
security coverage, independent exact-head review, required CI/canonical ship,
natural Mac activation and full aged admission acceptance remain separate gates.
Remaining plugin source closures/enrollment and the original failed latency cohort
belong to t_01a217a1e20d7b63; no competing owner or new prerequisite is introduced.
