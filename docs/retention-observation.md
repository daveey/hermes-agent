# Non-CI retention observation slice

This is the first implementation slice for t_49cb744e, not a completed retention
service. It deliberately cannot delete or authorize deletion. The existing
scratch-workspace Git preservation predicate is unchanged.

Run from the source environment:

    python -m hermes_cli.retention_cli observe --workspace /absolute/workspace \
        --candidate exact/relative/cache

JSON goes to stdout. Exit 0 means complete bounded observation, 2 means retained
with incomplete coverage (or disabled apply), 1 means invalid input. The caller
must save stdout outside the candidate. No retention state is provisioned.

The observation schema is intentionally distinct from a future live apply plan.
It retains every record; byte counts are allocated bytes, never promised free
space. Partial scans expose measured partial bytes separately and leave totals
unknown. Symlinks, Git metadata (including bare/reftable repositories), mount
boundaries, unavailable descriptors and traversal limits fail closed. Records
use base64 filesystem path bytes and decimal strings for identity/timestamps.
Duplicate/overlapping candidate roots are rejected; hardlinks are deduplicated.

Limits: Linux fdinfo mount IDs are required for completeness; scans are not atomic,
metadata cannot prove rebuildability, symlink entries are not followed, and
atime may change. There are entry/depth limits but no kernel syscall timeout.
No lifecycle, reference, recovery, root-process, container or lease proof is
implied. Install records and full clones remain ineligible.

Apply has a code-level refusal, no environment/config enable switch. Outstanding
work before a supervisor canary: owner/event/catalog checks; exact migration and
verified input/recovery manifests; manual claim/GC exclusion session; root /proc
and container-bind evidence; root-owned0600 plan-bound procedural approval;
durable quarantine/journal/reconcile and receipts; independent review, exact-head
CI, merge, and a fresh fifteen-minute in-window plan and supervisor approval.
Workers and timers must never run apply. Same-UID sudo operators mean root-owned
approval files are procedural authority, not a cryptographic security boundary.
Reservations are not writer exclusion. Actual successful reclaim telemetry must
advance only for committed positively verified removed bytes, with df separate.

Tests:

    scripts/run_tests.sh tests/hermes_cli/test_retention_plan.py \
        tests/hermes_cli/test_retention_cli.py -j 1

The standalone module entry point is intentional for this staged slice; no
`hermes maintenance` command or automatic completion hook is installed yet.
