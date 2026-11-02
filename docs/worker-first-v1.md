# Worker-first portable cards (opt-in, disabled by default)

`kanban.placement.worker_pools` maps an origin Board machine name to worker names, for example `{hub: [worker-1]}`. This is a pool, **not** an AND-list of `machine:*` labels. The default `placement: {}` is unchanged. `heavy_route` remains the legacy behavior only when the origin has no pool entry. A configured empty/unavailable pool queues portable work; it never overflows onto the hub.

## Creation and schema

Tool and CLI creation enroll portable light and heavy cards. The shared conservative `portable_reason` preserves scratch/default-board, assignee, carried-option, host/local/skill marker, project and gateway-requirement exclusions. Caller placement and `runs_on` (including `[]`) bypass automatic enrollment. Courier/origin creation keeps its original path. Parent and chat links are not replaced.

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
| Whole-host occupancy, malformed/unreadable fail-closed samples, reporter compatibility | `test_machine_occupancy.py`, `test_machine_metrics.py` |
| Local pressure/capacity refusal releases unused lease, ready and review | `test_kanban_worker_policy_db.py::test_worker_local_admission_rechecks_load_and_releases_unused_lease`, `test_admission_sampling_failure_releases_grant` |
| Real concurrent multi-origin reservations; unauthorized candidates/CAS; expiry/replay/recovery | server `worker/test/worker-placement.test.ts` |
| Numeric validity/freshness, score normalization, deterministic N-worker selection/ties | server `worker/test/worker-selection.test.ts` |
| Two-worker ready rebalance, immovable running lease, capabilities/pickup at claim | server `worker/test/worker-placement.test.ts` |
| Same-card review continuity, forged review-pin refusal, reserved-capacity exclusion | server `worker/test/worker-placement.test.ts` |
| Operator settings, schema, rollback retains fences/leases and explicit CAS repin | server `worker/test/machines.test.ts`, `worker-placement.test.ts` |

Server implementation/evidence and its deployment API are documented in `daveey/hermes-hub:docs/worker-first-v1.md`. Server integration tests execute real HTTP requests against Cloudflare's SQLite Durable Object test runtime; Python fake-service tests separately prove client sequencing and do not substitute for server atomicity tests.
