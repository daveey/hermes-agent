# Dispatcher candidate refusal receipts

`hermes_cli.kanban_candidate_receipts` observes dispatcher decisions without
changing eligibility, priority, scoring or the number of ranking/graph queries.
It records the first refusal actually reached for an unclaimed **ready** card,
including pool pickup candidates that are not pinned to this executor.
Successful claims suppress earlier observations in the same pass. Review cards
are not refusal records in this version.

## Read/export for Phase B

Run in the installed Hermes environment, on each executor holding the evidence:

```sh
python -m hermes_cli.kanban_candidate_receipts --db ~/.hermes/kanban.db --limit 1000
python -m hermes_cli.kanban_candidate_receipts --db ~/.hermes/kanban.db --task-id TASK_ID
```

This opens SQLite with `mode=ro`. The Python API is `read(conn, task_id=None,
limit=1000)`. Output is a version-1 JSON object with `receipts` and `passes` arrays
(newest first). Tables are machine-local, deliberately outside task events and
the sync feed; gather from compatible executors rather than only the hub replica.
An absent table yields empty arrays, not fabricated decisions.

Each receipt has `pass_id`, `worker` (registered gateway ID, or hostname on an
unsynced board), `pass_time_ms` (UTC Unix epoch milliseconds), `task_id`, `score`,
`score_status`, `gate`, `reason`, and `stage` (`global`, `pickup`, `screen`,
`eligibility`, `claim`). Gates are `prefilter`, `labels`, `capability`,
`budget/pacing`, `capacity`, `provenance`, `containment`; an unclassified claim
refusal is explicitly `unknown`. `reason` preserves the actual gate detail.

`score` is the total already computed by the existing admission ranker. It is
null with `score_status=not_observed` when a gate precedes ranking, ranking fell
back, or the bounded score observation window was full. Receipt collection does
**not** recompute a score. A global capacity refusal and a full pickup window
can precede compatibility/policy checks: the receipt names that first reached
gate, not a hypothetical later decision. Missing data never certifies eligibility.

## Bounds and gaps

Each pass retains at most 512 distinct refused candidates and 512 score samples.
`passes.dropped` counts refused observations beyond that window (not unique
unseen cards). Detail strings are capped at 400 characters. Every successful
non-dry pass trims the journal to seven days, 20,000 refusal rows and 2,000 pass
rows. Readers default to 1,000 rows per array; request a larger limit (maximum
20,000) for collection. Pass metadata may age out before a remaining receipt;
there is no completeness guarantee across retention or reader limits. SQLite
reuses freed pages; this bounds logical retained data, not historical file size.
Persistence errors warn without stopping scheduling. Dry runs write nothing.

## Replay

`test_kanban_candidate_receipts.py::test_replay_unknown_shapes` freezes the two
Phase A shapes: a macOS label refusal and a pool/coach card compatible by labels
but rejected by a missing-profile prefilter. It asserts the worker, common pass
time, observed score and first refusing gate survive export. This is a fixture
proof, **not** a claim to recover the unrecorded historical reasons for the two
original cards. The integration tests exercise non-pinned pickup, real combined
lane dispatch, capacity tails, claim suppression and capped local ready queues.
