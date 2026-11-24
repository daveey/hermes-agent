# Terminal-run reservation and cooperative wake servicing

## Problem and evidence

A timed fallback could wake a conditional park while its terminal probe was false,
using the final already-authorized spending run before the actual result arrived.
The original incidents are recorded on t_01a207b55723fdc2 (PR363/PR359 landers).

## Evidenced five-whys

1. Why could the lander not record its terminal outcome? Its spending cap had
   already been reached; existing spend admission correctly refused another run.
2. Why was the last run consumed early? `_wake_parks` admitted its generic timed
   fallback even while the card retained its own false condition. Extracted
   `test_last_budgeted_run_is_reserved_for_own_condition` fails twice on main
   a2e0c77673f3 with an unexpected awakened task, not an import error.
3. Why did the fallback win? No admission reservation distinguished the last
   authorized conditional run from ordinary earlier runs. The new reservation is
   subtractive: it creates no run, refunds no spend and releases no scope hold.
4. Why could an overdue timer wait through another phase? Serial dispatch serviced
   continuations once during reclaim. Injecting time advancement in reclaim,
   maintenance and capacity yields three actual next-phase status failures on
   main. Cooperative checkpoints now service existing timer guards between phases.
5. Why did previous checks miss this? Timer deadline checks alone pass without
   reproducing phase starvation or the final-run precondition. The new integrated
   tests exercise the real dispatcher, original spending accounting, and a
   foreign relayed probe; separate controls retain CI-unstartable repair.

Root (happened): generic timer admission and serial wake service did not preserve
conditional terminal settlement within the existing spending window.
Root (not caught): timer-only and ordinary-budget tests omitted the last-run and
inter-phase boundaries. Tests now cover both boundaries independently.
Recurrence: this is the producer-side recurrence described by the original PR363
and PR359 incidents, not a release of either historical card.

## Contract

Only generic `continuation_woken` admission is reserved, only on the canonical
home, only with exactly one authorized spending run left and no exhausted token
limit or pending spend decision. There must be a real local non-ask probe or a
repairable explicitly timed relayed probe. Terminal, stale, broken, lost-probe and
CI-unstartable outcomes retain their normal repair/settlement route. A reservation
is deduplicated for its park and backs off expired timers by 120 seconds.

Cooperative checkpoints run on the caller's existing serial connection, outside
transactions, at most once per five seconds measured from scan completion. They
never claim/spawn or enlarge capacity. They cannot preempt a blocking phase: this
is bounded checkpoint frequency, **not** a hard wall-clock latency guarantee.
The ready-floor diagnostic now reads only relevant events and released-run
judgments, with equivalence and no-full-history regressions.

## Scope boundaries

No parent-held or wait-cycle producer/rendering modules are included. Those b1/b2
slices were explicitly abandoned by the owning chat after their review stop rule.
No guard cards, existing scope renewals, spending-limit increases, legacy releases,
notification-derived authority, or changes to current flapping-wake recovery.

Focused regression receipts and later exact-head CI/review/installed live proof
belong to the implementation card; this document alone claims none of those gates.
