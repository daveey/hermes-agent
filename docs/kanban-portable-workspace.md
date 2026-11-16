# Portable continuation workspace archives

A continuation normally pins a pooled card when its workspace has unpushed or
unchecked state. To keep its existing pool constraints, publish a full archive
on the hub, then put this declaration on its own line in the checkpoint (or a
comment posted strictly after both the current run started and the last move):

```
portable-workspace: hub-admin:/srv/archives/<task-id>/workspace.tar.gz SHA256 <64-hex-digest>
```

The archive path must contain the exact task ID as a path component. Create the
archive **outside** the workspace using `tar -czf /outside/workspace.tar.gz -C
/workspace .`, transfer it to the hub, and calculate its SHA256. Do not change the
workspace after archiving; if it changes, publish a new archive and declaration.
Diagnostic bundles or unmarked references do not release a machine pin. The first
archive reference in the checkpoint, otherwise the newest eligible comment, wins;
an invalid reference never falls back to older evidence.

Verification hashes the archive on the hub and compares its complete inventory
(relative paths, entry types, permission bits, and file content SHA256s) with the
current workspace. All trees reported by the pin planner must lie inside that
workspace. Local inventory is read again after the hub read to catch intervening
edits. No files are extracted during verification. Each subprocess has a 20-second
timeout and inventories have a 100,000-entry cap; failures keep the existing pin
behavior. There is no exemption for schedule or block, and publication guards are
unchanged.

The initial format deliberately fails closed on symlinks, hardlinks in archives,
special files, linked-worktree `.git` files and Git alternates. A standalone clone
with its entire `.git` directory can be carried, but a linked worktree's external
Git metadata cannot. Archive the entire scratch workspace with a standalone clone
rather than declaring a partial worktree/evidence bundle portable. Roots and
entries must have the same modes, and the tar root must be `.`. A digest match
alone is never proof of workspace coverage. Concurrent writers must be stopped
before publishing; this is a bounded observation, not a filesystem lock.
