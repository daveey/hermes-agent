# First-send Board envelopes

The sync sender persists each outbox operation's first transmitted envelope in
`kanban_sync_outbox.wire_json` inside the write transaction that precedes network
I/O. An uncertain response, subsequent round or process restart reuses its op ID,
HLC and payload without consulting the current task/child row or generating a new
clock. Acknowledgement removes the outbox row and its envelope together.

Synthetic parent upserts have the same pending-transport contract in
`kanban_sync_parent_envelopes`: a lost response cannot mint a fresh parent ID.
Acknowledgement removes the parent envelope; pruning removes envelopes whose task
is gone or already acknowledged. A definitive rejection transfers the parent to
the existing bounded outbox rejection path. Only a confirmed rejection or explicit
schema/lifecycle repair may advance mutation identity. Transport errors, including
uncertain HTTP failures, never silently clear a saved envelope.

These are pending transport records, not a permanent per-operation receipt ledger.
Malformed saved envelopes fail closed: reconstructing unknown transmitted bytes
could re-execute a mutation. Do not clear them automatically to make sync green.

## Compatibility and limits

An existing pending row with no saved envelope acquires one on its next send.
The original envelope of a row already sent by pre-upgrade core cannot be
reconstructed. No capture-time migration or UUID-age expiry rejection is used:
a genuinely never-sent offline edit must retain first-send LWW ordering.

Deploy the sender before Worker replay arbitration. An old Worker plus the new
sender preserves task-field LWW on replay. It does not provide child/delete or
lifecycle replay-effect protection; those require the separate Worker change.
This sender patch alone is not end-to-end replay-after-receipt-expiry safety.

## Failure analysis and proof

Problem: receipt reclamation followed by the former sender's fresh-HLC retry can
replace another gateway's newer edit (hermes-hub historical real-DO fixture).
Why: receipt miss reexecutes the op; the fresh HLC wins field LWW; core rebuilt
payload and clock on every transmission. Root (happened): first-send mutation
identity/order was not durable across the expiring receipt boundary. Root (not
caught): receipt-hit and retention tests did not compose lost responses, restart,
receipt expiry and a competing writer. Recurrence: the PR160 review's known gap;
not a diagnostic-deployment regression.

`test_kanban_sync_envelope.py` failed on unchanged core for regenerated clock and
synthetic parent identity (2 failed/1 passed). The fixed sender passes. The edge
suite independently reads the DB at network entry to prove commit-before-send,
checks run/delete retries across reopening, acknowledgement/pruning, and rejects
an immortal synthetic-parent cached rejection (RED before that correction).
Lease and neutral-release behavioral suites remain required controls. End-to-end
Worker expiry/lifecycle tests and its normal review/CI/deploy gates are separate.
