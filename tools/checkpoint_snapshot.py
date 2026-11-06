"""Snapshot taking for :class:`tools.checkpoint_manager.CheckpointManager`: seed, stage, commit.

``CheckpointManager`` inherits these methods, so ``mgr._take`` / ``mgr._stage_all`` and the other
staging helpers keep their names. Module-level helpers (``_run_git``, ``_GIT_TIMEOUT``, ...) are
looked up on ``tools.checkpoint_manager`` at call time, so patches of that module still apply.
"""
from __future__ import annotations

import importlib
import logging
import os
import shutil
import time
from pathlib import Path
from types import ModuleType
from typing import Optional

from tools import checkpoint_staging as staging

logger = logging.getLogger("tools.checkpoint_manager")


def _cm() -> ModuleType:
    return importlib.import_module("tools.checkpoint_manager")


class SnapshotMixin:
    """Snapshot (``_take``) and index-staging methods of ``CheckpointManager``."""

    max_file_size_mb: int

    def _take(self, working_dir: str, reason: str, *, prune: bool = True) -> bool:
        """Take a snapshot.  Returns True on success."""
        cm = _cm()
        store = cm._store_path()

        err = cm._init_store(store, working_dir)
        if err:
            logger.debug("Checkpoint store init failed: %s", err)
            return False

        cm._touch_project(store, working_dir)

        # Quick size guard — don't try to snapshot enormous directories. Default-excluded
        # trees (build/, node_modules/, browser profiles) are never staged, so they don't count.
        if cm._dir_file_count(working_dir) > cm._MAX_FILES:
            logger.debug("Checkpoint skipped: >%d files in %s", cm._MAX_FILES, working_dir)
            return False

        dir_hash = cm._project_hash(working_dir)
        index_file = cm._index_path(store, dir_hash)
        ref = cm._ref_name(dir_hash)

        if not self._stage_snapshot(store, working_dir, index_file, ref):
            return False
        new_sha = self._commit_snapshot(store, working_dir, index_file, ref, reason)
        if new_sha is None:
            return False

        # Count and size budgets share one failure boundary. A workspace
        # removed after the snapshot landed (Kanban cleanup) has nothing to prune.
        if prune and os.path.isdir(working_dir):
            self._prune(store, working_dir, ref)

        return True

    @staticmethod
    def _reseed_index(store: Path, working_dir: str, index_file: Path, ref: str) -> Optional[float]:
        """Point the project index at the last checkpoint; the index mtime when warm, else None.

        Seed the per-project index from the last checkpoint, if any, so the
        diff/commit machinery sees only changes since then.  On first call,
        clear the index so ``git add -A`` produces a clean tree.
        ``read-tree -m`` keeps the stat data of entries that still match the
        ref, so ``git add -A`` rehashes only files changed since the last
        snapshot (a plain read-tree made it rehash the whole tree every turn).
        """
        cm = _cm()
        if not index_file.exists():
            # First snapshot for this project.
            index_file.parent.mkdir(parents=True, exist_ok=True)
            return None
        # Reset index to current ref tip to avoid accumulating stale paths.
        ok_ref, ref_commit, _ = cm._run_git(
            ["rev-parse", "--verify", ref + "^{commit}"],
            store, working_dir,
            allowed_returncodes={128},
        )
        if not (ok_ref and ref_commit):
            try:
                index_file.unlink()
            except OSError:
                pass
            return None
        try:
            index_mtime = index_file.stat().st_mtime
        except OSError:
            index_mtime = None
        ok_seed, _, _ = cm._run_git(
            ["read-tree", "-m", ref_commit],
            store, working_dir,
            index_file=index_file,
            allowed_returncodes={128},
        )
        if not ok_seed:
            cm._run_git(
                ["read-tree", ref_commit],
                store, working_dir,
                index_file=index_file,
                allowed_returncodes={128},
            )
            return None
        if index_mtime is not None:
            # Keep the mtime of the last add's stat refresh: git
            # treats entries modified at or after it as racy and
            # rehashes them, and so does the estimate below. A
            # skipped or killed add then leaves the next turn's
            # estimate anchored to the last real snapshot.
            try:
                os.utime(index_file, (time.time(), index_mtime))
            except OSError:
                return None
        return index_mtime

    def _stage_snapshot(self, store: Path, working_dir: str, index_file: Path, ref: str) -> bool:
        """Seed the index and stage the working tree into it; False when the snapshot is skipped."""
        cm = _cm()
        warm_since = self._reseed_index(store, working_dir, index_file, ref)

        # No snapshot to seed from: a git checkout's own index already carries
        # the stat data and blob ids of every tracked file, so seed from it
        # (copying the blobs the store lacks) and git hashes only what differs
        # from that index instead of the whole tree.
        seeded = False
        if warm_since is None and not index_file.exists():
            seeded = self._seed_from_repo_index(store, working_dir, index_file)

        # Drop entries the old tree (or the seeding checkout index) carries but this build
        # excludes BEFORE staging: ``add -A`` re-reads every tracked path, and a locked
        # browser-profile file would fail it each time.
        cm._unstage_store(store, working_dir, index_file)
        if not cm._unstage_excluded(store, working_dir, index_file):
            logger.debug("Checkpoint could not drop newly excluded paths from the index")
            return False
        # Walk after the index is settled: the scan stats what the index still tracks inside
        # pruned directories, since ``add -A`` re-reads those whatever the excludes say.
        scan = cm._staging_scan(working_dir, self.max_file_size_mb * 1024 * 1024, store, index_file)

        # Price the add before running it: files under the cap still cost a
        # hash each, and a tree of them can outlast the timeout (a fresh source
        # checkout on a loaded laptop). Over budget, skip the add instead of
        # blocking the turn until git is killed.
        over = self._over_staging_budget(
            store, working_dir, index_file, scan, warm_since, precise=seeded,
        )
        if over is not None:
            staging.mark_slow(store, working_dir)
            logger.warning(
                "Checkpoint of %s skipped before staging: %s, over the %.0fs budget "
                "(biggest files: %s); not retried for %d min",
                working_dir, over.text(), staging.budget_s(cm._GIT_TIMEOUT * 2),
                scan.biggest_text(), staging._SLOW_DIR_BACKOFF_S // 60,
            )
            return False

        # Stage with per-project index; files over max_file_size_mb are
        # excluded before git reads them.
        ok, _, err = self._stage_all(store, working_dir, index_file, scan)
        if not ok:
            if err.startswith("git timed out"):
                staging.mark_slow(store, working_dir)
                logger.warning(
                    "Checkpoint of %s timed out staging (biggest files: %s); not retried for %d min",
                    working_dir, scan.biggest_text(), staging._SLOW_DIR_BACKOFF_S // 60,
                )
            else:
                logger.debug("Checkpoint git-add failed: %s", err)
            return False

        # Backstop: anything over the cap that slipped past the excludes
        # (a name gitignore cannot express, a file that grew mid-add).
        if self.max_file_size_mb > 0:
            if not self._drop_oversize_from_index(store, working_dir, index_file):
                logger.debug("Checkpoint oversize index cleanup failed")
                return False
        return True

    @staticmethod
    def _commit_snapshot(store: Path, working_dir: str, index_file: Path, ref: str, reason: str) -> Optional[str]:
        """Commit the staged index onto ``ref``; the new commit, or None when nothing was recorded."""
        cm = _cm()
        # Compare against the current ref tip (not HEAD — HEAD points to a
        # branch that doesn't exist on a bare store, so ``diff --cached``
        # against HEAD would always show "new file" for every staged path).
        ok_ref, ref_commit, _ = cm._run_git(
            ["rev-parse", "--verify", ref + "^{commit}"],
            store, working_dir,
            allowed_returncodes={128},
        )
        has_ref = ok_ref and bool(ref_commit)

        if has_ref:
            ok_diff, _, _ = cm._run_git(
                ["diff-index", "--cached", "--quiet", ref_commit],
                store, working_dir,
                allowed_returncodes={1},
                index_file=index_file,
            )
            if ok_diff:
                logger.debug("Checkpoint skipped: no changes in %s", working_dir)
                return None
        else:
            # No ref yet — skip only if the index is empty.
            ok_ls, ls_out, _ = cm._run_git(
                ["ls-files", "--cached"],
                store, working_dir,
                index_file=index_file,
            )
            if ok_ls and not ls_out.strip():
                logger.debug("Checkpoint skipped: empty tree in %s", working_dir)
                return None

        # Write tree from per-project index.
        ok_tree, tree_sha, err = cm._run_git(
            ["write-tree"], store, working_dir,
            index_file=index_file,
        )
        if not ok_tree or not tree_sha:
            logger.debug("Checkpoint write-tree failed: %s", err)
            return None

        # A nested repository is stored as a gitlink, not its files: say so in
        # the checkpoint itself rather than only when a rollback is refused.
        ok_ls, tree_out, _ = cm._run_git(["ls-tree", "-r", "-z", tree_sha], store, working_dir)
        nested_repos = cm._gitlink_paths(tree_out) if ok_ls else []
        if nested_repos:
            logger.info("Checkpoint of %s does not capture nested git repositories: %s",
                        working_dir, ", ".join(nested_repos))
            # Listings read the subject (%s), which ends at the first blank
            # line, so keep a multiline reason (a terminal command) on one
            # line or the note falls outside every listing.
            reason = " ".join(
                line.strip() for line in reason.splitlines() if line.strip()
            ) + cm._uncaptured_note(nested_repos)

        # Build commit (parent = current ref tip, if any).
        commit_args = ["commit-tree", tree_sha, "-m", reason, "--no-gpg-sign"]
        if has_ref:
            commit_args = ["commit-tree", tree_sha, "-p", ref_commit, "-m", reason, "--no-gpg-sign"]
        ok_commit, new_sha, err = cm._run_git(
            commit_args, store, working_dir,
            index_file=index_file,
        )
        if not ok_commit or not new_sha:
            logger.debug("Checkpoint commit-tree failed: %s", err)
            return None

        # Update the per-project ref.
        update_args = ["update-ref", ref, new_sha]
        if has_ref:
            update_args = ["update-ref", ref, new_sha, ref_commit]
        ok_update, _, err = cm._run_git(
            update_args, store, working_dir,
        )
        if not ok_update:
            logger.debug("Checkpoint update-ref failed: %s", err)
            return None

        logger.debug("Checkpoint taken in %s: %s (%s)", working_dir, reason, new_sha[:8])
        return new_sha

    def _stage_all(
        self, store: Path, working_dir: str, index_file: Path,
        scan: Optional[staging.TreeScan] = None,
    ) -> tuple[bool, str, str]:
        """``git add -A`` into the project index without reading oversize files.

        Files over ``max_file_size_mb`` go to git as a per-project
        ``core.excludesFile`` and are unstaged first (excludes do not apply to
        a path already in the index, e.g. one that grew past the cap since the
        last snapshot). The excludes file loses to a project ``.gitignore``
        re-include (``!weights.bin``), so the same paths also ride as literal
        exclude pathspecs, which nothing in the tree overrides. On a timeout,
        the killed git's partial pack and index lock are removed. Callers hold
        the checkpoint store lock.
        """
        cm = _cm()
        if scan is None:
            scan = cm._staging_scan(working_dir, self.max_file_size_mb * 1024 * 1024, store, index_file)
        config: list[str] = []
        # The store-exclusion pathspec keeps a store nested in the tree (HERMES_HOME as the
        # working directory) out of its own snapshots.
        add_args = cm._stage_all_args(store.parent, working_dir)
        if scan.oversize:
            dir_hash = cm._project_hash(working_dir)
            excludes = staging.write_oversize_excludes(store, dir_hash, scan.oversize)
            if excludes is not None:
                config = ["-c", f"core.excludesFile={excludes}"]
            # git refuses command-line pathspecs beside --pathspec-from-file, so the store
            # self-exclusion rides in the same file. git add fails on an exclude pathspec
            # naming an ignored path, so only paths the ignore rules miss go in it.
            self._unstage(store, working_dir, index_file, scan.oversize)
            store_specs = cm._store_pathspecs(store.parent, working_dir)
            unignored = self._not_ignored(store, working_dir, config, scan.oversize + store_specs)
            if unignored is None:
                return False, "", "oversize ignore check failed"
            if unignored:
                spec = staging.write_oversize_pathspec(store, dir_hash, unignored)
                if spec is None:
                    return False, "", "oversize pathspec could not be written"
                add_args = ["add", "-A", f"--pathspec-from-file={spec}", "--pathspec-file-nul"]
        started = time.time()
        ok, out, err = cm._run_git(
            [*config, *add_args], store, working_dir,
            timeout=cm._GIT_TIMEOUT * 2, index_file=index_file,
        )
        if not ok and err.startswith("git timed out"):
            removed = staging.clear_timeout_debris(store, started, index_file)
            if removed:
                logger.info("Checkpoint: removed %d partial file(s) left by the timed-out git add", len(removed))
        return ok, out, err

    def _seed_from_repo_index(self, store: Path, working_dir: str, index_file: Path) -> bool:
        """Seed a cold project index from the git checkout's own index.

        A fresh checkout (a new worktree) has never been snapshotted, so every
        file would be hashed and written as a new object: 37 s for a
        hermes-agent worktree on a loaded hub, over the staging budget. The
        checkout's own index already holds each tracked file's blob id and stat
        data. Copying it lets ``git add -A`` rehash only files that differ from
        it; the blobs the store lacks are copied from the repository as one pack
        (``pack-objects`` reuses the stored data, ~2 s for that worktree), so the
        checkpoint never depends on the repository's object store.

        Only when ``working_dir`` is the checkout's top level and every entry is
        a plain stage-0 entry (no conflicts, assume-unchanged or skip-worktree
        bits, which would hide changes from ``git add``). Tracked files the
        checkpoint excludes (``DEFAULT_EXCLUDES``, the project's .gitignore) are
        dropped, so the snapshot matches a cold add. Any failure leaves no index
        behind (the cold path then prices and stages as before). Callers hold
        the checkpoint store lock.
        """
        cm = _cm()
        repo = cm._checkout_index(working_dir)
        if repo is None:
            return False
        repo_index, common_dir = repo

        def abandon(why: str) -> bool:
            logger.debug("Checkpoint: not seeding %s from its git index: %s", working_dir, why)
            for leftover in (index_file, Path(f"{index_file}.lock")):
                try:
                    leftover.unlink()
                except OSError:
                    pass
            return False

        try:
            index_file.parent.mkdir(parents=True, exist_ok=True)
            # copy2 keeps the index mtime: git's racy-clean check is anchored to it.
            shutil.copy2(repo_index, index_file)
        except OSError as exc:
            return abandon(f"copy failed: {exc}")

        # Drop extensions tied to the checkout (untracked cache, fsmonitor); a
        # split index fails here (its shared part is not in the store).
        ok, _, err = cm._run_git(
            ["-c", "core.untrackedCache=false", "-c", "core.fsmonitor=false",
             "update-index", "--no-untracked-cache", "--no-fsmonitor"],
            store, working_dir, index_file=index_file, allowed_returncodes={128},
        )
        if not ok:
            return abandon(err or "update-index failed")

        ok, out, err = cm._run_git(
            ["ls-files", "-z", "--cached", "--ignored", "--exclude-standard"],
            store, working_dir, index_file=index_file, allowed_returncodes={128},
        )
        if not ok:
            return abandon(err or "ls-files failed")
        excluded = [p for p in out.split("\x00") if p]
        if excluded:
            self._unstage(store, working_dir, index_file, excluded)

        ok, out, err = cm._run_git(
            ["ls-files", "-z", "-v", "--stage"],
            store, working_dir, index_file=index_file, allowed_returncodes={128},
        )
        if not ok:
            return abandon(err or "ls-files failed")
        blobs: set[str] = set()
        for record in filter(None, out.split("\x00")):
            meta, _, _path = record.partition("\t")
            parts = meta.split()
            if len(parts) != 4:
                return abandon(f"unexpected ls-files record {meta!r}")
            tag, mode, sha, stage_no = parts
            if tag != "H" or stage_no != "0":
                return abandon(f"index entry flagged {tag}/{stage_no}")
            if mode != "160000":  # a gitlink names a commit of another repo
                blobs.add(sha)
        if not blobs:
            return abandon("empty index")

        ok, out, err = cm._run_git(
            ["cat-file", "--batch-check=%(objectname)"], store, working_dir,
            input_text="\n".join(sorted(blobs)) + "\n",
        )
        if not ok:
            return abandon(err or "cat-file failed")
        missing = [line.split()[0] for line in out.splitlines() if line.endswith(" missing")]
        if missing:
            started = time.time()
            ok, err = cm._pack_from_repo(working_dir, common_dir, store, missing)
            if not ok:
                staging.clear_timeout_debris(store, started, None)
                return abandon(err)
        logger.debug(
            "Checkpoint: seeded %s from its git index (%d entries, %d blobs copied)",
            working_dir, len(blobs), len(missing),
        )
        return True

    def _over_staging_budget(
        self, store: Path, working_dir: str, index_file: Path,
        scan: staging.TreeScan, warm_since: Optional[float],
        *, precise: bool = False,
    ) -> Optional[staging.AddEstimate]:
        """The add's estimate when it would not finish inside the staging budget, else None.

        A free estimate from the tree walk comes first (every file on a cold
        index; files modified since the last snapshot on a warm one). Only when
        that is over budget does git list what it would really hash
        (``ls-files -o -m``: new and stat-changed, gitignored files dropped),
        which is a directory walk with no hashing. ``precise`` (an index seeded
        from the checkout's own) prices the free estimate as a cold index: that
        index's mtime says nothing about files left dirty before it was written,
        so only the listing may narrow it."""
        cm = _cm()
        budget = staging.budget_s(cm._GIT_TIMEOUT * 2)
        est = staging.estimate(scan, None if precise else warm_since)
        if est.seconds <= budget:
            return None
        ok, out, _ = cm._run_git(
            # Same store exclusion as the add: a nested store is never staged, so never priced.
            ["ls-files", "-z", "-o", "-m", "--exclude-standard",
             *cm._exclude_store_args(store.parent, working_dir)],
            store, working_dir, index_file=index_file,
        )
        if not ok:
            return est
        listed = set(filter(None, out.split("\x00")))
        est = staging.estimate(scan, None, only=listed)
        return est if est.seconds > budget else None

    @staticmethod
    def _not_ignored(store: Path, working_dir: str, config: list[str], paths: list[str]) -> Optional[list[str]]:
        """The ``paths`` git's ignore rules (with ``config``) do not exclude; None when git failed.

        ``check-ignore`` reads no file content, and ``--no-index`` judges tracked paths too.
        A ``!`` re-include shows here as not ignored. NUL-separated stdin keeps any name
        (newlines, quotes, non-UTF-8 bytes) exact; check-ignore rejects ``literal`` magic,
        so a ``./`` prefix keeps a leading ``:`` from parsing as magic (glob characters
        are matched as-is there).
        """
        if not paths:
            return []
        cm = _cm()
        ok, out, err = cm._run_git(
            [*config, "check-ignore", "-z", "--stdin", "--no-index"],
            store, working_dir, allowed_returncodes={1},
            input_text="".join(f"./{p}\x00" for p in paths),
        )
        # rc 1 (nothing ignored) is the only failure that is silent and prints nothing.
        if not ok and (out or err):
            return None
        ignored = {p.removeprefix("./") for p in out.split("\x00") if p}
        return [p for p in paths if p not in ignored]

    @staticmethod
    def _unstage(store: Path, working_dir: str, index_file: Path, paths: list[str]) -> bool:
        """Remove literal ``paths`` from the project index (absent ones are ignored).

        ``update-index --force-remove`` takes paths literally (no pathspec
        globbing) and, unlike ``rm --cached``, does not refuse an entry whose
        staged content differs from both the file and the ref. False when a
        batch failed (the index may still hold some of ``paths``)."""
        cm = _cm()
        batch = 200
        for i in range(0, len(paths), batch):
            ok, _, _ = cm._run_git(
                ["update-index", "--force-remove", "--", *paths[i:i + batch]],
                store, working_dir, index_file=index_file,
            )
            if not ok:
                return False
        return True

    def _exceeds_size_cap(self, path: Path) -> bool:
        """Whether *path* is larger than ``max_file_size_mb``.

        The same test :meth:`_drop_oversize_from_index` applies when building a
        checkpoint, so "excluded from the checkpoint" and "refused deletion at
        restore" agree on one definition. A cap of 0 disables it, and an
        unstattable path is not claimed to be oversize.
        """
        cap = self.max_file_size_mb * 1024 * 1024
        if cap <= 0:
            return False
        try:
            return path.stat().st_size > cap
        except OSError:
            return False

    def _drop_oversize_from_index(
        self, store: Path, working_dir: str, index_file: Path,
    ) -> bool:
        """Remove any staged file larger than ``max_file_size_mb`` from the index.

        Lets the agent keep snapshotting source code while refusing to
        swallow generated assets (datasets, model weights, logs, videos).
        """
        cm = _cm()
        if self.max_file_size_mb <= 0:
            return True
        ok, stdout, _ = cm._run_git(
            ["ls-files", "--cached", "-z"],
            store, working_dir, index_file=index_file,
        )
        if not ok:
            return False
        if not stdout:
            return True
        # NUL separators preserve whitespace within each literal filename.
        paths = [p for p in stdout.split("\x00") if p]
        abs_workdir = cm._normalize_path(working_dir)
        # Same predicate safe restore consults, called rather than restated:
        # a threshold that drifted between the two would make a file both
        # absent from the checkpoint and not recognised as capped at restore,
        # which is precisely the deletion this change exists to prevent.
        oversize = [
            rel for rel in paths if self._exceeds_size_cap(abs_workdir / rel)
        ]
        if not oversize:
            return True
        logger.debug(
            "Checkpoint: dropping %d oversize file(s) (>%d MB) from index",
            len(oversize), self.max_file_size_mb,
        )
        return self._unstage(store, working_dir, index_file, oversize)
