# Fork fast-check lane

`ci-fast` admits only the measured, no-build jobs listed in
`tests/ci/test_fast_runner_routing.py`. It is not a replacement for the heavy
`hermes-hub`, Docker `docker-ci`, or long-lived observer `hermes-hub-poll` lanes.
Jobs keep their existing names, conditions, dependencies, steps, permissions,
timeouts and required-result semantics. In particular, neither heavy tests nor
the advisory full-tree `ruff + ty diff` moves here. Completion aggregates and the
CI timing report also run here, never on the observer-only pool: long-running
live-comment pollers can await those very jobs and occupy every slot, and on the
heavy `hermes-hub` pool they queued behind full suites (PR #266: 23 min for a
7 s aggregate). `scripts/fork-ci-runner.sh` pins every admitted job back to
`ci-fast` when it is re-run after an upstream merge.
The `!cancelled()` condition still keeps superseded runs from latching their group.

## Upstream consolidation (November 2026)

Upstream merged the cheap checks into two jobs, so most admitted jobs below no
longer exist as jobs. The CI timing report is now advisory trailing steps of
`all-checks-pass` (`continue-on-error`, `always()`). Ruff enforcement, code
health and Windows footguns are steps of `lint.yml`'s single `lint` job, which
also runs the full-tree `ruff + ty diff` (20-minute timeout), so it runs on
`hermes-hub`. Every repository guard, lockfile/prompt-surface diff and
supply-chain job is a step of `repo-guards.yml`'s `guards` (15-minute timeout,
hadolint through `docker run`), so it runs on `docker-ci`. The `ci-fast` lane
now holds `detect`, `actionlint`, `all-checks-pass` and `macos-state-required`.

## Third admission (November 1, 2026)

`macOS state required (v1)` (`ci.yaml` job `macos-state-required`) is the
macOS state lane's own required check: checkout plus one `python3
scripts/ci/required_results.py --macos-state` over `toJSON(needs)`. No build,
Docker, secrets or matrix; 10-minute timeout. It ran 14 s on `ci-fast`
(runner `ec2-gwgen4-1`, run 41326127097, job 124910774212, 16:41:34–16:41:48
UTC). It must keep `if: ${{ always() }}` so a skipped or cancelled lane fails
closed, and an `always()` job may not wait on `hermes-hub`
(`tests/ci/test_fork_runner_routing.py`), so `ci-fast` is its only lane. It is
not a completion job: `scripts/fork-ci-runner.sh` pins its runner but leaves its
condition alone, and it is not in the aggregate's needs. The rollout must add
its name to main's required status checks separately before promotion is complete.

## Second admission (October 30, 2026)

At 02:31 PDT all 16 online `hermes-hub` runners were busy with full suites
while 9 of 11 `ci-fast` runners sat idle, and these light PR jobs queued up to
50 min. Sample: the latest 40 PR `ci.yaml` runs (successful jobs only, read at
10:15 UTC). Every job below needs only git, python3/jq or `setup-pm`; none uses
Docker, bubblewrap, hub-local paths or repository secrets. The `ci-fast`
slots run the same `gh-runner` image as the hub runners.

| Job | Samples | Median s | Max s | Queue median s (hub) | Queue max s (hub) |
|---|---:|---:|---:|---:|---:|
| All required checks pass (v2) | 12 | 18 | 20 | 54 | 1242 |
| CI timing report | 17 | 35 | 50 | 44 | 2074 |
| check-attribution | 30 | 54 | 67 | 235 | 1981 |
| check-common-ancestor | 33 | 51 | 58 | 208 | 2345 |
| Check uv.lock | 12 | 25 | 30 | 297 | 1696 |
| Icon assets freshness | 34 | 60 | 66 | 168 | 3072 |
| package-lock.json semantic diff | 12 | 54 | 83 | 248 | 1989 |
| Prompt surface summary | 29 | 54 | 68 | 223 | 1946 |
| Supply-chain scan | 26 | 52 | 69 | 424 | 2168 |
| PyPI dependency upper bounds | 11 | 54 | 56 | 387 | 2002 |
| Aggregate review statuses | 22 | 6 | 9 | 188 | 3150 |

Jobs already on `ci-fast` had a 2 s median queue (max 199 s) in the same
sample. The supply-chain jobs' timeouts drop from 15 to the lane's 10-minute
cap. Still on `hermes-hub`: test suites and E2E (Python slices, JS/TS, docs,
bootstrap, catalog, Rust, termux contracts), the advisory `ruff + ty diff`,
`label-rerun` (a 15-minute `gh` poll loop), release/archive jobs that use
secrets or environments, and the cache-timeout smoke.

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
