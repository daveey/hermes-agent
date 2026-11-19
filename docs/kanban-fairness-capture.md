# Queue-fairness evidence capture

Measures how long cards wait in `ready` on a live board, to check the dispatcher-fairness targets of
`t_01a1d774f7486668`:

| Gate | Cohort | Threshold |
|---|---|---|
| `p0_median_wait` | ready episodes whose stored priority was 0 on entering `ready` | median ≤ 1800 s |
| `max_wait_while_claims` | every ready episode | none past 7200 s while a claim is witnessed |
| `alarm_wait` | cards whose idempotency key has an alarm prefix (`kanban_admission_score.ALARM_KEY_PREFIXES`) | each ≤ 600 s |
| `owner_wait` | cards that held, or were next for, a land turn while ready | each ≤ 600 s |

It is evidence tooling only. It changes no priority, floor or aging rule, nothing in the dispatcher reads it,
and it does nothing until an operator arms it. This is the first of two phases: comparing the old and the new
scoring order at each pick is not built here.

## How a capture works

`scripts/kanban_fairness.py capture arm` installs, in one transaction on one Kanban database, two tables
(`kanban_fairness_journal`, `kanban_fairness_session`), a snapshot of every open card, and four
`kanban_fairness_*` triggers. From then on each row version of `tasks` that changes status, priority,
idempotency key, assignee, `ready_since` or lease holder, and each lifecycle event, is appended with the
database host's clock. Writes made by a sync apply are journaled too. No title, body, comment or payload is
stored.

- The journal row is part of the writer's transaction: a rollback removes it, and the writer never touches
  a file.
- The journal is bounded (100,000 rows or 64 MiB of field text). Past the bound the triggers count each
  change they did not store. Any dropped change makes the capture incomplete.
- `capture drain` copies new rows into `segment-NNNNNN.jsonl` files (hashed in `receipts.json`).
  `capture seal` removes the triggers, drains the tail and writes `manifest.json`. `capture clear` drops the
  tables.
- Drains compare the installed trigger SQL with the expected definitions and persist observed loss even
  when there are no new rows. Seal checks inside its write transaction before removing triggers and stores
  that result in the session, so a later drain or repeated seal cannot hide loss. Missing or changed
  triggers, and legacy seals without an integrity receipt, produce `trigger_loss` coverage gaps.
  This detects loss observed by drain or seal, not a trigger removed and identically restored entirely
  between checks; do not repair triggers during a capture or treat such a window as certified.

Which database to arm: a board's own `kanban.db` holds only what that gateway knows. To see every gateway's
cards from one host, arm the board's replica (`--board default --replica`, the `sync-shadow` copy) and state
its worst pull lag with `--apply-lag-s`. The reader widens every time by that lag. A replica that is rebuilt
from a snapshot loses its journal; the next drain records `journal_missing` and the capture is incomplete.

## Reading a capture

`validate --input DIR` lists coverage gaps. `report --mode live --input DIR --output live.json` writes the
cohorts and gates. Every wait is an interval `[lower, upper]`:

- start: the Board's `ready_since` when the row has one, else the sighting of the ready row (minus the lag);
- claim: the `claimed` event's second when journaled, else the sighting of the `running` row;
- a card still ready at the close has no upper bound;
- a card that left `ready` without a claim (blocked, archived, parked) has no upper bound either, unless the
  capture was armed with `--unclaimed-exit elapsed_ready`. That choice is fixed at arm time and printed in
  the report;
- `--clock-error-s` (default 2) widens both ends.

A gate is PASS only when its upper bound meets the threshold, FAIL only when its lower bound breaks it, and
INDETERMINATE otherwise: an empty cohort, an unbounded episode that decides the result, no witnessed claim
(for the max gate), a window shorter than `--min-window-s`, or any coverage gap. A proven FAIL stands on an
incomplete capture; a PASS does not. No episode is removed from a cohort.

Exit codes: 0 all gates PASS, 1 a gate FAILED, 2 INDETERMINATE or incomplete, 3 tool error.

## Owner history

The owner gate needs `owner_authority.jsonl` in the capture directory: `interval` lines (a card held or was
next for a land turn from `start_s` to `end_s`) and `coverage` lines (the journal vouches for `core-lane` or
`state-turn` between two times; `complete: false` marks a chain break). Without the file, or where coverage
does not span the whole window for both sources, the owner gate is INDETERMINATE. The file is produced by
the turn-urgency observer's owner journal in the state repository; this repository only reads it.

## Inventories

`inventory scan --tree NAME=PATH ...` lists every line in the named trees that sets a flat top priority
(`priority=1000`, `"priority": 1000`, `p1000`) or mentions an alarm key prefix (the three floored ones plus
`worker-resources-unknown:` and `turn-urgency-health:` as candidates), and hashes each tree.
`inventory check --scan S --classification C` needs one row per match
(`{"id", "family", "disposition", "note"}` under `producers` / `alarms`); an unclassified match or a row
whose match is gone exits 2. Skill trees differ per executor: scan on each one, check once per tree hash.

## A measurement window

```bash
OUT=~/.hermes/kanban/evidence/fairness-150min
scripts/hermes-python scripts/kanban_fairness.py capture arm --out "$OUT" --capture-id fairness-150min \
    --board default --replica --apply-lag-s 60
# ... every 10-15 minutes, and at the end of the window:
scripts/hermes-python scripts/kanban_fairness.py capture drain --out "$OUT"
scripts/hermes-python scripts/kanban_fairness.py capture seal --out "$OUT"
scripts/hermes-python scripts/kanban_fairness.py report --mode live --input "$OUT" --output "$OUT/live.json" \
    --min-window-s 9000
scripts/hermes-python scripts/kanban_fairness.py capture clear --out "$OUT"
```

`--apply-lag-s 60` is an example, not a measured value: use the replica's observed worst pull interval for
the window.

## Limits

- One database per capture. Which executors served the queue, and at which revisions, is not journaled; put
  the roster in `plan.json` (`"roster": [...]`) before sealing if the report should carry it.
- Capacity can make a threshold unreachable in a given window; the report says what happened, not why.
- Trigger cost on a busy board has not been measured.
