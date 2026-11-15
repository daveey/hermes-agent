# Completion artifact surface increment (unshipped)

The existing `kanban_complete` tool authenticates ownership/run identity, checks
foreign ownership, review and goal gates, then calls `stage_completion` before
`complete_task` enters the outer source receipt preflight. Only an already
registered source with a caller-bound run is staged. No shell mutation exception,
new model tool, inferred caller identity, or protocol advertisement is added.

Explicit completion paths are replaced with the committed selected snapshots.
The journal-backed original→snapshot mapping is reused by prose discovery and
metadata preservation, preventing a second attachment/copy if the original is
also cited in handoff metadata or prose. Receipt evidence identities remain
unchanged. A refused/rolled-back ending keeps the committed bytes and rows for
retry. Explicit `artifacts=[]` clears the selection; absence leaves it alone.

Verification in this increment:
- Missing helper: 3 test failures before implementation.
- Real completion handler + real receipt decorator: owner control failed before
  the call-site integration; foreign/unbound/stale controls remain refused.
- Final selected four suites: 108 passed, 0 failed. Includes managed-scratch
  snapshot reuse, original mutation after staging, prose/metadata/receipt reuse,
  ending rollback and empty selection. Git diff check passed.
- Independent incremental static review found lost mapping and empty-selection
  ambiguity. Both corrected with regression coverage. This is not independent
  exact-head shipping approval; exhaustive per-condition mutants remain owed.

Limits: source registration still must already exist. Other ending surfaces and
mid-run selection/registration remain unfinished; the terminal Kanban mutation
fence must stay intact. No actual successful managed-worker completion or HTTP
artifact transport is claimed by these fixtures: the tool test stops in a
controlled ending callback after exercising the real outer receipt decorator.
Nonselected metadata-only files retain legacy best-effort preservation. Top-level
artifacts retain their existing merge-with-metadata contract; an empty top-level
list does not override a simultaneously supplied nonempty metadata list.

Remaining full-card acceptance is unchanged: prelaunch download, shared absolute
transport deadline, unavailable-vs-mismatch retry, move/reclaim, publication
selection fence after I/O, published attach/ending repository identity, old-Board
race-safe pending reconciliation, authorized registration/selection surface and
clone guidance, environment/lane controls, composed HTTP/fresh-process/managed
launch proof, condition mutants, independent exact-head shipping review, canonical
PR/CI/landing, installed rollout and two-worker proof. Advertisement remains 1.
