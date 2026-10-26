# Fork fast-check lane

`ci-fast` admits only the measured, no-build jobs listed in
`tests/ci/test_fast_runner_routing.py`. It is not a replacement for the heavy
`hermes-hub`, Docker `docker-ci`, or long-lived observer `hermes-hub-poll` lanes.
Jobs keep their existing names, conditions, dependencies, steps, permissions,
timeouts and required-result semantics. In particular, neither heavy tests nor
the advisory full-tree `ruff + ty diff` moves here. Existing aggregators stay on
the poll lane; no checks are added to that lane.

## Evidence behind admission

Sample collected October 26, 2026, 02:32–02:33 PDT: latest 25 CI
runs (509 jobs), plus 15 independent actionlint runs. Only successfully completed
jobs with assigned runners contribute runtime measurements. Seconds below are
start-to-completion, not queue time; this is a small observational sample, not a
CPU/RSS benchmark.

| Job | Samples | Median seconds | Maximum seconds |
|---|---:|---:|---:|
| Detect affected areas | 11 | 15 | 33 |
| actionlint | 8 | 14 | 28 |
| Blocking ruff | 4 | 36 | 70 |
| Code health | 3 | 48 | 74 |
| Windows footguns | 4 | 64.5 | 68 |
| Case collision | 8 | 14 | 17 |
| No infographics | 9 | 14 | 23 |
| Blob size | 9 | 15 | 21 |
| Profile archives | 6 | 14 | 24 |
| Lazy dependencies | 7 | 22 | 44 |

Excluded: docs median 207s, JS/TS 983.5s, Python shards 2195–2971s,
bootstrap 385.5s, advisory lint diff 135.5s. Short duration alone does not admit
Docker builds or arbitrary jobs: selected guards perform static scans, Git
metadata/diff inspection, or syntax checks; no test-suite/build fan-out. Their
existing 5–10 minute timeouts remain unchanged. Slow network fetches can still
consume minutes, especially the footgun advisories.

## Capacity and rollout contract

The provisioning owner supplies two isolated, disposable EC2 slots with `ci-fast`
and no heavy or poll label, bounded by the approved node's CPU/memory/storage
limits and 24-hour idle-drain lease. This change provisions no host resources.
Only the runner owner commissions labels after security/lifecycle canaries.

On October 26, 2026 (PDT), the hub supervisor added `ci-fast` to all three
permanent general runners, `hermes-hub-1`, `hermes-hub-2`, and `hermes-hub-3`
(runner IDs 28, 27, 32). Each retains `hermes-hub` and its unchanged CPU budget;
all three were independently read back online with both labels. This persistent
pool prevents EC2 lease expiry from narrowing fallback to a single heavy slot.
The supervisor-owned EC2 drain must preserve `ci-fast` on all three permanent
runners. Dedicated EC2 slots must pass canaries and have a durable idle-drain
handle before this routing change merges.

GitHub labels are AND constraints, not prioritized alternatives: shared runners
may take fast jobs even while dedicated slots are idle. This is a scheduling
fallback, not an ordered preference or a latency guarantee during overload.
After the 24-hour dedicated-node lease ends, the permanent eligible pool equals
the permanent general pool, but reserved admission is gone: heavy work can again
delay fast jobs. Retirement therefore preserves schedulability, not the pilot's
latency benefit; continued separation requires renewed dedicated capacity within
an approved budget. Temporary general EC2 nodes are not part of this fallback.

Never route to bare `self-hosted`, add the heavy label to dedicated fast slots,
or borrow observer slots. Before retiring the shared fallback, either provide
another verified persistent eligible runner or normally review/merge routing
back to `hermes-hub`; do not strand already queued jobs. Preserve fork policy,
job isolation and root-only operator credentials. A label is not a security
boundary.

## Failure chain and recurrence guard

Small checks waited behind long Python/JS jobs because both requested the same
finite runner class. More general capacity helped throughput but could be filled
by another batch of long jobs; the earlier observer-only split did not reserve
admission for checks. Existing routing tests protected cancelled aggregators,
not fast checks. The symptom is queue delay, not excessive checker execution:
the sampled queue contained blocking ruff/health jobs waiting about 25 minutes
for checks whose observed median execution was 36/48 seconds.

Dedicated slots plus the conservative routing allowlist separate admission.
Regression tests require the exact light class, bounded timeouts/no matrix or
service containers, independent actionlint triggering, required-gate dependency
coverage, and unchanged heavy/observer placement. Existing required-result tests
exercise failures, skips and release strictness. Rollout evidence must still
show actual fast jobs on dedicated runner identities while heavy work progresses;
unit tests and configured labels alone do not establish that.

Main retains its single `ci-main` concurrency group. Gains are primarily PR job
queue delay; this does not remove main's deliberate serialization or establish
an end-to-end main latency target.
