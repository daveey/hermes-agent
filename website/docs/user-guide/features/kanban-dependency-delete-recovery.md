---
title: "Kanban: abandoning containment-refused dependency deletes"
description: "Audited per-link recovery preserves the live authoritative dependency and the original refused operations."
---

# Abandoning a refused dependency delete

A refused `task_links` DELETE can leave a live canonical dependency, a restored local link,
a stale local deletion tombstone, and one or more rejected outbox operations. A later containment
release is **not** permission to retry that historical DELETE. This recovery abandons it instead.

```bash
hermes kanban sync dependency-delete 'PARENT_ID>CHILD_ID' --reason 'retain the authoritative dependency' --json
# Inspect the preview and preserve its token. Then use the identical reason:
hermes kanban sync dependency-delete 'PARENT_ID>CHILD_ID' --reason 'retain the authoritative dependency' \
  --apply --expect TOKEN --json
```

The `>` must be quoted to avoid shell redirection. `--reason` is required and nonblank.
Preview performs a diagnostic replica refresh but does not write the live board or create receipt tables.
A busy/incomplete refresh is a refusal, not a successful preflight. The command requires an enrolled
sync identity matching the local board/gateway and binds its nonsecret endpoint fingerprint into the token.

## Eligibility and evidence

- The local and refreshed replica link must already exist with exactly the requested parent and child.
- Every operation for that link must be a rejected empty-fields DELETE with the exact Board refusal
  `containment_hold: use scoped resume-contained; generic writes cannot satisfy containment`.
  Both endpoint task rows must exist, with no endpoint task outbox work.
- The only eligible tombstone is this link's, with its deletion clock equal to the latest covered DELETE.
  Matching `unlinked` history must remain available and is copied into the receipt without rewriting its payload.
- Both canonical endpoint rows and the link must be live, with valid versions/identities. Endpoint origin and
  settled local version must match the Board. Local containment checks must pass, including certified-release
  verification; fresh authenticated Board containment contexts must say neither endpoint is held.
- An active worker lease is preserved. This command never changes lifecycle state, runs, claims or lease ownership.
- The replica cursor must match authenticated empty feed evidence (`head = next = cursor`, `more=false`).
  All authoritative row reads and the concluding empty-feed check must cover that same cut; a moving feed refuses.
  The entire evidence interval, including SQLite writer-lock wait, is limited to 30 monotonic seconds.
- Apply re-fetches evidence, compares the preview token, then compares exact local inputs again in its transaction.
  Changed operations, history, task rows, identities, reason or link versions require a fresh preview.

This is a bounded observation, not an atomic reservation or a distributed Board lock.
No server mutation is sent. Operators must recheck authoritative state after recovery.

## Writes and audit custody

Apply atomically inserts one host-local `kanban_sync_dependency_recoveries` receipt and clears only the exact
covered stale link tombstone. The receipt contains original outbox rows (including original `fields` strings),
matching historical events, a SHA-256 of the canonical snapshot, the reason, endpoint fingerprint and canonical
proof. SQL triggers prohibit receipt UPDATE and DELETE. The original rejections remain unchanged at apply.
No dependency, task or historical event is inserted, updated or deleted; no replacement DELETE is queued.

Normal sync subsequently retires only byte-identical receipt-covered rejected operations after fresh canonical
link/endpoint/containment checks. A changed link, pending/newer work, changed audit or damaged receipt retains
rejections. Unrelated later progress events do not invalidate the historical audit. Retiring an outbox operation
does not remove its immutable receipt or its existing `unlinked` / rejection events.

A repeated apply after the tombstone has been cleared refuses rather than creating a second receipt.
Run normal sync to settle the existing receipt. If evidence or newer work prevents settlement, investigate it;
do not clear the outbox, edit the receipt, remove the dependency or replay the old DELETE manually.

The operational owner remains responsible for repeated completed diagnostic refreshes, authenticated empty-head
verification and confirming that no unrelated drift remains. An implementation test or successful apply alone
is not a claim that a live board has converged.
