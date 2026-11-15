"""Bounded failure and stop tokens for update receipts and shared metrics."""

# Why a `hermes update` run failed or was refused, derived from the FINAL receipt only
# (shared_metrics_update.update_failure_class); ``none`` unless outcome is failed/refused/partial.
UPDATE_FAILURE_CLASSES = frozenset({
    "aborted_before_apply",  # fallback: exited before the checkout moved with no reason recorded
    "build_failed",          # the build stage reported failure
    "deps_failed",           # PM dependency preparation failed (no deps mark, or a PM error type)
    "exception",             # the run ended on an uncaught exception (type name dropped)
    "fleet_stale",           # post-update verification found a gateway still stale or down
    "fleet_unverified",      # verification could not prove the fleet current (no rows, unaccounted runtime)
    "git_failed",            # a git/installer subprocess failed before the checkout moved (stash, pull, merge)
    "interrupted",           # KeyboardInterrupt / exit 130
    "lock_held",             # another updater held the update lock (refused, exit 2)
    "managed_install",       # admission refused: image/package-managed or commit-build install (refused)
    "none", "other",
    "os_error",              # the run ended on an OSError (disk full, permission, file in use)
    "restart_failed",        # the gateway restart failed, or a skipped restart left the fleet owing one
    "subprocess_failed",     # a subprocess error (CalledProcessError, TimeoutExpired) reached the boundary
    "unknown",               # the reporter sent no reason (Desktop packaged updaters)
    "disk_full",             # out of disk space (ENOSPC, or no room to stage the ZIP)
    "local_changes_parked",  # committed, but the user's stashed changes could not be re-applied (partial)
    "permission_denied",     # the run ended on a PermissionError
})
# The exit that stopped a run before its apply stage mark, recorded at the exit itself as one of
# these tokens (update_receipt.record_stop_reason -> the receipt's ``stop_class``).
UPDATE_STOP_CLASSES = frozenset({
    "branch_missing",         # the target branch exists neither locally nor on origin
    "branch_unsupported",     # --branch on the Windows ZIP fallback
    "channel_unresolved",     # the update channel could not be resolved
    "checkout_move_failed",   # git could not move the checkout (ff refused, reset or switch failed)
    "commit_point_refused",   # the commit point could not be armed durably; nothing moved
    "detached_head",          # a detached checkout stayed put, or its commits could not be backed up
    "disk_full",              # (see above)
    "downgrade_refused",      # the target predates the gateway runtime a state.db already uses
    "download_failed",        # the ZIP fallback download failed
    "fetch_failed",           # git fetch failed
    "gateway_pause_failed",   # Windows: the gateways could not be paused safely before the venv moves
    "git_in_progress",        # a merge/rebase/cherry-pick/... was already in progress
    "git_index_locked",       # git refused: another git process's .git/index.lock exists
    "ref_lock_stale",         # ref lock exists; diagnosis is not permission to reclaim it
    "git_timeout",            # a network git call hit the updater's time limit
    "head_moved",             # a ref moved during the update; HEAD is not the selected commit
    "local_changes_blocked",  # local changes git could not stash, or a dirty tree the ZIP will not overwrite
    "lock_held",              # (see above)
    "managed_install",        # (see above): HERMES_MANAGED installs refuse before any receipt
    "merge_conflict",         # local commits on a custom branch conflict with upstream
    "not_git_checkout",       # the install is not a git checkout (non-Windows)
    "old_version_handoff",    # an older updater's hand-off to this version could not finish
    "parked_branch_blocked",  # the checkout is parked on another branch that is unsafe to switch
    "permission_denied",      # (see above): git could not write a file it needs (e.g. index.lock's directory)
    "stash_restore_rejected",  # re-applied local changes broke Hermes; the update stopped
    "syntax_rollback",        # the pulled code failed the syntax check and was rolled back
    "target_syntax_error",    # the target failed the syntax check before anything moved
    "target_unresolved",      # the fetched target ref did not resolve to a commit
    "unexpected_branch",      # the checkout ended on a branch that is not the target
    "up_to_refused",          # --up-to <sha> refused (malformed, not first-parent ahead, or an unsupported combo)
    "venv_foreign_owner",     # the venv belongs to another OS user
    "zip_failed",             # the ZIP fallback swap failed and was rolled back
})
UPDATE_FAILURE_CLASSES |= UPDATE_STOP_CLASSES
