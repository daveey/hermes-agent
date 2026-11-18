# Workspace source v2 artifacts

## Current activation contract

Enabled dispatchers advertise source protocol 2 after the v2 consumer integration;
CLI claims, configured-off hosts and a process that detected an unsupported Board
do not advertise it. Existing v1 manifest/attachment protocol defaults remain v1.
This capability advertisement does not enable `kanban.workspace_source` by default.

Live rollout still requires two upgraded installed workers, a compatible deployed
Board, registered clean clones, task-owned branches and verified Git/GitHub access.
The first v2 custody row is the rollback boundary: preserve upgraded recovery
capacity and repair forward; never downgrade/delete custody or strip artifacts.
Advertisement alone does not prove live two-worker transfer acceptance.

## Historical incremental implementation notes

The remaining notes record intermediate spike stages, not the current integration
status or a new requirement to repeat the stopped mutation campaign.

`kanban_source_artifacts.stage(conn, task_id, paths, board=..., run_uid=...)`
records an explicitly selected set, including an intentional empty set. It is an
internal helper: the future CLI/ending caller must authorize the live source run.
There is no automatic discovery of all attachments or scratch files.

## Durable producer

- Snapshot bounded regular-file bytes, reject denylisted names and credentials,
  then hash the copied bytes. Deterministic destination is outside scratch under
  the ordinary attachment root. Existing fsynced atomic-file writer is reused.
- One local write transaction inserts ordinary `task_attachments`, a dedup journal
  keyed by task/resolved path/copied SHA256, and the explicit selection keyed by
  task/run UID. No additional source custody authority is created.
- An interrupted transaction leaves no attachment row/selection. It can leave a
  deterministic orphan file, reused on retry; rollback never deletes another
  caller's committed bytes. Changed bytes receive a new attachment UID; previous
  rows remain intact. Duplicate basenames from distinct paths remain distinct.
- Selection is run-bound so a returning machine cannot silently substitute an old
  selection for a newer incoming manifest. Explicit same-run empty selection is
  distinct from no selection. Premanifest resume inherits only the exact verified
  predecessor's selection inside the same live-run-checked observation transaction.
  An already selected successor set (including []) is never overwritten on replay.
  Missing selected attachment metadata rolls the observation back and becomes a
  typed AttachRefused, so dispatcher release handling remains in control.

## Transport and publication

`portable` pushes normal attachment metadata and calls `ensure`, which uses the
existing attachment upload/download implementation. Only selected descriptors are
accepted, with task/UID/filename/digest readback. Upload pins the expected digest
against the exact byte buffer passed to the existing blob API. Checking a separate
read before calling the transport is insufficient: a reproduced two-read race
uploaded changed bytes before the expected-digest transport check was added.

Both publisher and ending-preflight unchanged shortcuts now compare selected
artifacts. Artifact-only changes get a new manifest revision, not a false unchanged
receipt. Missing selected bytes retain custody. First scratch publication keeps
`scratch_external`/version2; artifact-bearing worktree manifests use version2 and
never downgrade a prior v2 manifest.

Selection staging (completion, review and heartbeat `artifacts`) requires a
protocol-2 run (`source_protocol >= 2`). Protocol-1 runs, i.e. every existing v1
worktree card, keep ordinary attachment preservation: no snapshot scan, no
selection, no v2 artifact manifest. `kanban_heartbeat(artifacts=...)` on such a
run is refused; `kanban_complete`/`kanban_request_review(artifacts=...)` fall
through to the legacy path.

## Evidence and limitations

Canonical tests: new staging18 + publisher3, existing transport16 + publisher78 +
receipt14 = 129 passed, zero failed. Initial staging15 RED (missing module), three
publication RED, then explicit upload-read race RED before its fix. Removing each
unchanged comparison separately is assertion-killed; restored publisher tests pass.
Raw logs live in the hub spike evidence/core-artifact-staging directory.

Tests use actual SQLite transactions, files, sync row transport and real Git, with
fake Board/GitHub endpoints. Reopened SQLite connection is not a fresh interpreter
proof. No live upload, PR, deploy, or protocol2 advertisement occurred.

## Next integration gates (must precede activation)

1. Wire live-holder-authorized staging before outer completion/review receipt
   preflight; reuse staged paths without duplicate completion attachment rows.
   Recheck run identity and selection after asynchronous work, including concurrent
   stage/publish. Current helper is deliberately not an authorization API.
2. Exact-predecessor premanifest inheritance is implemented; verify it through the
   composed managed-worker path. A different host must restore incoming selected
   artifacts before managed worker launch (still pending).
3. Thread a shared absolute preflight deadline into metadata push/blob transport;
   current publisher checks the budget around the call, not inside each transfer.
   Add 60s host retry for unavailable versus mismatch policy, and move/reclaim tests.
4. Finish initial/CLI wiring, published attach and ending repository identity checks,
   M3 old-Board race-safe reconciliation, N4 environment isolation, N6 lane fences.
5. Real composed Board/core artifact HTTP/fresh-process proof, managed-worker launch,
   all-condition mutations, exact-head independent shipment review, canonical ship,
   CI/landing, installed rollout and two-worker proof. Acceptance is unchanged.

## Run and selection fences (still internal)

`stage_for_run(..., expected_run_id=...)` takes the authenticated caller's bound
run ID and verifies the same primary-machine, live, running, attached observation
before snapshot I/O and again in the selection commit transaction. Ended/outcome
rows, expired/nonfinite leases and changed observations are refused. The surface
must authenticate the caller; this helper does not itself authenticate CLI input.

Staging snapshots the original selection and compares it in the final write
transaction, refusing a conflicting writer without replacing its selection or
inserting losing attachment rows. Equivalent selections are semantically equal;
this is not an ABA history/version detector. Portable transport also refuses if
selection changed during metadata/blob I/O, preserving bytes and UIDs for retry.
It does not yet fence the later manifest publication against a subsequent change.

Snapshots are copied one file at a time; only path/digest/size descriptors survive
to the next copy. At most a single snapshot plus an existing-file comparison
buffer are retained, rather than every selected file's contents. A weak-reference
buffer-lifetime test proves release before the next copy; it is not an RSS benchmark.
A second SQLite connection committing during the first caller's snapshot proves
lost-update refusal. Copy failure retains prior metadata; no fresh-process proof
or OS-thread contention stress is claimed by this increment.

Further hardening before shipping: fresh-interpreter and broader filesystem-failure
proofs. Snapshot now refuses
leaf symlinks/special files, scans the filename, opens nonblocking/no-follow where
available, and checks opened device/inode/type against the original lstat before
reading. Parent aliases resolve normally; selecting the real file remains allowed.
POSIX replacement-at-open controls cover FIFO, symlink and regular-file swaps;
NONBLOCK avoids hanging before descriptor validation. Windows behavior is not yet
verified by these POSIX controls. No production caller currently invokes stage,
and protocol advertisement remains1.

Latest incremental regression: eight canonical suites102 passed, zero failed.
Six targeted mutants exit1 after a green final-test baseline; the first leaf guard
mutant was masked by the inode guard until its refusal-reason assertion was made
specific. Successor-overwrite mutant fails on SQLite's uniqueness fence, not an
assertion; this is not exhaustive per-condition shipping proof. Source restored
before the final eight-suite run.
