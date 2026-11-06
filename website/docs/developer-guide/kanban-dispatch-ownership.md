# Per-board dispatch ownership

`dispatch_owner` is optional board metadata for cooperative, same-UID runtime
coordination. It is not a credential, authorization check, or security boundary.
A host must still validate task provenance/generation and authorize its scoped
spawn function. Anyone who can modify these files or execute arbitrary Python
as the same user can change ownership. Do not give task workers a dispatcher ID.

## Operator commands

Stop ALL older dispatchers that can access the board before starting any upgraded
ones or setting ownership. Drain active workers at a safe idle window; do not kill
sessions to activate this feature. Mixed-version operation is unsupported: older
code ignores ownership and may lock an unresolved symlink path while new code
locks the canonical target. Then upgrade every dispatcher and run:

```sh
hermes kanban boards dispatch-owner example dedicated-worker --json
hermes kanban boards dispatch-owner example --json
# Explicitly restore stock dispatcher eligibility:
hermes kanban boards dispatch-owner example --clear --json
```

The command targets the named board's canonical database, not a worker's env pin.
It refuses a busy dispatch lock: retry after that tick completes. It does not
terminate workers already running, start a service, or change profiles. Draining
old workers and upgrading older dispatchers are deployment responsibilities.
Older releases do not enforce this field; writing metadata alone is NOT rollout.

The Python operator API is `hermes_cli.kanban_dispatch_owner.set_dispatch_owner(
db_path, owner)`; pass `None` to remove the key. `get_dispatch_owner(db_path)` is
the strict readback. Both operate on existing on-disk DB paths. Other metadata
edits preserve ownership and serialize with ticks; malformed metadata must be
repaired explicitly rather than silently replaced by display defaults.

## Dedicated runtime

Dedicated runtimes use internal core APIs, not the published plugin API. Plugins
must not import these internals. A same-UID host runtime opens the board with
`hermes_cli.kanban_db_connect.connect_closing(board="example")` and, within that
connection's context, calls `hermes_cli.kanban_db_dispatch.dispatch_once` with
`conn`, `board="example"`, a constant `dispatcher_id="dedicated-worker"`, and
its own `spawn_fn=scoped_spawn`. The context closes the connection after the tick.
This embedding contract is maintained with the core, not as a plugin extension.

The ID is an explicit Python keyword, not an environment variable or a worker
grant. Stock CLI, daemon, and embedded gateway callers omit it and cannot dispatch
owned boards. No plugin monkeypatch or lock bypass is necessary. The dedicated
runtime must supply its own triage decomposition and health telemetry: the stock
gateway excludes owned boards from both, and no CLI/daemon/gateway config grants
an identity implicitly.

## Invariants

* Missing `dispatch_owner` retains stock eligibility. Legacy missing metadata is
  unowned. A present empty/null/non-string/non-slug value is invalid, not cleared.
* Slugs are exact lowercase alphanumeric/hyphen/underscore strings, 1–64 chars,
  beginning with an alphanumeric. No trimming, case folding, or coercion.
* The actual SQLite main database path selects metadata and the dispatch lock;
  an explicit board label or `HERMES_KANBAN_DB` alias cannot select a different
  owner's metadata. Symlinks resolve to their target, independent of the caller's
  home or which metadata files happen to exist. Every DB uses the adjacent
  `<database filename>.dispatch-owner.json` sidecar (JSON object with optional
  `dispatch_owner`). There are no directory-name heuristics. Display/discovery
  `board.json` keeps its original board-local location and is not ownership
  authority; use the operator command/API above for strict owner readback.
  Earlier unreleased candidates stored owners in display metadata; those files
  are not migrated automatically. This feature must not have been activated
  before the final reviewed storage contract.
* Move the ownership sidecar with its DB. Native board archive moves both in the
  same directory rename; an external symlink target and its sidecar stay together
  outside the archive. Recreating the archived slug starts a new unowned DB.
  Export/import intentionally does not copy runtime ownership. Hard-link aliases
  and independently relocated/bind-mounted DB files without their sidecars are
  unsupported; all runtimes must share a canonical DB and its adjacent sidecar.
* Under the same lock, invalid/unreadable metadata or a mismatched identity returns
  `skipped_owned=True` before reconciliation, reclaim, promotion, claim, spawn,
  checkpoint, or dispatch observers. A valid owner appears in `dispatch_owner`;
  read/validation failures appear in `ownership_error`.
* An unavailable lock refuses dispatch and owner changes. This intentionally
  removes the former unguarded fallback even on unowned boards: without the lock
  an owner setter could race an apparently unowned tick. Fix lock permissions
  rather than bypassing it. Skipped-lock ticks do not fire dispatch observers,
  since ownership cannot safely be established without acquiring the lock.
* Owner changes and display metadata edits use atomic replacement under that
  lock. Busy edits fail immediately and should be retried; they do not stall the
  gateway. Removal refuses busy boards before changing the current pointer or
  moving a directory, avoiding partially archived boards on lock contention.
* Gateway auto-decomposition also holds this lock through listing and decomposing
  triage work. Owned/invalid boards neither spend auxiliary LLM calls nor count
  toward the stock gateway's stuck-dispatch health check.
* Dispatch requires a native `kanban_db_connect.connect`/`connect_closing`
  connection. Native open captures device/inode under the lifecycle lock, and
  both tick and gateway pre-work revalidate it under that lock. Archive/delete
  followed by recreating a slug cannot redirect an already-open handle to the
  replacement's owner. Raw SQLite and in-memory handles fail closed. A native
  connection opened while that lock is busy remains usable for ordinary task
  APIs, but must be closed and reopened before dispatch (even after contention
  clears). This returns `skipped_locked`, not an ownership error. Native opens
  briefly take the same nonblocking lock as ticks and edits; under heavy connection
  traffic even an unowned board can report busy. This intentional serialization
  prevents stale-handle ownership bypasses and applies to unowned boards too.
  CLI metadata commands report a retryable error rather than a traceback; dashboard
  create/update returns HTTP 409 on lock contention. Retry with a fresh connection.
* Direct task APIs are not changed: this coordinates dispatchers, not arbitrary
  same-UID database writers. Replacing a DB manually without removing its sidecar
  retains ownership, including corruption recovery; clear ownership explicitly
  if the replacement should become unowned.
* Auto-decomposition holds the lock across auxiliary calls; operator edits may
  report busy for their entire duration and must be retried.

## Tests

Run `scripts/run_tests.sh tests/hermes_cli/test_kanban_dispatch_owner.py
 tests/hermes_cli/test_kanban_dispatch_owner_operator.py
 tests/hermes_cli/test_kanban_dispatch_owner_regressions.py
 tests/hermes_cli/test_kanban_dispatch_owner_lifecycle.py
 tests/hermes_cli/test_kanban_dispatch_owner_connection.py
 tests/hermes_cli/test_kanban_dispatch_owner_busy.py
 tests/plugins/test_kanban_board_lifecycle_api.py
 tests/gateway/test_kanban_pre_dispatch_isolation.py` (one shell command).
These exercise native dispatch, CLI and embedded-gateway denial, strict metadata,
actual-connection identity, alias/symlink handling, operator set/read/clear,
allowed scoped spawning, and owner/display edits attempted inside a live tick.
