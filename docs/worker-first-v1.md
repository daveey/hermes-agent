# Worker-first portable cards (opt-in, disabled by default)

`kanban.placement.worker_pools` maps an origin Board machine name to worker names, for example `{hub: [worker-1]}`. This is a pool, **not** an AND-list of `machine:*` labels. The default `placement: {}` is unchanged. `heavy_route` remains the legacy behavior only when the origin has no pool entry. A configured empty/unavailable pool queues portable work; it never overflows onto the hub unless the operator enables worker-first overflow (below).

## Worker-first hub overflow (off by default)

Workers always win. The Board reserves the origin hub for a portable card only when **every** compatible member of the origin's *configured* pool (never the request subset) is known, fresh and refused solely for pressure (score >= .85) or full slots, AND the hub's reported measured CPU busy is fresh (conservative end within 15 s) and strictly below the operator's threshold, AND hub capacity/memory/drain allow it, AND no other hub overflow reservation is outstanding, AND the sample's conservative start is after the last hub overflow grant. Unknown, stale, held, draining or offline workers veto overflow and the card queues. Zero compatible workers queues with `no_eligible_worker` (overflow path only; with overflow off the same card queues with the per-candidate rollup `capability_mismatch` or `mixed`, and core treats that as the same state when every configured pool member carries a structural `supports()` reason): the automatic structural origin repin was removed (only the explicit owner repin clears the fence). The origin then posts one owner notice per card revision (`hermes_cli/kanban_repin_notice.py`): a comment naming the missing labels and the exact `hermes kanban move <id> --runs-on machine:<origin> --reason "no pool worker can run it"` command, plus a `worker_repin_notice` event its origin chat receives; the dispatcher stuck summary lists such cards as "waiting for a manual repin" instead of a stall. Nothing repins automatically. Prior runs, host-bound cards and running leases are never moved.

* Board: operator-owned `worker_overflow` setting, e.g. `{"hub": {"machine": "hub", "cpu_busy_below_pct": 50}}`; `{}`/absent disables it. The lease grant re-runs the decision with the current registry and records the consumed sample; a worker freeing up, a CPU rise, a used sample or a disabled setting refuses the grant.
* Core: `kanban.admission.cpu_busy_below_pct` in the **installation-root** config enables (a) a 10 s metrics report carrying `cpu_busy_pct`/`cpu_busy_window_ms`/`cpu_busy_sample_end_ms` (2 s hard total, never replayed), (b) a lock-free advisory pre-check before any Board lease, and (c) the final gate: with `admission.lock` held and before the claim commits, an independent full ~1 s `/proc/stat` sample must be strictly below the limit and the host running count below the root `kanban.max_in_progress`. Any unknown (unreadable config, bad value, failed sample, window outside 1-2 s, passed deadline) refuses. The gate applies to every new ready/review claim on that host (legacy, host-bound and policy cards); the worker load formula is not used for the origin's own cards.
* Rollout order: deploy Boards and clients first; set root `cpu_busy_below_pct`; verify reports; only then enable Board `worker_overflow`. Rollback: set Board `worker_overflow` to `{}` and read it back (outstanding hub reservations become unclaimable). Rollback keeps the root CPU guard and `kanban.max_in_progress` (7): removing the root key or raising the cap is a separate, explicit operator decision, never part of rollback. After rollback the Board still admits the origin hub's review continuation of a card it already ran on the hub (review ruling 11), on the hub's non-CPU admission only (active, unheld, pickup, capability, fresh metrics, no drain, memory < 85 %, a free slot under a known cap); the root CPU guard is then that claim's only CPU check, which is why rollback keeps it. Rollback never creates an overflow, a hub reservation or a new hub assignment, and there is no operator move of a review card to ready: a review card the hub can never admit moves only once its refusal reason is cleared or overflow is re-enabled.
* The advisory is fed only by the sync loop's metrics reporter (`cache/hub-cpu-sample.json`, written before the Board POST, so a Board outage does not starve it). When the sync loop is not running (sync role given up, no identity) or a round stretches past the 15 s freshness, every hub claim on that host defers as `hub_cpu_unknown`: fail-closed by design. The slowest healthy round (10 s cadence + <= 2 s sample + 2 s POST budget) stays inside 15 s.
* Turns are not refused by a claim's sample: on **every** host (whether or not the root limit is set), chat, notification and review turns wait out a busy `admission.lock` for longer than its longest designed hold (`admission_fence.CLAIM_MAX_HOLD_S`), a waiting turn makes the next claims defer to their next tick (also on hosts without the CPU gate, e.g. worker-1: `BUSY_WAIT_S` rose from 0.5 s to 3.0 s for all turn sources), and `prompt.admit` waits outside the session resume lock and then retries its admission once (nothing durable precedes admission, so no duplicate turn).
* A Board grant the core gate refuses (or any claim that never started locally) is released with `unused_grant: true`; the Board then keeps the card first-start, so overflow is evaluated again.

## Creation and schema

Tool, CLI and direct DB producers enroll portable light and heavy cards, including worker children and origin-bound chats. The shared conservative `portable_reason` preserves scratch/default-board, assignee, carried-option, host/local/skill marker, project and gateway-requirement exclusions. Explicit local/foreign placement and `runs_on` (including `[]`) bypass automatic enrollment. Courier creation keeps its immutable owner placement; a chat origin alone does not pin execution. Parent and chat links are not replaced. The internal `placement_checked` flag preserves a front door's already-resolved decision, including deliberate non-enrollment.

The admission CPU sample measures **non-nice busy**: `100 * (delta_total - delta_idle - delta_iowait - delta_nice) / delta_total`. All positive-nice user CPU is excluded, not only nice >= 10; system, IRQ and steal still count. This does not relax memory, slot, drain or freshness guards. A busy batch workload is not evidence that the host has recovered memory or I/O capacity.

Local SQLite `tasks.placement_policy` is a nullable TEXT migration, included in Task and sync column declarations. `worker-first-v1` is persisted in the first create transaction. Unknown policies and contradictory policy/manual pins are refused. A policy card has no runnable local fallback: ready and review claim paths enforce the fence before requesting a Board lease, even when sync is off/shadow or registry retrieval fails.

## Board protocol and admission

The paired daveey/hermes-hub implementation owns selection, CAS and reservations:

`POST /v2/boards/default/placement/reserve`

Request: `{task_id: string, expected_revision: integer, policy: "worker-first-v1", candidates: string[]}` (1–64 candidates, operator allowlisted).

Response: `{state: "assigned" | "queued", machine: string | null, reservation_id: string | null, expires_at: number | null, reason: string}`. Expiry is epoch milliseconds; queued identities are null and reason is `waiting_for_worker_capacity`.

Ordinary primary sync reevaluates only acknowledged origin-owned READY cards without a retained lease or pending local task write. Dispatch retries before checking the origin's own spawn budget. Only the authoritative pull installs assignments; a lost reply cannot manufacture an assignment. Repeated original revisions replay idempotently; current revisions reevaluate/renew. Never move active runs or rebalance review phases.

Score is `max(cpu_load1/cpus, mem_used_pct/100)` below 0.85, plus a separate free-slot requirement. CPU load is load average, not utilization. Server admission rejects unknown/nonfinite/negative/future/stale (>90s) signals and missing capacity/pickup/capability attestations. Workers retain `pickup=explicit`. Registry labels `profile:<assignee>`, `capability:<requirement>`, and `skill:<name>` are operator attestations, not self claims.

`machine_metrics.active_card_runs` samples the shared Kanban root and named boards across profiles, deduplicating physical databases. Foreign lease replicas do not consume local capacity. Unreadable or unknown schema returns unknown, not zero. Before opening a local run, both ready/review claims take a fresh pressure/occupancy sample; refusal releases the unused grant. Existing hard host admission remains in force.

## Rollback and deployment order

1. Deploy the Board schema/routes and matching clients on **every claiming gateway** before enabling any origin pool. Do not run older clients against enrolled cards.
2. Verify fresh whole-host metrics, capacity and operator capability labels. Enable the Board's operator-owned `worker_pools` setting and then the intended client origin pools.
3. Roll back new enrollment by removing client origin entries and setting Board pools to `{}`. This does **not** drop any existing policy fence or interrupt/relocate running leases.
4. Allow current leases to finish/renew. Drain remaining ready cards or explicitly repin each through the creating gateway's `Hub.repin("default", task_id=..., expected_revision=..., runs_on=["machine:hub"])`. The endpoint is `POST /v2/boards/default/placement/repin`; `[]` is an intentionally unrestricted manual pin. Current revision, creator and no-lease/ready checks remain mandatory. Pull and read back policy=null and the exact requested labels before execution. Review cards stay assigned to their last authenticated worker; do not erase their fence in SQLite.

5. **Revert the Board server last, never while policy cards remain queued.** Removing the placement routes first removes `Hub.repin`, while clients retain their execution fences. Verify the authoritative queue is drained/repinned and existing review/running leases have finished before reverting the server.

Clients always send `active_card_runs` in machine metrics, even with pools disabled. A pre-PR Board rejects that unknown field (HTTP 400); metrics reporting is best-effort/display-only, so metrics displays may go stale after server rollback. Roll back clients too for compatible telemetry; do not treat missing telemetry as spare capacity.

No configuration is activated by these code changes. Deployment, actual hostname/holder/run receipts, saturation/recovery, authenticated credential probes and two-worker live distribution belong to the separate rollout phase. If no second existing worker is available, N-worker live acceptance remains incomplete; this does not authorize rentals.

## Frozen acceptance map

| Requirement | Executable evidence |
|---|---|
| Portable light/heavy; retained exclusions; pins/empty pins/courier; legacy routing | `tests/hermes_cli/test_kanban_worker_placement.py`, existing `test_kanban_placement.py` |
| First-insert policy, parent/origin preservation, unknown/contradictory policies | `test_kanban_worker_policy_db.py` |
| Tool and CLI create entry paths | `test_kanban_placement.py::test_tool_creates_worker_policy_atomically_and_reports_queue`, `test_kanban_worker_policy_cli.py` |
| Direct ready/review and local dispatch fences with sync off/registry failure | `test_kanban_worker_policy_db.py`, `test_kanban_worker_reserve_client.py::test_queued_origin_cannot_spawn_even_with_local_capacity` |
| Exact reserve/repin HTTP contract; post-ack reevaluation; stale-field protection; ready-only/no lease | `test_kanban_worker_reserve_client.py` |
| `no_eligible_worker` owner notice once per revision, origin-chat delivery, stuck summary, no repin | `test_kanban_repin_notice.py` |
| Whole-host occupancy, malformed/unreadable fail-closed samples, reporter compatibility | `test_machine_occupancy.py`, `test_machine_metrics.py` |
| Local pressure/capacity refusal releases unused lease, ready and review | `test_kanban_worker_policy_db.py::test_worker_local_admission_rechecks_load_and_releases_unused_lease`, `test_admission_sampling_failure_releases_grant` |
| Real concurrent multi-origin reservations; unauthorized candidates/CAS; expiry/replay/recovery | server `worker/test/worker-placement.test.ts` |
| Numeric validity/freshness, score normalization, deterministic N-worker selection/ties | server `worker/test/worker-selection.test.ts` |
| Two-worker ready rebalance, immovable running lease, capabilities/pickup at claim | server `worker/test/worker-placement.test.ts` |
| Same-card review continuity, forged review-pin refusal, reserved-capacity exclusion | server `worker/test/worker-placement.test.ts` |
| Operator settings, schema, rollback retains fences/leases and explicit CAS repin | server `worker/test/machines.test.ts`, `worker-placement.test.ts` |
| Hub overflow selection, one-reservation/one-sample fences, grant re-check, rollback | server `worker/test/worker-overflow.test.ts` |
| CPU sampler, root-only limit, reporter cadence/budget | `test_machine_cpu_metrics.py`, `test_machine_metrics.py`, `test_kanban_sync_metrics_budget.py` |
| Final hub gate under admission.lock (ready/review, advisory, cap, deadline) | `test_kanban_hub_admission.py`, `test_kanban_worker_policy_db.py` |
| Gate scopes (named boards, profiles, primary/local, legacy/host-bound/policy), `unused_grant` release | `test_kanban_hub_admission_scopes.py`, `test_kanban_sync_client.py` |
| Turns and `prompt.admit` outlast a claim's sample (no 5035), waiting turns make claims defer | `test_kanban_hub_admission_turns.py`, `tests/tui_gateway/test_prompt_admission_busy_lock.py`, `test_admission_fence.py` |
| Reporter/advisory budget, root key known and off by default | `test_hub_cpu_gate_budget.py` |

Server implementation/evidence and its deployment API are documented in `daveey/hermes-hub:docs/worker-first-v1.md`. Server integration tests execute real HTTP requests against Cloudflare's SQLite Durable Object test runtime; Python fake-service tests separately prove client sequencing and do not substitute for server atomicity tests.
