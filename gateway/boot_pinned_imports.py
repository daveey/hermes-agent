"""Keep a long-running process on ONE code revision after an in-place core update.

A gateway or ``hermes serve`` imports most of the tree at boot and the rest lazily. When
``hermes update`` moves the checkout under it, the next first-time import loads the NEW file
against the OLD modules already in ``sys.modules``: ``from agent.model_routing_managed import
ManagedPinRejectedError`` then fails with ImportError because the loaded ``model_routing_managed``
predates the class (t_01a1824adc287c5a). Retirement (``web_server_skew_exit``) only fires once the
backend is idle, which a busy Desktop can postpone for hours.

This finder closes that window. It snapshots the boot commit; once the checkout has moved past it,
every first-time import of a git-tracked module under the project root is served from the boot
commit's blob (``git cat-file``) instead of the working tree, so the process keeps running one
consistent revision until it restarts on the new code. Running turns are untouched.

Every uncertain case falls back to the normal import (exactly the pre-fix behaviour): no boot
commit, git missing or failing, a path outside the root or untracked (venv, plugins), a module that
did not exist at boot. The finder never raises.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import linecache
import logging
import os
import shutil
import subprocess
import sys
import threading
from typing import Callable, Optional

_log = logging.getLogger(__name__)

_GIT_TIMEOUT_S = 15
_GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES")


def _sha_of(fingerprint: Optional[str]) -> Optional[str]:
    """Full commit sha from a ``git:<ref>:<sha>`` fingerprint, else None."""
    if not fingerprint:
        return None
    sha = fingerprint.rsplit(":", 1)[-1].strip()
    if len(sha) in (40, 64) and all(c in "0123456789abcdef" for c in sha):
        return sha
    return None


class _BootBlobLoader(importlib.abc.Loader):
    """Executes a module from the boot commit's source while keeping its on-disk location
    (``__file__``, package ``__path__``, data files) so relative file access still works."""

    def __init__(self, fullname: str, source: bytes, origin: str, is_package: bool):
        self.name = fullname
        self.path = origin
        self._source = source
        self._is_package = is_package

    def create_module(self, spec):
        return None

    def exec_module(self, module) -> None:
        text = self._source.decode("utf-8")
        # Tracebacks and inspect show the code that actually runs, not the newer file on disk.
        linecache.cache[self.path] = (len(text), None, text.splitlines(True), self.path)
        code = compile(self._source, self.path, "exec", dont_inherit=True)
        exec(code, module.__dict__)

    def is_package(self, fullname: str) -> bool:
        return self._is_package

    def get_source(self, fullname: str) -> str:
        return self._source.decode("utf-8")

    def get_filename(self, fullname: str) -> str:
        return self.path

    def get_data(self, path: str) -> bytes:
        with open(path, "rb") as fh:
            return fh.read()

    def get_resource_reader(self, fullname: str):
        if not self._is_package:
            return None
        from importlib.readers import FileReader

        return FileReader(self)  # type: ignore[arg-type]  # FileReader only reads .path


class BootPinnedFinder(importlib.abc.MetaPathFinder):
    """``sys.meta_path`` entry that pins first-time in-tree imports to ``boot_sha`` once the
    checkout has moved. ``current_sha`` reads the checkout's HEAD sha (None when unknown)."""

    def __init__(self, root: str, boot_sha: str, current_sha: Callable[[], Optional[str]],
                 git: Optional[str] = None):
        self.root = os.path.realpath(root)
        self.boot_sha = boot_sha
        self._current_sha = current_sha
        self._git = git or shutil.which("git")
        self._top_level = self._list_top_level()
        self._lock = threading.RLock()
        self._local = threading.local()
        self._skewed = False
        self._boot_files: Optional[frozenset[str]] = None
        self._boot_dirs: Optional[frozenset[str]] = None
        self._git_failed = False
        self._announced = False
        self.pinned: list[str] = []  # module names served from the boot commit (observability/tests)

    # -- helpers -------------------------------------------------------------------------------
    def _list_top_level(self) -> frozenset[str]:
        try:
            names = os.listdir(self.root)
        except OSError:
            return frozenset()
        return frozenset(n.removesuffix(".py") for n in names)

    def _rel(self, path: str) -> Optional[str]:
        try:
            real = os.path.realpath(path or os.getcwd())
        except (OSError, ValueError):
            return None
        if real == self.root:
            return ""
        if real.startswith(self.root + os.sep):
            return os.path.relpath(real, self.root).replace(os.sep, "/")
        return None

    def _run_git(self, *args: str) -> Optional[bytes]:
        if not self._git or self._git_failed:
            return None
        env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
        try:
            done = subprocess.run([self._git, "--no-replace-objects", "-C", self.root, *args],
                                  capture_output=True, timeout=_GIT_TIMEOUT_S, env=env,
                                  stdin=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            self._git_failed = True
            _log.warning("boot-pinned imports disabled: git failed (%s); lazy imports load from disk", exc)
            return None
        if done.returncode != 0:
            return None
        return done.stdout

    def _boot_tree(self) -> Optional[frozenset[str]]:
        if self._boot_files is None:
            out = self._run_git("ls-tree", "-r", "-z", "--name-only", self.boot_sha)
            if out is None:
                self._git_failed = True
                _log.warning("boot-pinned imports disabled: boot commit %s is unreadable; "
                             "lazy imports load from disk", self.boot_sha[:10])
                return None
            self._boot_files = frozenset(p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p)
        return self._boot_files

    def is_skewed(self) -> bool:
        """Latched: once the checkout left the boot commit, this process stays pinned to it."""
        if self._skewed:
            return True
        try:
            current = self._current_sha()
        except Exception:
            current = None
        if current and current != self.boot_sha:
            self._skewed = True
        return self._skewed

    def _tracked_dir(self, rel: Optional[str]) -> bool:
        """True when ``rel`` lies under a top-level directory the boot commit tracks (so not the
        untracked venv, caches or plugin checkouts that happen to live under the root)."""
        files = self._boot_tree()
        if files is None or rel is None:
            return False
        if self._boot_dirs is None:
            self._boot_dirs = frozenset(f.split("/", 1)[0] for f in files if "/" in f)
        return rel == "" or rel.split("/", 1)[0] in self._boot_dirs

    def _boot_location(self, fullname: str, path) -> Optional[tuple[str, bool]]:
        """(repo-relative file, is_package) for ``fullname`` in the boot tree, searching the same
        directories the path finder would (the parent package's ``__path__`` or ``sys.path``)."""
        files = self._boot_tree()
        if files is None:
            return None
        tail = fullname.rpartition(".")[2]
        for entry in (path if path is not None else sys.path):
            rel_dir = self._rel(entry) if isinstance(entry, str) else None
            if rel_dir is None:
                continue
            base = f"{rel_dir}/{tail}" if rel_dir else tail
            if f"{base}/__init__.py" in files:
                return f"{base}/__init__.py", True
            if f"{base}.py" in files:
                return f"{base}.py", False
        return None

    # -- MetaPathFinder ------------------------------------------------------------------------
    def find_spec(self, fullname, path=None, target=None):
        if getattr(self._local, "busy", False):
            return None
        if path is None and fullname.partition(".")[0] not in self._top_level:
            return None  # not a top-level name the project root provides
        self._local.busy = True
        try:
            return self._find_spec(fullname, path)
        except Exception:
            _log.debug("boot-pinned finder fell back for %s", fullname, exc_info=True)
            return None
        finally:
            self._local.busy = False

    def _find_spec(self, fullname, path):
        if self._git_failed or not self.is_skewed():
            return None
        with self._lock:
            if path is not None and not any(isinstance(e, str) and self._tracked_dir(self._rel(e))
                                            for e in path):
                return None  # a submodule of a package outside the tracked tree (venv, stdlib)
            disk = importlib.machinery.PathFinder.find_spec(fullname, path)
            disk_rel = self._rel(disk.origin) if disk is not None and disk.origin and disk.has_location else None
            if disk is not None and disk.has_location and disk_rel is None:
                return None  # resolves outside the project root: not ours
            located = self._boot_location(fullname, path)
            if located is None:
                if disk_rel is not None and disk_rel != "" and self._tracked_dir(disk_rel):
                    _log.warning("boot-pinned imports: %s did not exist at boot commit %s; loading "
                                 "it from disk", fullname, self.boot_sha[:10])
                return None
            rel, is_package = located
            source = self._run_git("cat-file", "blob", f"{self.boot_sha}:{rel}")
            if source is None:
                return None
            if not self._announced:
                self._announced = True
                _log.warning("checkout moved past boot commit %s; first-time imports of in-tree "
                             "modules now load from the boot commit until this process restarts",
                             self.boot_sha[:10])
            self.pinned.append(fullname)
        origin = os.path.join(self.root, *rel.split("/"))
        loader = _BootBlobLoader(fullname, source, origin, is_package)
        spec = importlib.util.spec_from_loader(fullname, loader, origin=origin, is_package=is_package)
        if spec is None:
            return None
        spec.has_location = True
        if is_package:
            spec.submodule_search_locations = [os.path.dirname(origin)]
        return spec


_installed: Optional[BootPinnedFinder] = None


def install(root, boot_fingerprint: Optional[str],
            current_fingerprint: Callable[[], Optional[str]]) -> Optional[BootPinnedFinder]:
    """Put a ``BootPinnedFinder`` at the front of ``sys.meta_path`` (idempotent per process).

    No-op when ``HERMES_BOOT_PINNED_IMPORTS=0``, under pytest unless it is ``1``, or when the boot
    revision is not a resolved commit."""
    global _installed
    setting = os.environ.get("HERMES_BOOT_PINNED_IMPORTS", "").strip().lower()
    if setting in {"0", "false", "no", "off"}:
        return None
    if os.environ.get("PYTEST_CURRENT_TEST") and setting not in {"1", "true", "yes", "on"}:
        return None  # a test process imports a dirty worktree; serve lifespans under test must not pin it
    boot_sha = _sha_of(boot_fingerprint)
    if boot_sha is None:
        return None
    if _installed is not None and _installed in sys.meta_path:
        return _installed
    finder = BootPinnedFinder(str(root), boot_sha, lambda: _sha_of(current_fingerprint()))
    sys.meta_path.insert(0, finder)
    _installed = finder
    return finder
