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
any required source's validity. Each required source may specify its original
`source_class`; absent that field it inherits the envelope's class, never the
consumer's fetch age. Its own observed time plus that class's TTL also bounds
validity. Missing owner or nonempty evidence references, invalid attempt identity,
missing identity/timing, skew or unavailable data
means unknown, not capacity, success, failure or stalled work. Preserve
`last_known_state` separately. Expired evidence is stale and current state unknown.

States: running, queued, waiting, needs_user, done, failed, cancelled, unknown.
Archive, containment and supersession are not delivered success. Stale current
execution evidence does not revoke historical delivery evidence. `last_activity`
and nullable `last_progress` are independent: claim, heartbeat, poll and comment
alone never establish substantive progress. Missing progress does not mean stalled.
A wait, when present, carries qualified target, predicate, since, due_at and owner.

## Work projection hardening (additive; adapter semantics unchanged)

`hermes_cli/execution_observations.py` (`project`, `select`, `summarize`) and
`apps/desktop/src/lib/execution-observations.ts` (`projectExecutionObservation`,
`selectExecutionObservation`, `summarizeExecutionObservations`) are pure and share
`schemas/fixtures/execution-observations-v1.json`; both test suites load that one
file, so verdicts live only there. `adapt` keeps its published behavior and existing
tests; it additionally rejects non-string/bool/array values instead of raising,
and treats `version: true` as unsupported.

- **Attempt identity**: `run_id`/`generation` is an integral JSON number in
  1..9007199254740991 (7.0 is the integer 7, never "7") or a nonblank identifier
  string. Booleans, fractions, unsafe integers, arrays are invalid. Identifier
  strings (identity, owner, wait target) must be nonempty with no leading/trailing
  whitespace; whitespace set = union of Python `str.isspace` and JS `trim`.
- **TTL lookup** is own-property and string-typed first. A required source's
  `source_class` absent inherits the envelope; explicit null/array/unknown/inherited
  names are invalid. `required_sources: null` is invalid.
- `unknown_reason` is kept only when a nonempty string, else `source_unknown`.
- **Human requests**: `resolved_check`/`resolved_check_every` are stripped from
  `human_request` and each `retained_human_requests` entry on projector entry (input
  untouched), before validation and output; all other fields and expired asks are retained. Visible semantics (five text fields,
  interaction set, 2-6 distinct `{label<=60, value<=120}` choices by code point) are
  implemented identically in both languages. `needs_user` with a `time`,
  `dependency` or `transient` wait is unknown (`invalid_human_wait`).
  `summarize(...).outstanding_human_requests` counts retained valid requests
  regardless of freshness or state (a request with such a nonhuman wait is not valid).
- **Progress** needs an explicit successful, nonblank referenced evidence record and
  `last_progress <= observed_at` and `<= now`; kinds are never coerced.
- **last_known_state** is a known non-unknown state or null, and `done` survives only
  with a matching verified `delivered` receipt (also for stale and legacy input). The
  verified historical receipt itself is retained through expiration.
- **select** matches the exact normalized identity key, ignores malformed entries,
  is independent of input order, and returns unknown for invalid candidate timing, a
  future-dated winner, different sources, or conflicting same-time records, using
  JSON equality (bool is not a number; 7 equals 7.0).
- TS output is a deep copy (own keys defined as data, so `__proto__` is inert).

### Unknown selection preserves outstanding evidence

When `select` has exact identity-matched candidates but its result is `unknown`, it
projects each matched candidate before collecting evidence. Conflicts, invalid timing,
future-dated winners and expiration do not resolve an outstanding human request or
revoke verified historical delivery. The additive result fields are:

- `retained_human_requests`: every distinct valid visible request, including previously
  retained requests. Singular requests accompanied by nonhuman waits are excluded.
- `historical_delivery_receipts`: every distinct verified `delivered` receipt whose
  identity matches the selected execution, including previously historical receipts.

Both arrays use structural JSON-semantic deduplication, independent of object property
order (7 and 7.0 coincide; booleans, missing/null and array order remain distinct).
They have a canonical typed structural ordering, using code-point string encoding and
IEEE-754 number bits with zero normalized. This is deterministic, **not** an ordering
of authority, chronology, request resolution or execution outcomes. Only exact matched
candidates contribute: another gateway, profile, board, run or generation is fenced out.

A valid already-selected singular `human_request` / `delivery_receipt` stays selected.
Otherwise a unique retained value also fills its singular field; multiple distinct values
stay in the arrays without inventing a winner. Matching verified historical receipts
support `last_known_state: done`, but **never** change `state: unknown` to current success.
Re-projection preserves these arrays and strips their private request probes again.
Summarization counts the union of valid retained and singular requests **per observation**
without double counting, while work state counts remain one per observation. Historical
receipts are not additional completed work.

This change does not wire a consumer. Two review notes remain explicit future design:
expected-identity mismatch output retains its existing shape, and no-match selection
retains its existing reason rather than adding a new no-match reason.

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

The task-detail `capacity_evidence` schema is
`schemas/kanban-capacity-evidence-v1.schema.json`. Observation storage holds one
bounded, local latest-dispatch snapshot, not replicated admission authority.
Board identity comes from the actual connection, including pinned DBs; an
explicit foreign read remains unknown. Changes to assignee, placement labels,
attempt or parent state invalidate the previous observation. Reads never write.
The 128-task bound exposes truncated coverage instead of inventing a complete
fleet census. Native unmapped restrictions remain explicit alongside namespaced
reasons; notes include the causal phase.

`kanban_capacity_metrics.attempts` consumes ordered durable event histories.
Missing ready transitions stay missing: created time is never substituted, and
native retry events without an explicit ready status cannot establish readiness.
Its summary preserves samples and censored attempts; p50 needs two samples and
p95 needs twenty. `idle_coverage` unions shared machine/slot-pool intervals across
boards. Only same-interval fresh complete backlog/slot/restriction coverage can
produce unexplained idle slot-seconds, capped at the lesser of eligible backlog
and admissible slots. Backlog is a deduplicated whole-slot-pool count, never a
per-board sum; disagreeing pool counts make coverage unknown. Unknown coverage is machine-seconds, not
invented unknown slot-seconds. Current dispatch snapshots deliberately leave
admissible slot counts unknown; configured limits are not measured free slots.

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
