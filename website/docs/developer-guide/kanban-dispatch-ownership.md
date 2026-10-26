# Per-board dispatch ownership

`dispatch_owner` is optional board metadata for cooperative, same-UID runtime
coordination. It is not a credential, authorization check, or security boundary.
A host must still validate task provenance/generation and authorize its scoped
spawn function. Anyone who can modify these files or execute arbitrary Python
as the same user can change ownership. Do not give task workers a dispatcher ID.

## Operator commands

After upgrading every dispatcher that can access the board:

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

Call the native boundary with a constant ID and your scoped spawn function:

```python
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_dispatch import dispatch_once

with kb.connect_closing(board="example") as conn:
    result = dispatch_once(conn, board="example", dispatcher_id="dedicated-worker",
                           spawn_fn=scoped_spawn)
```

The ID is an explicit Python keyword, not an environment variable or a worker
grant. Stock CLI, daemon, and embedded gateway callers omit it and cannot dispatch
owned boards. No plugin monkeypatch or lock bypass is necessary.

## Invariants

* Missing `dispatch_owner` retains stock eligibility. Legacy missing metadata is
  unowned. A present empty/null/non-string/non-slug value is invalid, not cleared.
* Slugs are exact lowercase alphanumeric/hyphen/underscore strings, 1–64 chars,
  beginning with an alphanumeric. No trimming, case folding, or coercion.
* The actual SQLite main database path selects metadata and the dispatch lock;
  an explicit board label or `HERMES_KANBAN_DB` alias cannot select a different
  owner's metadata. Symlinks resolve to their target. Default-board metadata
  lives under `kanban/boards/default/board.json`; other DB metadata is adjacent.
* Under the same lock, invalid/unreadable metadata or a mismatched identity returns
  `skipped_owned=True` before reconciliation, reclaim, promotion, claim, spawn,
  checkpoint, or dispatch observers. A valid owner appears in `dispatch_owner`;
  read/validation failures appear in `ownership_error`.
* An unavailable lock refuses dispatch and owner changes. Owner changes and
  display metadata edits use atomic replacement while holding that lock.
* In-memory/unidentifiable connections fail closed. Direct task APIs are not
  changed: this coordinates dispatchers, not arbitrary same-UID database writers.

## Tests

Run `scripts/run_tests.sh tests/hermes_cli/test_kanban_dispatch_owner.py
 tests/hermes_cli/test_kanban_dispatch_owner_operator.py` (one shell command).
These exercise native dispatch, CLI and embedded-gateway denial, strict metadata,
actual-connection identity, alias/symlink handling, operator set/read/clear,
allowed scoped spawning, and owner/display edits attempted inside a live tick.
