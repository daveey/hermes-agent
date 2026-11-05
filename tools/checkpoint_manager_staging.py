"""Index seeding and staging for :class:`tools.checkpoint_manager.CheckpointManager`.

Everything a snapshot does before ``write-tree``: seed the per-project index (from the last
checkpoint or the checkout's own index), price the add against the staging budget, run
``git add -A`` without reading oversize files, and drop anything over the size cap.

Module-level helpers (``_run_git``, ``_scan_tree``, ``_MAX_FILES``, ``staging``, ``logger``...)
are read from :mod:`tools.checkpoint_manager` at call time, so tests that monkeypatch them there
keep working and log records stay under the ``tools.checkpoint_manager`` logger.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from tools import checkpoint_staging as staging


def _cm():
    from tools import checkpoint_manager

    return checkpoint_manager


class CheckpointStagingMixin:
    """Staging phase of a checkpoint; mixed into ``CheckpointManager`` (needs ``max_file_size_mb``)."""

    max_file_size_mb: int

    def _seed_project_index(
        self, store: Path, working_dir: str, index_file: Path, ref: str,
    ) -> tuple[Optional[float], bool]:
        """Seed the per-project index; returns ``(warm_since, seeded_from_repo)``.

        Seed from the last checkpoint, if any, so the diff/commit machinery sees only changes
        since then. ``read-tree -m`` keeps the stat data of entries that still match the ref,
        so ``git add -A`` rehashes only files changed since the last snapshot (a plain
        read-tree made it rehash the whole tree every turn). With no snapshot to seed from, a
        git checkout's own index is used instead (see :meth:`_seed_from_repo_index`).
        """
        cm = _cm()
        warm_since: Optional[float] = None
        if index_file.exists():
            # Reset index to current ref tip to avoid accumulating stale paths.
            ok_ref, ref_commit, _ = cm._run_git(
                ["rev-parse", "--verify", ref + "^{commit}"],
                store, working_dir,
                allowed_returncodes={128},
            )
            if ok_ref and ref_commit:
                warm_since = self._reseed_from_ref(store, working_dir, index_file, ref_commit)
            else:
                cm._unlink_quiet(index_file)
        else:
            # First snapshot for this project.
            index_file.parent.mkdir(parents=True, exist_ok=True)

        # No snapshot to seed from: a git checkout's own index already carries
        # the stat data and blob ids of every tracked file, so seed from it
        # (copying the blobs the store lacks) and git hashes only what differs
        # from that index instead of the whole tree.
        seeded = False
        if warm_since is None and not index_file.exists():
            seeded = self._seed_from_repo_index(store, working_dir, index_file)
        return warm_since, seeded

    @staticmethod
    def _reseed_from_ref(
        store: Path, working_dir: str, index_file: Path, ref_commit: str,
    ) -> Optional[float]:
        """Reset an existing index to ``ref_commit``; the warm-since mtime, or None if cold."""
        cm = _cm()
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
                cm.os.utime(index_file, (cm.time.time(), index_mtime))
            except OSError:
                return None
        return index_mtime

    def _stage_snapshot(
        self, store: Path, working_dir: str, index_file: Path,
        scan: staging.TreeScan, warm_since: Optional[float], seeded: bool,
    ) -> bool:
        """Bring the seeded index up to the working tree; False skips this checkpoint."""
        cm = _cm()
        staging, logger = cm.staging, cm.logger
        # Old checkpoints and seeded repository indexes may still track
        # newly excluded paths. Drop them before pricing or staging the add.
        cm._unstage_store(store, working_dir, index_file)
        if not cm._unstage_excluded(store, working_dir, index_file):
            logger.debug("Checkpoint could not drop newly excluded paths from the index")
            return False

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

    def _stage_all(
        self, store: Path, working_dir: str, index_file: Path,
        scan: Optional[staging.TreeScan] = None,
    ) -> tuple[bool, str, str]:
        """``git add -A`` into the project index without reading oversize files.

        Files over ``max_file_size_mb`` go to git as a per-project
        ``core.excludesFile`` and are unstaged first (excludes do not apply to
        a path already in the index, e.g. one that grew past the cap since the
        last snapshot). On a timeout, the killed git's partial pack and index
        lock are removed. Callers hold the checkpoint store lock.
        """
        cm = _cm()
        staging = cm.staging
        if scan is None:
            scan = cm._scan_tree(working_dir, self.max_file_size_mb * 1024 * 1024, cm._MAX_FILES)
        cm._unstage_store(store, working_dir, index_file)
        if not cm._unstage_excluded(store, working_dir, index_file):
            return False, "", "could not drop newly excluded paths"
        config: list[str] = []
        add_args = cm._stage_all_args(store.parent, working_dir)
        oversize_pathspec = None
        if scan.oversize:
            excludes = staging.write_oversize_excludes(store, cm._project_hash(working_dir), scan.oversize)
            if excludes is not None:
                config = ["-c", f"core.excludesFile={excludes}"]
            if not self._unstage(store, working_dir, index_file, scan.oversize):
                return False, "", "could not drop oversize paths"
            # gitignore cannot encode newlines. A failed excludes-file write
            # must also fail closed rather than read oversized files into git.
            if excludes is None or any(staging.gitignore_literal(p) is None for p in scan.oversize):
                oversize_pathspec = self._write_oversize_pathspec(
                    scan.oversize + cm._store_pathspecs(store.parent, working_dir))
                add_args = ["add", "-A", f"--pathspec-from-file={oversize_pathspec}",
                            "--pathspec-file-nul"]
        started = cm.time.time()
        try:
            ok, out, err = cm._run_git(
                [*config, *add_args], store, working_dir,
                timeout=cm._GIT_TIMEOUT * 2, index_file=index_file,
            )
        finally:
            if oversize_pathspec is not None:
                cm._unlink_quiet(oversize_pathspec)
        if not ok and err.startswith("git timed out"):
            removed = staging.clear_timeout_debris(store, started, index_file)
            if removed:
                cm.logger.info("Checkpoint: removed %d partial file(s) left by the timed-out git add", len(removed))
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
            cm.logger.debug("Checkpoint: not seeding %s from its git index: %s", working_dir, why)
            for leftover in (index_file, Path(f"{index_file}.lock")):
                try:
                    leftover.unlink()
                except OSError:
                    pass
            return False

        try:
            index_file.parent.mkdir(parents=True, exist_ok=True)
            # copy2 keeps the index mtime: git's racy-clean check is anchored to it.
            cm.shutil.copy2(repo_index, index_file)
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
        if excluded and not self._unstage(store, working_dir, index_file, excluded):
            return abandon("could not drop excluded paths")

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
            started = cm.time.time()
            ok, err = cm._pack_from_repo(working_dir, common_dir, store, missing)
            if not ok:
                cm.staging.clear_timeout_debris(store, started, None)
                return abandon(err)
        cm.logger.debug(
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
        staging = cm.staging
        budget = staging.budget_s(cm._GIT_TIMEOUT * 2)
        est = staging.estimate(scan, None if precise else warm_since)
        if est.seconds <= budget:
            return None
        ok, out, _ = cm._run_git(
            ["ls-files", "-z", "-o", "-m", "--exclude-standard"],
            store, working_dir, index_file=index_file,
        )
        if not ok:
            return est
        listed = set(filter(None, out.split("\x00")))
        est = staging.estimate(scan, None, only=listed)
        return est if est.seconds > budget else None

    @staticmethod
    def _unstage(store: Path, working_dir: str, index_file: Path, paths: list[str]) -> bool:
        """Remove literal ``paths`` from the project index (absent ones are ignored).

        ``update-index --force-remove`` takes paths literally (no pathspec
        globbing) and, unlike ``rm --cached``, does not refuse an entry whose
        staged content differs from both the file and the ref."""
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

    @staticmethod
    def _write_oversize_pathspec(paths: list[str]) -> Path:
        """Write NUL-delimited literal excludes suitable for ``git add``."""
        cm = _cm()
        fd, raw_path = cm.tempfile.mkstemp(prefix="hermes-checkpoint-", suffix=".pathspec")
        path = Path(raw_path)
        try:
            with cm.os.fdopen(fd, "wb") as handle:
                for rel in paths:
                    handle.write(cm.os.fsencode(f":(exclude,top,literal){rel}"))
                    handle.write(b"\0")
        except Exception:
            cm._unlink_quiet(path)
            raise
        return path

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
        cm.logger.debug(
            "Checkpoint: dropping %d oversize file(s) (>%d MB) from index",
            len(oversize), self.max_file_size_mb,
        )
        return self._unstage(store, working_dir, index_file, oversize)
