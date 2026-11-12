# Execution observations v1 (capacity adapter)

Optional `execution_observations[]` is additive advisory evidence. Existing outcomes,
completion/origin release receipts, admission and structured human requests remain
authoritative and unchanged. Missing optional fields never erase outstanding work.
The producer schema is `schemas/execution-observation-v1.schema.json`; the pure
legacy/freshness adapter is `hermes_cli/execution_observations.py`.

## Identity and validity

`identity` qualifies gateway, profile, kind and work_id. Cards also require board.
Sessions require session or lineage. Preserve an actual run_id or generation; never
invent an attempt from the current fetch. `owner` is qualified accountability, not
proof of process liveness. `source_revision` identifies the observed source (for
example its durable event), not merely the adapter build.

All execution-envelope timestamps are finite nonnegative epoch **seconds**.
Capacity-event fields named `*_ms` are milliseconds and must be explicitly converted.
`observed_at` and `valid_until` belong to the producer. Consumer age is derived;
fetch, copy and file mtime never renew validity. Apply separate existing upper
bounds: sidebar 180 seconds, imported-owner 600 seconds. Validity cannot exceed
any required source's validity. Missing identity/timing, skew or unavailable data
means unknown, not capacity, success, failure or stalled work. Preserve
`last_known_state` separately. Expired evidence is stale and current state unknown.

States: running, queued, waiting, needs_user, done, failed, cancelled, unknown.
Archive, containment and supersession are not delivered success. Stale current
execution evidence does not revoke historical delivery evidence. `last_activity`
and nullable `last_progress` are independent: claim, heartbeat, poll and comment
alone never establish substantive progress. Missing progress does not mean stalled.
A wait, when present, carries qualified target, predicate, since, due_at and owner.

## Diagnostics

Concurrent diagnostics carry `code`, `native_code`, `phase`, `evidence_ref`.
Codes: capacity.memory_pressure, capacity.provider_limit, capacity.placement,
capacity.dependency, capacity.update_drain. Phases: pre_claim, post_claim,
post_start. Provider refusal after process start cannot explain pre-claim waiting.
Dependency-held work is not eligible ready backlog. Unknown coverage must remain
explicit; multiple placement candidates or boards do not multiply shared slots.

## Compatibility and scope

Legacy consumers ignore this optional envelope. `adapt` accepts a legacy record
and returns unknown without fabricating timestamps or losing embedded human
requests/receipts. This adapter does not write outcome_json or change receipt
semantics. Sibling status projectors may reuse this contract independently.

## Tests and delivery state

`tests/hermes_cli/test_execution_observations.py` exercises the pure adapter.
`tests/hermes_cli/test_kanban_capacity_harness.py` exercises actual isolated
four-card dispatch with fake spawn and a dependency control, capped at 120 seconds.
The isolated integration suite exercises persisted evidence through the actual
show tool, CLI JSON/text handler and FastAPI task-detail route, including the
non-ready dependency control. Legacy queue fields remain unchanged; capacity
and execution fields are optional siblings. Dispatch instrumentation runs after
the admission result and does not feed scheduling decisions. Receipt latency
uses matching `worker_bound` events, not submitter `spawned` events, with the
existing event clock's one-second resolution; it is not exact launch timing.

This implementation checkpoint is not delivery: final review, additional
lifecycle coverage, CI, publication/deployment and bounded live readback remain
outstanding. Complete admission coverage is currently unknown, never idle.
