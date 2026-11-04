"""File passthrough registry for remote terminal backends (Docker, Modal, SSH).

Sandboxes start with no host files; this module tells them which credential files
(skill ``required_credential_files`` + ``terminal.credential_files`` config), skill
dirs, and host cache dirs to mount or sync in, at creation and before each command.
"""

from __future__ import annotations

import contextlib
import logging
import os
import posixpath
import stat
import threading
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from hermes_cli.config import cfg_get
from hermes_constants import get_hermes_dir, get_hermes_home

from agent.provider_media import GENERATED_SUBDIR, MEDIA_CACHE_MAX_AGE_HOURS
from agent.skill_utils import EXCLUDED_SKILL_DIRS

try:  # pragma: no cover - exercised via the fail-closed test below
    from agent.file_safety import get_read_block_error, read_deny_candidate_names
except ImportError:
    get_read_block_error = None  # type: ignore[assignment]
    read_deny_candidate_names = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Session-scoped registry; ContextVar prevents cross-session bleed in the gateway.
_registered_files_var: ContextVar[dict[str, str]] = ContextVar("_registered_files")

# Cache for config-based file list, one entry per profile home (tests reset it).
_config_files: dict[str, list[dict[str, str]]] = {}
# Serializes in-place refreshes of the sanitized skill copies within this process.
_safe_skills_lock = threading.Lock()


def _get_registered() -> dict[str, str]:
    val = _registered_files_var.get(None)
    if val is None:
        _registered_files_var.set(val := {})
    return val


def _mount(host_path: Path | str, container_path: str) -> dict[str, str]:
    return {"host_path": str(host_path), "container_path": container_path}


def _contained_host_path(rel: str, hermes_home: Path, abs_msg: str, traversal_msg: str) -> Optional[Path]:
    """Resolve *rel* under HERMES_HOME, refusing absolute paths and escapes."""
    if os.path.isabs(rel):
        logger.warning(abs_msg, rel)
        return None
    host_path = hermes_home / rel
    from tools.path_security import validate_within_dir  # resolves symlinks and ``..`` before checking

    if containment_error := validate_within_dir(host_path, hermes_home):
        logger.warning(traversal_msg, rel, containment_error)
        return None
    return host_path.resolve()


def _is_read_denied(resolved: Path, rel: str) -> bool:
    """True (logged) when *resolved* must not be mounted: a master store on the read deny-list.

    Master credential stores are never mountable, even though they sit inside HERMES_HOME and therefore
    pass the containment check. Shared by the skill route and the ``terminal.credential_files`` config
    route so neither can mount what the read surface denies. Fails CLOSED: if the canonical guard can't
    be consulted the mount is refused rather than risk bind-mounting auth.json into a sandbox. The
    sentinel + logger.exception keep guard failures debuggable instead of silently swallowed (#67665).
    """
    if get_read_block_error is None:
        logger.error("credential_files: refusing %r — agent.file_safety could not be "
                     "imported, so the master-store deny-list cannot be consulted", rel)
        return True
    try:
        denied = get_read_block_error(str(resolved))
    except Exception:
        logger.exception("credential_files: refusing %r — read guard raised", rel)
        return True
    if denied:
        logger.warning("credential_files: refused %r — it is a credential store the agent "
                       "is denied from reading; mount a per-service token, "
                       "not the master key files", rel)
        return True
    return False


def register_credential_file(relative_path: str, container_base: str = "/root/.hermes") -> bool:
    """Register a HERMES_HOME-relative credential file for mounting; True if it exists and was registered.

    Rejects absolute paths and traversal out of HERMES_HOME. Containment alone is not
    enough: HERMES_HOME holds the MASTER stores (``.env``, ``auth.json``, ``mcp-tokens/``),
    which are refused via the canonical read deny-list so the mount surface cannot hand a
    skill what the read surface denies. Fails CLOSED (logged) if the guard is unavailable or raises.
    """
    resolved = _contained_host_path(
        relative_path, get_hermes_home(),
        "credential_files: rejected absolute path %r (must be relative to HERMES_HOME)",
        "credential_files: rejected path traversal %r (%s)")
    if resolved is None:
        return False
    if not resolved.is_file():
        logger.debug("credential_files: skipping %s (not found)", resolved)
        return False
    if _is_read_denied(resolved, relative_path):
        return False

    container_path = f"{container_base.rstrip('/')}/{relative_path}"
    _get_registered()[container_path] = str(resolved)
    logger.debug("credential_files: registered %s -> %s", resolved, container_path)
    return True


def register_credential_files(entries: list, container_base: str = "/root/.hermes") -> list[str]:
    """Register skill-frontmatter entries (str or dict with ``path``); return missing paths."""
    missing = []
    for entry in entries:
        if isinstance(entry, dict):
            entry = entry.get("path") or entry.get("name") or ""
        elif not isinstance(entry, str):
            continue
        rel_path = entry.strip()
        if rel_path and not register_credential_file(rel_path, container_base):
            missing.append(rel_path)
    return missing


def _load_config_files() -> list[dict[str, str]]:
    """Load ``terminal.credential_files`` from config.yaml (cached per profile home: the
    multiplexed gateway must never mount the launch profile's credential files into a
    secondary profile's sandbox)."""
    from hermes_constants import hermes_home_key
    home_key = hermes_home_key()
    cached = _config_files.get(home_key)
    if cached is not None:
        return cached

    result: list[dict[str, str]] = []
    try:
        from hermes_cli.config import read_raw_config
        hermes_home = get_hermes_home()
        cred_files = cfg_get(read_raw_config(), "terminal", "credential_files")
        for item in cred_files if isinstance(cred_files, list) else []:
            rel = item.strip() if isinstance(item, str) else ""
            if not rel:
                continue
            resolved_path = _contained_host_path(
                rel, hermes_home,
                "credential_files: rejected absolute config path %r",
                "credential_files: rejected config path traversal %r (%s)")
            if (resolved_path is not None and resolved_path.is_file()
                    and not _is_read_denied(resolved_path, rel)):
                result.append(_mount(resolved_path, f"/root/.hermes/{rel}"))
    except Exception as e:
        logger.warning("Could not read terminal.credential_files from config: %s", e)

    _config_files[home_key] = result
    return result


def get_credential_file_mounts() -> list[dict[str, str]]:
    """Skill-registered + config credential files as ``host_path``/``container_path`` dicts (re-checked for existence)."""
    mounts = {cp: hp for cp, hp in _get_registered().items() if Path(hp).is_file()}
    for entry in _load_config_files():
        cp, hp = entry["container_path"], entry["host_path"]
        if cp not in mounts and Path(hp).is_file():
            mounts[cp] = hp
    return [_mount(hp, cp) for cp, hp in mounts.items()]


# --- Skills directory mounts ---

def _skill_dir_roots(container_base: str) -> Iterator[tuple[Path, str]]:
    """Yield ``(host_dir, container_root)`` for every existing skills directory.

    Local skills mount at ``<base>/skills``, external at ``<base>/external_skills/<i>``, trusted
    project-local at ``<base>/project_skills/<i>`` (own namespace so paths stay stable if external_dirs change).
    """
    base = container_base.rstrip("/")
    skills_dir = get_hermes_home() / "skills"
    if skills_dir.is_dir():
        yield skills_dir, f"{base}/skills"
    try:
        from agent.skill_utils import get_external_skills_dirs, get_project_skills_dirs
    except ImportError:
        return
    for label, dirs in (("external_skills", get_external_skills_dirs()), ("project_skills", get_project_skills_dirs())):
        yield from ((d, f"{base}/{label}/{idx}") for idx, d in enumerate(dirs) if d.is_dir())


def _skill_path_read_denied(path: Path) -> bool:
    """True when the canonical read guard denies *path* (``.env``/``.envrc`` basenames, the
    ``skills/.hub`` cache, credential stores), so neither the skills mount nor sync may ship it.

    Fails CLOSED like :func:`_is_read_denied`: an unavailable or raising guard withholds the path.
    """
    if get_read_block_error is None:
        return True
    try:
        return bool(get_read_block_error(str(path)))
    except Exception:
        logger.exception("credential_files: withholding skills path %s — read guard raised", path)
        return True


def _skill_entry_denier(root: Path) -> Optional[Callable[[Path], bool]]:
    """Per-walk deny predicate for entries under *root*; ``None`` when *root* itself is denied.

    The full guard costs ~ms per path and sync re-walks every few seconds, so only entries whose
    name is in ``read_deny_candidate_names()`` (computed once per walk) get the full check; an
    ordinary ``SKILL.md`` can't be denied by name, and its dirs were each checked on the way down.
    Fails CLOSED: without the pre-filter every entry is withheld.
    """
    if _skill_path_read_denied(root):
        return None
    if read_deny_candidate_names is None:
        return lambda _path: True
    try:
        names = read_deny_candidate_names()
    except Exception:
        logger.exception("credential_files: withholding skills tree %s — deny-name pre-filter raised", root)
        return lambda _path: True
    return lambda path: path.name.casefold() in names and _skill_path_read_denied(path)


def _walk_skill_tree(root: Path, *, skip_dir: Optional[Path] = None) -> Iterator[tuple[Path, list[Path]]]:
    """Yield ``(dir, regular_non_symlink_files)`` for every directory a sandbox should receive.

    Prunes ``EXCLUDED_SKILL_DIRS`` *before* descending so bookkeeping/dependency trees (``.hub``,
    ``.archive``, ``.curator_backups``, ``node_modules``, ``.git``, ...) the remote agent never reads
    are never even walked; sync thus agrees with discovery on what is skill content. Deliberately
    not ``is_excluded_skill_path()``: that also prunes ``references/``, ``templates/``, ``assets/``,
    ``scripts/`` — progressive-disclosure files and bundled scripts the sandbox does execute.
    Paths the read deny-list blocks (a skill's ``.env``, ``.envrc``, a denied dir, a denied root)
    are dropped too: the sandbox must not receive what the agent may not read on the host.
    *skip_dir* (the sanitized-copy store) is pruned too, so a skills root that contains it
    (an external dir pointed at ``HERMES_HOME``) never copies the copies into themselves.
    """
    denied = _skill_entry_denier(root)
    if denied is None:
        logger.warning("credential_files: withholding read-denied skills root from sandbox: %s", root)
        return
    skip = os.path.realpath(skip_dir) if skip_dir is not None else None
    skip_name = os.path.basename(skip) if skip else None
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_SKILL_DIRS and not denied(base / d)
                             and not (d == skip_name and os.path.realpath(base / d) == skip))
        yield base, [f for f in (base / n for n in filenames)
                     if not f.is_symlink() and f.is_file() and not denied(f)]


def get_skills_directory_mount(container_base: str = "/root/.hermes") -> list[dict[str, str]]:
    """Directory mount entries for all skill dirs (local + external + project).

    Every root mounts its sanitized copy (the sync view: no symlinks, no ``.hub``, nothing the
    read deny-list blocks), never the raw dir. A raw bind mount is live, so a ``.hub`` or a
    skill ``.env`` created after container start would reach the sandbox at once; the copy
    only ever receives files the guard allows. Each call refreshes the copies in place, so a
    running container that keeps calling :func:`refresh_skills_directory_mounts` (docker does,
    before commands) tracks its source in both directions.
    """
    return [_mount(_safe_skills_path(d), cp) for d, cp in _skill_dir_roots(container_base)]


def refresh_skills_directory_mounts() -> None:
    """Bring every skills root's mounted copy up to date with its source (one sync tick)."""
    for skills_dir, _container_root in _skill_dir_roots("/root/.hermes"):
        _safe_skills_path(skills_dir)


def _safe_skills_path(skills_dir: Path) -> str:
    """Refresh and return the sanitized copy of *skills_dir* (the same view as sync).

    The copy has a stable per-source path under ``<HERMES_HOME>/cache/sandbox-skills`` and is
    updated in place, never replaced: a container started by an earlier call, or by another
    process and reused here, keeps its bind mount and sees each refresh. (A per-process
    ``mkdtemp`` copy was deleted at that process's exit, emptying the mount of any container
    a later process reused.) Refreshes of one copy are serialized across threads and processes.

    The source is resolved once: the copy's key and the walk use the same realpath, so a
    root (or root ancestor) swapped between the two can never fill this copy from elsewhere.
    """
    from tools.skill_usage import skill_file_lock

    real = os.path.realpath(skills_dir)
    safe_dir = _sanitized_copy_dir(real)
    with _safe_skills_lock, skill_file_lock(safe_dir.with_name(f"{safe_dir.name}.lock")):
        return str(_refresh_sanitized_copy(Path(real), safe_dir))


def _sanitized_copy_store() -> Path:
    """Where the per-source sanitized skills copies live (stable across processes)."""
    return get_hermes_home() / "cache" / "sandbox-skills"


def _sanitized_copy_dir(skills_dir: Path | str) -> Path:
    import hashlib

    key = hashlib.sha256(os.path.realpath(skills_dir).encode("utf-8")).hexdigest()[:16]
    safe_dir = _sanitized_copy_store() / key
    if not safe_dir.is_dir():
        safe_dir.mkdir(parents=True, exist_ok=True)
        logger.info("credential_files: created sanitized skills copy of %s at %s", skills_dir, safe_dir)
    return safe_dir


def _remove_entry(path: Path) -> None:
    import shutil

    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


# --- Race-free source reads for the sanitized copy ---
#
# The copy's source may be writable by the very sandbox that mounts the copy (a trusted
# project's skills under a cwd mounted rw at /workspace, an rw docker volume), and the copy is
# refreshed on that sandbox's commands. Any path-based open between "vetted" and "read" can then
# be redirected by swapping a file, or any directory above it, for a symlink to a host file. So
# every source component is opened relative to its already-open parent with O_NOFOLLOW, and a
# file is copied from its own fd only after fstat proves it a regular file.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
# O_NONBLOCK: a FIFO swapped in after vetting must not block the open (fstat then rejects it).
_SOURCE_FILE_FLAGS = os.O_RDONLY | _O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | _O_CLOEXEC
_SOURCE_DIR_FLAGS = _SOURCE_FILE_FLAGS | _O_DIRECTORY
# Without these (Windows) there is no race-free way to read a sandbox-writable tree, so the
# sanitized copy is withheld (fails closed) instead of falling back to a path-following copy.
_ANCHORED_COPY_SUPPORTED = bool(
    _O_NOFOLLOW and _O_DIRECTORY
    and os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks and os.scandir in os.supports_fd
)
_withheld_roots_warned: set[str] = set()


def _open_dir_anchored(real_path: str) -> int:
    """Open the absolute, already-resolved *real_path* one component at a time from ``/``,
    never following a symlink: a component swapped for a link after resolution raises."""
    fd = os.open("/", _SOURCE_DIR_FLAGS)
    try:
        for part in real_path.split(os.sep):
            if part:
                child = os.open(part, _SOURCE_DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _walk_skill_tree_anchored(root: Path, *, skip_dir: Optional[Path] = None
                              ) -> Iterator[tuple[str, int, list[tuple[str, os.stat_result]]]]:
    """:func:`_walk_skill_tree`'s view of the resolved *root*, read through directory fds.

    Yields ``(rel, dir_fd, [(name, lstat), ...])``: *rel* is ``""`` or ``"/a/b"``, *dir_fd* the
    open directory to open the files from (valid only until the next item). Same pruning and
    deny rules as the path walk. Each entry is vetted before it is opened, and a directory that
    is no longer the one that was listed (now a symlink, a file, another inode) is skipped.
    Fails closed: a root that can't be opened without following a symlink yields nothing.
    """
    denied = _skill_entry_denier(root)  # vetted before the open: a swap after this fails the open
    if denied is None:
        logger.warning("credential_files: withholding read-denied skills root from sandbox: %s", root)
        return
    try:
        root_fd = _open_dir_anchored(str(root))
    except OSError as exc:
        logger.warning("credential_files: withholding skills root %s: not reachable without symlinks (%s)",
                       root, exc)
        return
    skip_id = None
    if skip_dir is not None:
        with contextlib.suppress(OSError):
            skip_st = os.stat(skip_dir)
            skip_id = (skip_st.st_dev, skip_st.st_ino)

    def walk(dir_fd: int, rel: str) -> Iterator[tuple[str, int, list[tuple[str, os.stat_result]]]]:
        base = Path(str(root) + rel)
        files: list[tuple[str, os.stat_result]] = []
        subdirs: list[tuple[str, os.stat_result]] = []
        with os.scandir(dir_fd) as entries:
            for entry in entries:
                try:
                    st = entry.stat(follow_symlinks=False)  # taken before vetting: what is vetted gets opened
                except OSError:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    if (entry.name not in EXCLUDED_SKILL_DIRS and (st.st_dev, st.st_ino) != skip_id
                            and not denied(base / entry.name)):
                        subdirs.append((entry.name, st))
                elif stat.S_ISREG(st.st_mode) and not denied(base / entry.name):
                    files.append((entry.name, st))
        yield rel, dir_fd, sorted(files, key=lambda item: item[0])
        for name, listed in sorted(subdirs, key=lambda item: item[0]):
            try:
                child = os.open(name, _SOURCE_DIR_FLAGS, dir_fd=dir_fd)
            except OSError as exc:  # swapped for a symlink/file, or gone, since it was listed
                logger.warning("credential_files: skipping skills dir %s: %s", base / name, exc)
                continue
            try:
                opened = os.fstat(child)
                if (opened.st_dev, opened.st_ino) != (listed.st_dev, listed.st_ino):
                    logger.warning("credential_files: skipping skills dir %s: replaced while walking", base / name)
                    continue
                yield from walk(child, f"{rel}/{name}")
            finally:
                os.close(child)

    try:
        yield from walk(root_fd, "")
    finally:
        os.close(root_fd)


def _copy_if_stale(dir_fd: int, name: str, listed: os.stat_result, dst: str) -> None:
    """Copy the regular file *name* under *dir_fd* over *dst* unless it already matches
    (size + ns mtime, which the copy keeps).

    The source is opened ``O_NOFOLLOW`` relative to its directory fd and read from that fd only
    after ``fstat`` shows a regular file, so a symlink, FIFO or device swapped in after the walk
    vetted it raises instead of being followed. Written to a sibling temp file and
    ``os.replace``d in: a live container never reads a half-written file, and a read-only
    (0444) previous copy can still be replaced.
    """
    import shutil

    try:
        dst_st = os.lstat(dst)
        if (stat.S_ISREG(dst_st.st_mode) and listed.st_size == dst_st.st_size
                and listed.st_mtime_ns == dst_st.st_mtime_ns):
            return
    except FileNotFoundError:
        pass
    if os.path.isdir(dst) and not os.path.islink(dst):
        shutil.rmtree(dst, ignore_errors=True)
    src_fd = os.open(name, _SOURCE_FILE_FLAGS, dir_fd=dir_fd)
    try:
        src_st = os.fstat(src_fd)
        if not stat.S_ISREG(src_st.st_mode):
            raise OSError(f"not a regular file: {name}")
        tmp = os.path.join(os.path.dirname(dst), f".{os.path.basename(dst)}.hermes-tmp")
        _remove_entry(Path(tmp))
        try:
            out_fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC, 0o600)
            with open(out_fd, "wb") as fdst, open(src_fd, "rb", closefd=False) as fsrc:
                shutil.copyfileobj(fsrc, fdst, 1 << 20)
            os.chmod(tmp, stat.S_IMODE(src_st.st_mode) & 0o777)  # never carry setuid/setgid into a mount
            os.utime(tmp, ns=(src_st.st_atime_ns, src_st.st_mtime_ns))
            os.replace(tmp, dst)
        except OSError:
            _remove_entry(Path(tmp))
            raise
    finally:
        os.close(src_fd)


def _refresh_sanitized_copy(skills_dir: Path, safe_dir: Path) -> Path:
    """Make *safe_dir* hold exactly the sync view of the resolved *skills_dir*, in place.

    Runs on every docker refresh tick, so it keys on plain path strings: the ``Path``
    arithmetic (``relative_to``, hashing) cost more than the syscalls on a 5k-file tree.
    Reads go through :func:`_walk_skill_tree_anchored` (see there for why); where that is
    unavailable the copy is emptied and stays empty rather than copied by path.
    """
    dst_root = str(safe_dir)
    wanted_dirs: set[str] = set()
    wanted_files: set[str] = set()
    if _ANCHORED_COPY_SUPPORTED:
        tree = _walk_skill_tree_anchored(skills_dir, skip_dir=safe_dir.parent)
    else:
        if str(skills_dir) not in _withheld_roots_warned:
            _withheld_roots_warned.add(str(skills_dir))
            logger.warning("credential_files: withholding skills %s from the sandbox mount: this platform has"
                           " no O_NOFOLLOW/dir_fd opens, so a race-free copy is impossible", skills_dir)
        tree = iter(())
    for rel, dir_fd, files in tree:
        dst_dir = dst_root + rel
        wanted_dirs.add(dst_dir)
        if os.path.islink(dst_dir) or (os.path.lexists(dst_dir) and not os.path.isdir(dst_dir)):
            _remove_entry(Path(dst_dir))  # was a file, is now a dir (parents were fixed on the way down)
        os.makedirs(dst_dir, exist_ok=True)
        for name, listed in files:
            dst = os.path.join(dst_dir, name)
            try:
                _copy_if_stale(dir_fd, name, listed, dst)
            except OSError as exc:  # unreadable, vanished or swapped mid-walk: skip it, don't drop every mount
                logger.warning("credential_files: skipping unreadable skills file %s%s/%s: %s",
                               skills_dir, rel, name, exc)
                continue
            wanted_files.add(dst)
    # Drop what the source no longer has (or now withholds), deepest first; never the root.
    for dirpath, _dirnames, filenames in os.walk(dst_root, topdown=False):
        for name in filenames:
            path = os.path.join(dirpath, name)
            if path not in wanted_files:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(path)
        if dirpath != dst_root and dirpath not in wanted_dirs:
            with contextlib.suppress(OSError):
                os.rmdir(dirpath)
    return safe_dir


def iter_skills_files(container_base: str = "/root/.hermes") -> list[dict[str, str]]:
    """Per-file entries for all skills files (for backends that upload individually)."""
    store = _sanitized_copy_store()
    return [_mount(item, f"{container_root}/{item.relative_to(host_dir).as_posix()}")
            for host_dir, container_root in _skill_dir_roots(container_base)
            for _base, files in _walk_skill_tree(host_dir, skip_dir=store) for item in files]


# --- Cache directory mounts (documents, images, audio, videos, screenshots) ---

# (new_subpath, old_name) pairs matching hermes_constants.get_hermes_dir().
_GENERATED_CACHE = f"cache/{GENERATED_SUBDIR}"
_CACHE_DIRS: list[tuple[str, str]] = [
    ("cache/documents", "document_cache"),
    ("cache/images", "image_cache"),
    ("cache/audio", "audio_cache"),
    ("cache/videos", "video_cache"),
    ("cache/screenshots", "browser_screenshots"),
    ("cache/web", "web_cache"),
    ("cache/delegation", "delegation_cache"),
    ("cache/spillover", "cache/spillover"),  # oversized tool results; host side is canonical
    # Unswept generated image/video deliverables (#126445) need their own mount/sync
    # entry or remote backends never see them. No legacy alias exists.
    (_GENERATED_CACHE, _GENERATED_CACHE),
    # Flat top-level desktop staging dirs (tui_gateway attach RPCs; no legacy alias),
    # mounted so vision/file tools in sandboxes reach uploads and dropped files.
    # Mount it so vision can reach uploads inside sandbox containers (#69575). No legacy alias exists, so
    # both tuple slots are ``images``.
    ("images", "images"),
    # Mount it so the agent's file tools can read dropped binaries (zip/pdf/...) from inside sandbox
    # containers instead of dangling host paths (#76577).
    ("attachments", "attachments"),
    # Desktop stages a large plain-text paste as a `.txt` under this Hermes-managed dir
    # (apps/desktop/electron/composer-paste.ts; `COMPOSER_PASTES_DIRNAME` in
    # agent/context_references.py) and attaches it as `@file:`. Without a mount/sync
    # entry, remote execution backends (ssh/daytona/vercel_sandbox) never received the
    # bytes and `to_agent_visible_cache_path` left the gateway-host path dangling on
    # the remote host (#110174). No legacy alias, so both tuple slots match.
    ("composer-pastes", "composer-pastes"),
]


def _cache_dir_roots(container_base: str, *, create_missing: bool) -> Iterator[tuple[Path, str]]:
    """Yield ``(host_dir, container_root)`` per cache dir; always maps to the *new* container layout."""
    base = container_base.rstrip("/")
    for new_subpath, old_name in _CACHE_DIRS:
        host_dir = get_hermes_dir(new_subpath, old_name)
        if not host_dir.is_dir():
            if not create_missing:
                continue
            # Docker snapshots this list at container CREATION, so a dir appearing later
            # would dangle for the container's life: create it now (empty bind mount is free).
            # get_hermes_dir already picked new-vs-legacy, so this can't shadow a legacy dir.
            try:
                # Create missing staging dirs instead of skipping them: Docker snapshots this mount list at
                # container CREATION, so a dir that appears later (first desktop attachment, first clipboard
                # image) would dangle for the whole life of a persistent container (#76577). An empty
                # bind-mounted dir costs nothing; a missing mount costs the feature. get_hermes_dir()
                # already resolved new-vs-legacy layout, so creating its answer cannot shadow a populated
                # legacy dir.
                host_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue  # unwritable home (tests, RO mounts) — skip as before
        yield host_dir, f"{base}/{new_subpath}"


def get_cache_directory_mounts(container_base: str = "/root/.hermes") -> list[dict[str, str]]:
    """Bind-mount entries for each cache directory (host layout via ``get_hermes_dir``)."""
    return [_mount(h, c) for h, c in _cache_dir_roots(container_base, create_missing=True)]


def _remap_cache_path(path: str, container_base: str, src: str, dst: str, join: Callable[[str, Path], str]) -> Optional[str]:
    """Translate *path* from the *src* side of a cache mount to its *dst* side; None if unmounted."""
    for mount in get_cache_directory_mounts(container_base=container_base):
        if Path(path).is_relative_to(mount[src]):
            return join(mount[dst], Path(path).relative_to(mount[src]))
    return None


def map_cache_path_to_container(host_path: str, container_base: str = "/root/.hermes") -> Optional[str]:
    """POSIX container path for a host path under an auto-mounted cache dir, else None.

    Also matches through symlinks: ``@file:`` expansion hands over RESOLVED paths while the mount roots keep
    HERMES_HOME's configured spelling, so a symlinked home (``~/.hermes`` -> dotfiles, macOS ``/var`` ->
    ``/private/var``) left a staged attachment's host path in front of the sandboxed agent (#103147)."""
    def join(root: str, rel: Path) -> str:
        return posixpath.join(root, rel.as_posix())

    mapped = _remap_cache_path(host_path, container_base, "host_path", "container_path", join)
    if mapped is not None:
        return mapped
    try:
        real = Path(host_path).resolve()
        for mount in get_cache_directory_mounts(container_base=container_base):
            root = Path(mount["host_path"]).resolve()
            if real.is_relative_to(root):
                return join(mount["container_path"], real.relative_to(root))
    except (OSError, RuntimeError):
        pass
    return None


def from_agent_visible_cache_path(container_path: str, container_base: str = "/root/.hermes") -> str:
    """Inverse of :func:`to_agent_visible_cache_path`; unchanged unless Docker + cache dir."""
    if _terminal_backend() != "docker":
        return container_path
    mapped = _remap_cache_path(container_path, container_base, "container_path", "host_path", lambda root, rel: str(Path(root) / rel))
    return mapped if mapped is not None else container_path


# Backends whose file-sync lands under the remote home: ``~/.hermes`` is
# expanded by the remote shell, so it resolves regardless of the actual home.
_HOME_RELATIVE_BACKENDS = frozenset({"ssh", "daytona", "vercel_sandbox", "kubernetes"})


def _terminal_backend() -> str:
    """Active ``TERMINAL_ENV`` through the per-turn terminal scope (a routed multiplex profile's
    backend, never the launch profile's process env)."""
    from tools.terminal_scope import terminal_env
    return (terminal_env("TERMINAL_ENV") or "local").strip().lower()


def to_agent_visible_cache_path(host_path: str, container_base: str = "/root/.hermes") -> str:
    """Translate a host cache path to where the active backend (TERMINAL_ENV) sees it.

    Mirrors ``_agent_cache_base_for_env`` in tools/image_generation_tool.py: docker/modal mount at
    ``/root/.hermes``; ssh/daytona/vercel_sandbox under ``~/.hermes``; plugin backends declare
    ``cache_path_base`` (None = host paths stay correct, e.g. the singularity plugin); local/unknown
    unchanged.

    * docker / modal — bind-mounted (docker) or per-file-synced (modal) at ``/root/.hermes`` (the
    *container_base* default). * ssh / daytona / vercel_sandbox — file-synced under the remote user's home;
    ``~/.hermes`` is shell-expanded by the remote shell, so tool commands resolve it regardless of the
    actual remote home. Previously these backends synced the bytes but still rendered the dangling host path
    (#76577 gap).
    """
    backend = _terminal_backend()
    if backend in _HOME_RELATIVE_BACKENDS:
        container_base = "~/.hermes"
    elif backend not in ("docker", "modal"):
        try:
            from agent.terminal_env_registry import provider_flag
            plugin_base = provider_flag(backend, "cache_path_base", None)
        except Exception:
            plugin_base = None
        if not plugin_base:
            return host_path
        container_base = str(plugin_base)

    mapped = map_cache_path_to_container(host_path, container_base=container_base)
    return mapped if mapped is not None else host_path


def iter_cache_files(container_base: str = "/root/.hermes") -> list[dict[str, str]]:
    """Per-file cache entries (Modal upload/resync); skips symlinks. ``cache/generated`` is
    never swept, so only its files from the last ``MEDIA_CACHE_MAX_AGE_HOURS`` are synced —
    otherwise every remote sync would re-walk and upload the whole generation history."""
    generated_cutoff = time.time() - MEDIA_CACHE_MAX_AGE_HOURS * 3600
    gen_root = f"{container_base.rstrip('/')}/{_GENERATED_CACHE}"
    entries: list[dict[str, str]] = []
    for host_dir, root in _cache_dir_roots(container_base, create_missing=False):
        for item in host_dir.rglob("*"):
            try:
                st = item.lstat()
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and (root != gen_root or st.st_mtime >= generated_cutoff):
                entries.append(_mount(item, f"{root}/{item.relative_to(host_dir)}"))
    return entries


def clear_credential_files() -> None:
    """Reset the skill-scoped registry (e.g. on session reset)."""
    _get_registered().clear()
