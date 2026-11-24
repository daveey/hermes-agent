# Historical rejected-event audit (not transport retirement)

This diagnostic addresses only frozen `task_events` upserts refused with the exact
containment/spend reasons accepted by `kanban_history_residue_source`. It does not
execute, replay, remove, rewrite, settle or accept the rejected operation. Ambiguous
historical identity remains ambiguous. Existing restrictions remain in force.

After normal server/core review, CI, landing and deployment:

```
hermes kanban --board default sync history-residue OP_ID --reason 'Retain historical rejected intent without replay' --json
hermes kanban --board default sync diff --history-evidence --json
```

The first command stores the complete exact local outbox source in a separate
`sync-shadow/default.history-residue.db` sidecar. It stages immutable request
custody under short local/replica reservations, releases SQLite reservations, then
sends at most one authenticated POST and requires an authenticated GET readback.
An existing intent is GET-only even if its first process exited before POST.
A missing readback remains unresolved; there is no replacement identity or resend.
The sidecar contains reported historical source bytes; keep it private and retain
it with the original board/evidence. It is not a normal synced task table.

`sync diff` never trusts that sidecar alone. A successful nonbusy diagnostic pull
must precede a same-head empty feed / fresh current observation / empty feed cut.
The consumer matches the full exact source, local event, sealed receipt and
portable local/replica/canonical parent, and vetoes active workers, claims, leases,
new local work, tombstones, omissions and quarantine. Existing containment/spend
restrictions are retained, not released. All network reads occur without SQLite
writer reservations. Final zero-wait reservations compare the original snapshots
and build the actual diff before releasing them. The 30-second monotonic evidence
budget includes refresh, local lock acquisition, HTTP and final comparisons.

The raw `rejected_ops` count stays unchanged. `audited_history_rejections` names
only exact IDs verified in that particular cut. Unknown rejections and unrelated
row/child drift still prevent clean. Ordinary local `kanban_sync_client.diff`
has no historical receipt exemption. No stored proof can be applied to a later
diff. Busy, partial, stale, unavailable or conflicting proof fails closed.

`--history-evidence` includes exact per-row source and current response bytes,
plus both empty-feed brackets and their head. Treat this output as private audit
evidence, not publication material. The output explicitly retains
`historical_identity_certified=false` and `execution_authority=false`.
`no_retained_op_receipt` is only a current cache observation; it is never a
never-seen operation claim or historical admission proof.
