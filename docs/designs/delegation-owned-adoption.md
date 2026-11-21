# Owner-claimed async delegation registration

`tools.async_delegation_adopt.adopt_unit` is an internal scheduling seam for a durable
unit **already claimed by this process**. It is not an automatic retry of abandoned
work and does not transfer ownership, park children, change update admission, or
replace `delegate_task continue`.

The caller must first establish resumable child custody, provide a runner for only
the unfinished children, and keep the original delegation ID. This seam refuses a
missing, terminal, delivery-settled, foreign-incarnation or already registered row.
It rechecks the unchanged durable row under the in-memory registration lock.

Scheduling uses the same session-close, slot capacity, approval-notification lease,
retirement reservation, context propagation and progress-monitor path as new units.
Registration does not INSERT/REPLACE the ledger or reset delivery attempts. The
original dispatch time, routing scope, transcripts, child sessions, slot and partial
finished results survive. A pool/lease/submit failure removes only the in-memory
registration and releases scheduling reservations; the adopted durable row remains
available to the caller. Finished children join the remaining results before the
original one-shot finalizer publishes the original unit's completion.

New units persist `slot_key` in task_json; older rows without it conservatively use
the original delegation ID as a separate slot. No schema change is required.

Verification: `bash scripts/run_tests.sh tests/tools/test_async_delegation_adopt.py`.
This verifies registration/finalization, not SIGKILL-after-park recovery. That later
protocol must separately prove original completion and external tool effects exactly
once across restart before it is enabled. Reverting this seam is an ordinary reviewed
revert; no parked rows are produced by this change.
