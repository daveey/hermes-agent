# Ready-floor type-key counter-analysis

The `drop-type-key` mutation replaces `(name, type(value), value)` with
`(name, object, value)` in `EventHistory.ready_floor`. It is behaviorally equivalent
for the current production scalar run snapshots, not a missing float-output test.
No production change is proposed here.

## Why equal int/float snapshots cannot change the floor

The cache admits only exact `str`, `int`, `float`, `bytes` and `NoneType` values.
Booleans, subclasses and mutable metadata take the uncached fallback. Among
admitted values the cross-type equality relevant here is int/float equality.

The reduction has just two run-dependent operations:

* `unused_grant_run_ids` groups by run ID using normal dictionary/set equality,
  so equal numeric IDs already identify the same events. Its classifier tests
  status/outcome against strings, checks timestamps/worker fields for `None`,
  tests summary/error truthiness, and parses metadata. Equal int/float values
  give the same answers: numeric metadata is not the required JSON object.
  The `unused_grant is True` predicate applies to parsed metadata, not an admitted
  bool scalar in the cache key.
* `_last_run_end_ts` passes each end through `kanban_db._to_epoch` (numeric values
  become `int`) and explicitly returns `int(max(...))`. Equal finite numeric ends
  therefore yield the same value **and type**. Non-finite floats do not equal any
  integer; changing them into a different type cannot create this collision.

The event snapshot is unchanged while run snapshots change. Event-derived
candidates and the filtered event set consequently remain unchanged too. The
source comment claiming `100` versus `100.0` would change the floor type is
inaccurate for this reduction. Keeping that comment/key unchanged avoids turning
this investigation into a product optimization.

## Consumer boundary

`EventHistory.ready_floor` is called by `_last_ready_entry_ts`. Its production
callers are the stranded-ready and assigned-ready diagnostic rules in
`kanban_diagnostics.py`, and `kanban_parent_unclaimed.ready_since`. The latter
passes an ordinary event list and additionally casts the result to `int`; it
does not use this cache. No production reader inspects the private cache key.
Thus skipping a recomputation for equal int/float snapshots does not change a
production result. This is not a promise about future reducers or arbitrary
custom objects, nor a recommendation to remove the type key.

## Executable evidence

`test_ready_floor_equal_numeric_snapshots_are_behaviorally_equivalent` in
`tests/plugins/test_kanban_history_ready_reductions.py` exercises all eleven
run fields read by the reduction, at zero, matching run ID and a later timestamp,
with ordinary and unused-grant runs. Each case reuses one EventHistory through
int → float → int snapshots, comparing its floor value and exact type to both a
cold EventHistory and the uncached list path, and compares visible diagnostics.

At base `eb7c3bfb240cc839ba68267e48f2042d6fe87d51`:

| Variant | Result | Skips |
| --- | --- | --- |
| Unmodified production code, five history suites | 132 passed | 0 |
| Exact `drop-type-key` mutation, same five suites | 132 passed | 0 |
| Positive control: `_last_run_end_ts` returns `float(max(...))` instead of `int(max(...))`; new matrix only | 58 assertion failures, 8 passed | 0 |

The positive control fails the exact output-type assertion, demonstrating that
the test observes type behavior rather than just comparing numerically equal
outputs. It is **not** a claimed kill of `drop-type-key`. All mutations were
restored; the committed change contains tests and this analysis only.

Run with `HERMES_TEST_FILE_RETRIES=0 bash scripts/run_tests.sh` and these files:

* `tests/plugins/test_kanban_board_history.py`
* `tests/plugins/test_kanban_event_marks.py`
* `tests/plugins/test_kanban_history_ready_reductions.py`
* `tests/plugins/test_kanban_history_reductions.py`
* `tests/plugins/test_kanban_run_history.py`

Use a separate `--junitxml` path for each file when collecting skip counts: the
parallel runner forwards a shared path to every subprocess, so a shared XML path
contains only the last writer's results. The positive control can be selected
with `-k equal_numeric` on the ready-reductions file. Run mutations only in an
isolated worktree and restore production files before publication.
