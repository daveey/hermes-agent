# Hub admission v2

Status: approved design, implementation in progress. This document describes the
full contract, not a claim that the current branch implements it all.

Implemented so far: DB create/readback metadata and transactional postimage
validation on edit; CLI create/edit `--class` and `--frees`; create tool schema,
pre-placement validation, persistence/readback and remote option encoding.
Admission metadata edits may be combined with ordinary title/body/priority edits,
but not operator fields or completion-contract edits (separate transactions).
Local run pool/class receipts now survive task class edits. The whole-host census
can count those receipts, and opt-in pre-lease/final CPU gates select the separate
priority pool. Ready and review claim tests admit urgent work at 70% CPU with seven
regular slots full. These are claim-path tests, not dispatcher or live proof.

Still pending: origin receipts/cap, dispatcher budget and queue notes, Board and
courier receiver compatibility, full malformed-config/receipt tests, sync replay
coverage, guard mutations, scoring, reporting and live activation. Do not enable
this incomplete implementation. `priority_slots` is currently opt-in (absent = 0)
until the full Board-compatible rollout is ready; the enabled-hub target is 3.

## Policy and rollout

Ship phase 1 (separate pools) first, then phase 2 (scoring, explanation and effect
reporting). The final slot amendment supersedes the original reserved-slot design:

- `kanban.max_in_progress` remains the regular pool, configured as 7 on the hub.
- `kanban.admission.priority_slots` is a separate pool, default 3 on an enabled hub.
- Only `capacity` and `urgent` cards may enter the priority pool. Numeric priority
  never grants eligibility. Marked cards prefer priority, then regular capacity.
- An idle priority slot never falls through to normal work. An eighth normal card
  waits even when all three priority slots are idle.
- Priority claims require CPU busy strictly below
  `kanban.admission.priority_cpu_busy_below_pct` (default 85). Regular claims retain
  the installation-root `cpu_busy_below_pct` policy (50 on the hub).
- Activation is hub-only, tied to the existing installation-root CPU admission
  policy; workers with that policy off retain existing behavior. Invalid root
  settings fail closed; a profile override cannot increase host capacity.
- Critical memory pressure admits none. Elevated pressure allows at most one new
  run per dispatcher tick across both pools, not one per pool. Existing drain,
  update, turn-waiter, lease expiry and lock-budget fences remain intact.

No full-pool default changes apply to workers. A higher CPU ceiling does not
change placement or allow portable work to ignore worker-first routing.

## Metadata

Tasks gain synced fields:

| Field | Contract |
| --- | --- |
| `admission_class` | `normal` (default), `capacity`, or `urgent` |
| `admission_frees` | Capacity requires a nonempty single-line `frees: ...` claim |

Expose these through `kanban_create`, CLI create/edit (`--class`, `--frees`),
normal task readback and detector producers. Validate at the shared database
boundary, not only argparse/tool schemas. Editing class or frees validates the
resulting pair within the same write transaction. Existing cards migrate to
normal; no title-based automatic upgrades.

Runs gain a local, immutable admission receipt: selected pool (`regular` or
`priority`), claim-time class and origin key. Keep it separate from mutable
completion metadata. NULL legacy receipts count as regular. Class edits must not
move an active run between pools or erase its capacity-origin charge.

Capacity-origin limit: at most two active capacity runs per originating chat
across all host execution boards. Prefer the original creator chat tuple
(gateway, profile, session), not a shepherd or a current worker session. Without
one, fall back to (creating gateway, created_by), with missing identity sharing a
conservative unknown bucket. Urgent runs do not count against the capacity-only
origin limit, but still consume one of the two pools.

## Authorities and integration

The installation-root `admission.lock` is the whole-host execution authority.
Inside its existing bounded transaction:

1. Read current task class and origin.
2. Count immutable run receipts across execution boards, deduplicating file
   identities and excluding remote-held replicas.
3. Exclude only the exact proven unused Board-grant echo, using the existing
   task/run UID, holder and claim-lock proof.
4. Select an eligible pool and enforce origin concurrency.
5. Sample CPU independently under the lock; recount before commit and refuse
   expired leases or an exceeded lock hold budget.
6. Persist the selected receipt with the claimed run and event before releasing
   the lock. Claimed evidence includes both pool occupancies and CPU ceiling.

The dispatcher budget and pre-lease advisory must use the same pool policy so
neither an early seven-slot cap nor the 50% advisory prevents eligible priority
work from reaching final admission. Advisory snapshots never grant authority.

The Board service is a separate integration boundary. Its lease cap and
worker-first overflow/review gates currently use a scalar machine cap and CPU
ceiling. They must understand the new eligibility policy before enabling the
extension. A Board lease is a reservation, not proof an interpreter started.
Board Durable Objects serialize per board, not across boards; they do not replace
the local host lock. Preserve existing total-cap behavior for unenabled machines.
Task metadata must round-trip through Board migrations/field allowlists; local
run receipts must not be overwritten by a later feed or placeholder adoption.

Deployment order: compatible Board schema/policy first, compatible core next,
then enable and verify hub configuration. No unsupported live SQL backfill and no
forced updater restart. Keep legacy reads conservative during the transition.

## Phase 2: deterministic score

For eligible hub-local ready/review candidates:

```
score = min(effective_priority, 1000)
      + 500 * class_rank
      + 25 * min(unique_open_descendants, 20)
      + 25 * min(floor(ready_minutes / 10), 60)
      - 100 * consecutive_failures
```

`class_rank` is normal=0, capacity=1, urgent=2. Defaults live under
`kanban.admission.weights` (`class`, `unblock`, `age`, `failure`). Validate finite,
nonnegative numeric weights. Effective priority retains existing transitive
priority donation. Descendants are unique, cycle-safe and stop at terminal nodes.
Aging uses the current ready/review interval, not creation age or prior run wait;
its maximum contribution is 1500 after ten hours. Ties use readiness timestamp,
then task ID. Scoring is read-only and never rewrites stored priorities.

Score selects order only; it cannot bypass placement, either pool cap, CPU,
memory, origin cap or update holds. Workers keep plain dispatch order.

`queue_note` and `hermes kanban queue --explain ID` show component values, position,
placement, current class, applicable refusal and `regular N/7, priority M/3`.
Ready-wait notices recommend class/placement corrections, never priority bidding.

## Effect reporting

A weekly hub report lists capacity claims with their stated frees metric,
claim-time and post-completion observations, ready-wait p50 and worker free slots.
Unknown/missing/incomparable data is explicitly unknown, not a success or failure.
Before implementation of automatic class loss, encode and test a deterministic
repeated-no-effect policy using comparable observations; retain auditable origin,
measurement window and evidence. Do not demote creators merely because unrelated
queue load rose. Reporting must run on the hub, not a sleeping Mac.

## Acceptance

All original CPU/fence/grant-echo tests remain. Add cases to
`tests/hermes_cli/test_kanban_hub_admission.py` proving:

- marked claim with seven regular runs active;
- fourth marked claim falls back to a free regular slot or waits;
- idle priority pool never admits an eighth normal card;
- numeric priority alone never qualifies;
- exact 85% boundary, unknown CPU, critical/elevated memory and unchanged workers;
- ready/review races across boards, own-grant echo and immutable receipt accounting;
- capacity origin cap survives active task class edits;
- aging, descendants, score breakdown and explain output.

Per-guard mutation tests and read-only old/new replay across real boards and
sync-shadow inputs accompany broader classifiers. Require independent exact-head
review and green required CI through `hermes-core-wt ship` for each phase.

After natural hub installation, class the designated capacity work through the
supported edit path; read back a claimed event in the priority pool while regular
occupancy is seven. Record ready-wait p50 before and after with sample counts and
windows. If original target cards have finished, preserve that history and use
remaining real capacity work, documenting the substitution rather than inventing
a live claim. Final handoff is review, not completion before live proof.
