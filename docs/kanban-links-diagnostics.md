# Link reconciliation diagnostic counters

These counters do not change cache policy, TTL, skip admission, projection order,
or failure/retry behavior. They are exported by existing loop-accounting snapshots.

## Reading the counters

- `sync.links.board.<slug>.work`: inclusive CPU/wall per board, including borrow,
  input collection, replay, and reconciliation. The existing `sync.links.board`
  remains its aggregate, **not an additional cost**.
- `sync.links.board.<slug>.reason.{skip,missing,changed,degraded,chat-replay,age}`:
  one zero-CPU decision event for an unchanged-enabled board check. `degraded`
  means unknown required inputs or failed replay; `chat-replay` means changed
  answers. `age` applies only to the existing uncertified/legacy max-age fallback.
- `sync.links.board.<slug>.changed.{identity,revision,replica,ledger,mode,signature}`:
  overlapping component changes, compared from the already collected input tuple.
  No extra store probes. A changed replica stat does not itself prove business DML.
- `.certificate.degraded`: a completed reconciliation whose chat reads could not
  be certified. This is an **outcome**, not a second decision. Existing age retry
  remains in effect. Missing whole input keys still publish no completed memo.
- `links_strict.hit`, `links_strict.miss.{missing,inputs,age,age_inputs}`:
  the final strict lookup decision, including the locked double-check. These are
  distinct from background refresh checks. A hit after another thread publishes
  does not count as a strict publication.
- `links_strict.publish` and `links_refresh.publish`: count the actual copy-on-
  publish assignment, not just a build attempt. Failed projection publishes none.
- `links_{strict,refresh,display,other}.projection`: CPU of the actual projection;
  `links_{strict,refresh}.fingerprint`: CPU of fingerprint collection.
  `sync.links.ledger_slices`: CPU of slicing the returned cards, separately from
  strict fingerprint/projection and the existing inclusive `sync.links.sources`.
  `links_strict` includes strict callers other than sync; it is not automatically
  a subset of `sync.links.sources` across a whole process. Background costs are on
  their worker thread and must not be charged as sync-thread CPU.
- Existing `links_refresh.{skip,cold,inputs,age,age_inputs,build,changed.*}` retain
  their definitions and distinguish wrapper checks from builds.
- `links_display.stale`: the warm stale path still avoids fingerprints/locks.

At most 16 board slugs of at most 80 characters retain names per process; other
boards fold into `other`, without evicting earlier identities. Event reason and
component suffixes are fixed enums. The existing global accounting cap also applies.

## Observation bracket

`{sync.links,links_strict,links_display,links_refresh}.{enter,exit}` are zero-CPU
start/end events, including exception exits. Their cumulative difference is the
number in flight. An endpoint with unequal counters can straddle an unfinished
pass whose CPU will only publish at its exit; report that contamination rather
than selecting a later passing interval. Background enter occurs after lock and
scope validation, so queued work additionally requires the worker's existing busy
state if relevant. These counters are not agent-activity measurements.

Before selecting the first newly published accounting snapshot, start read-only
polling of the gateway runtime status and record its activity throughout. Preserve
all raw snapshots and activity polls. Require one complete >=900-second window,
>=3000-second gateway uptime at its start, and stable PID, OS birth, loaded SHA.
Retain interruptions, non-idle observations and in-flight endpoint uncertainty;
do not slide the interval until it passes. Use thread-CPU deltas from `sync.links`
for percent of one core (`100 * cpu_delta / wall_delta`), not process CPU or the
sum of nested spans. An active window remains useful attribution but is not idle
budget acceptance. Name the dominant measured component and estimate any proposed
saving from its whole-window contribution; conditional per-miss means alone are
not a full-window saving. No fix is implied by these diagnostics.
