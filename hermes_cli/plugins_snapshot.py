"""Persisted record of one plugin discovery, for readers that only need its toolset effects. [daveey]

The kanban admission check (``kanban_capabilities._profile_capabilities``) asks one question of a
profile's toolset policy: does the cli platform grant ``terminal`` and ``file``? Answering it by
discovering every plugin imports ~60 plugins in a cold helper process, which took 10-17 s on a
loaded Mac under background-tier I/O. Plugin discovery can change that answer in only three ways:

* a plugin tool takes a built-in tool name (an override), or lands in a built-in toolset;
* the ``TOOLSETS`` table or the registry's toolset aliases change while plugins load;
* the plugin toolset keys / portable MCP names it reports differ (read by ``_get_platform_tools``).

:func:`record` writes, after a real discovery, the keys and names it found plus whether any
of the first two happened, stamped with :func:`fingerprint` of the discovery inputs taken BEFORE
the sweep. :func:`load` returns that record only while the fingerprint still matches and the
sweep showed no built-in collision; any other state returns None, and callers run the full
discovery exactly as before. The fingerprint is stat-only (no file reads, no imports).
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

SNAPSHOT_VERSION = 1
# Files a discovery sweep reads or imports under a plugin root.
_PLUGIN_SUFFIXES = (".py", ".yaml", ".yml", ".json", ".toml", ".pth")
_SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".venv", "venv", ".mypy_cache", ".pytest_cache"}
# Core modules whose code decides what discovery loads and how toolsets resolve.
_CORE_MODULES = (
    "hermes_cli/plugins.py", "hermes_cli/plugins_loader.py", "hermes_cli/plugins_discovery.py",
    "hermes_cli/plugins_manifest.py", "hermes_cli/plugin_capabilities.py", "hermes_cli/plugins_snapshot.py",
    "hermes_cli/tools_config.py", "toolsets.py", "tools/registry.py", "model_tools.py",
)
# Env flags discovery reads. (HERMES_HOME is not one: the home the sweep scans is hashed itself,
# so a helper that scopes into a profile and that profile's own sessions share one record.)
_ENV_INPUTS = ("HERMES_SAFE_MODE", "HERMES_ENABLE_PROJECT_PLUGINS", "HERMES_BUNDLED_PLUGINS",
               "HERMES_NEMO_RELAY_PLUGINS_TOML")


@dataclass(frozen=True)
class Snapshot:
    toolset_keys: frozenset[str]
    portable_mcp: frozenset[str]


_active: contextvars.ContextVar[Optional[Snapshot]] = contextvars.ContextVar("plugin_snapshot", default=None)


def active() -> Optional[Snapshot]:
    """The snapshot ``plugins._nowait_plugin_set`` serves instead of discovering, if one is active."""
    return _active.get()


@contextmanager
def serving(snapshot: Snapshot) -> Iterator[None]:
    """Serve ``snapshot`` to the ``*_nowait`` probes, with discovery suppressed so nothing inside
    loads plugins behind the reader's back (the manager stays undiscovered for later consumers)."""
    from hermes_cli.plugins_discovery import suppress_plugin_discovery

    token = _active.set(snapshot)
    try:
        with suppress_plugin_discovery():
            yield
    finally:
        _active.reset(token)


def _cache_path(home: Path) -> Path:
    return home / "cache" / "plugin_toolset_keys.json"


def _stat_entry(path: Path, label: str) -> list:
    # [daveey] No st_ctime_ns: it moves on every metadata write (chmod, utime, xattr), not only on
    # content changes. hermes-sync's `rsync -a --checksum` (macOS openrsync) re-applies mode/times to
    # every unchanged plugin file each ~10 min pass, which made the record stale on every pass, so the
    # bb gate's next cold assign paid a full discovery (20-45 s) again. A content change still moves
    # one of the kept fields: an in-place write sets a new mtime, and a sync that restores the source
    # mtime writes a temp file and renames it (new inode). Only an in-place, same-size rewrite that
    # then forges the old mtime back goes unseen (cp -p / rsync --inplace onto an existing file); no
    # path hermes uses does that (hermes-sync rsync without --inplace, git, pip, editors).
    try:
        st = path.stat()
    except OSError as exc:
        return [label, "missing", exc.errno]
    return [label, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns]


_DIST_SUFFIXES = (".dist-info", ".egg-info", ".egg-link", ".pth", ".egg")


def _site_entry(path: Path, label: str) -> list:
    """A site dir's installed-distribution entries (not the dir's own mtime, which also moves
    whenever any process first byte-compiles a top-level module into ``__pycache__``)."""
    try:
        names = sorted(n for n in os.listdir(path) if n.endswith(_DIST_SUFFIXES))
    except OSError as exc:
        return [label, "missing", exc.errno]
    return [label, [_stat_entry(path / n, n) for n in names]]


def _root_is_empty(root: Path) -> bool:
    """True when ``root`` is missing or holds nothing but skipped dirs: discovery loads the same
    (nothing) from either, so a process creating an empty ``<home>/plugins`` after boot must not
    read as a changed input."""
    try:
        return not any(n not in _SKIP_DIRS for n in os.listdir(root))
    except OSError:
        return True


def _walk_root(root: Path, label: str, out: list) -> None:
    if not root.is_dir() or _root_is_empty(root):
        out.append([label, "absent"])
        return
    seen: set[tuple[int, int]] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        try:
            st = os.stat(dirpath)
        except OSError:
            continue
        if (st.st_dev, st.st_ino) in seen:  # a symlink cycle
            dirnames[:] = []
            continue
        seen.add((st.st_dev, st.st_ino))
        rel = os.path.relpath(dirpath, root)
        # The directory's identity only: its mtime also moves when ANY process (a sibling test worker,
        # a pytest run importing a bundled plugin) first creates a __pycache__ in it, which made a
        # record stale between two cold processes. Added/removed/renamed plugin files and
        # subdirectories already show up below as entries of their own.
        out.append([f"{label}:{rel}/", st.st_dev, st.st_ino])
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for name in sorted(filenames):
            if name.endswith(_PLUGIN_SUFFIXES):
                out.append(_stat_entry(Path(dirpath) / name, f"{label}:{rel}/{name}"))


def fingerprint(home: Optional[Path] = None) -> Optional[str]:
    """Stat-only digest of what a discovery sweep in ``home`` reads: every plugin root's files
    (bundled, user, opt-in project), the profile config and .env, the discovery env flags, the
    site-packages dirs (a pip install or uninstall changes an entry-point set), and the core
    modules that gate plugins and resolve toolsets. Independent of the calling process's script,
    cwd and priority, so every process scanning ``home`` with the same inputs shares one record.
    None when it cannot be computed."""
    try:
        from hermes_cli import plugins as _plugins

        home = Path(home) if home is not None else get_hermes_home()
        from hermes_cli._startup_fast import project_root_str
        repo = Path(project_root_str())
        parts: list = [SNAPSHOT_VERSION, sys.version, sys.prefix, str(home), str(repo)]
        parts.append([[name, os.environ.get(name)] for name in _ENV_INPUTS])
        for name in ("config.yaml", ".env"):
            parts.append(_stat_entry(home / name, name))
        for rel in _CORE_MODULES:
            parts.append(_stat_entry(repo / rel, rel))
        # Installed distributions (entry-point plugins) live in site dirs; a pip install or
        # uninstall changes the directory. Script and source dirs on sys.path differ per caller
        # and hold no distributions, so they are left out to keep the record shared.
        for entry in sorted({e for e in sys.path if e and os.path.basename(e) in ("site-packages", "dist-packages")}):
            parts.append(_site_entry(Path(entry), f"syspath:{entry}"))
        roots = [("bundled", Path(_plugins.get_bundled_plugins_dir())), ("user", home / "plugins")]
        if _plugins._env_enabled("HERMES_ENABLE_PROJECT_PLUGINS"):
            roots.append(("project", Path.cwd() / ".hermes" / "plugins"))
        for label, root in roots:
            files: list = []
            _walk_root(root, label, files)
            parts.append([label, str(root), files])
        return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()
    except Exception:
        logger.debug("plugin discovery fingerprint failed", exc_info=True)
        return None


def precheck(home: Optional[Path] = None) -> Optional[str]:
    """[daveey] A cheap subset of :func:`fingerprint`'s inputs (~1 ms against ~15-20 ms on the hub's real
    plugin tree): the discovery env flags, config.yaml and .env (``hermes plugins enable`` writes
    ``plugins.enabled``), the user plugin root's entries and each top-level plugin dir's manifest and
    ``__init__.py``. Unchanged means a long-lived process skips the full fingerprint at a session
    boundary; a deep in-place edit of a plugin module is the one input it misses. None when it cannot
    be computed (callers then compute the full fingerprint)."""
    try:
        from hermes_cli import plugins as _plugins

        home = Path(home) if home is not None else get_hermes_home()
        parts: list = [str(home), [[name, os.environ.get(name)] for name in _ENV_INPUTS]]
        for name in ("config.yaml", ".env"):
            parts.append(_stat_entry(home / name, name))
        roots = [Path(_plugins.get_bundled_plugins_dir()), home / "plugins"]
        if _plugins._env_enabled("HERMES_ENABLE_PROJECT_PLUGINS"):
            roots.append(Path.cwd() / ".hermes" / "plugins")
        for root in roots:
            try:
                names = sorted(n for n in os.listdir(root) if n not in _SKIP_DIRS)
            except OSError:
                names = []
            if not names:  # missing and empty load the same (nothing): see _root_is_empty
                parts.append([str(root), "absent"])
                continue
            parts.append(_stat_entry(root, str(root)))
            for name in names:
                entry = root / name
                parts.append(_stat_entry(entry, name))  # follows a symlinked plugin to its directory
                for leaf in ("plugin.yaml", "plugin.yml", "__init__.py"):
                    parts.append(_stat_entry(entry / leaf, f"{name}/{leaf}"))
        return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()
    except Exception:
        logger.debug("plugin discovery precheck failed", exc_info=True)
        return None


def builtin_collision() -> Optional[str]:
    """Why the live (discovered) plugin set may change built-in toolset membership, or None."""
    import toolsets
    from hermes_cli import plugins as _plugins
    from tools.registry import registry

    if toolsets.TOOLSETS != toolsets._STATIC_TOOLSETS:
        return "TOOLSETS changed during discovery"
    if getattr(registry, "_toolset_aliases", None):
        return "registry toolset aliases present"
    static_tools = {t for key in toolsets._STATIC_TOOLSETS
                    for t in toolsets.resolve_toolset(key, include_registry=False)}
    for name in sorted(_plugins.get_plugin_manager()._plugin_tool_names):
        if name in static_tools:
            return f"plugin tool {name!r} takes a built-in tool name"
        entry = registry.get_entry(name)
        if entry is not None and entry.toolset in toolsets._STATIC_TOOLSETS:
            return f"plugin tool {name!r} lands in built-in toolset {entry.toolset!r}"
    return None


def record(stamp: Optional[str], home: Optional[Path] = None) -> None:
    """Persist the live registry's toolset effects, stamped with the pre-sweep ``stamp``
    (best-effort: a failed write only means the next reader discovers again)."""
    try:
        from hermes_cli import plugins as _plugins
        from utils import atomic_json_write

        home = Path(home) if home is not None else get_hermes_home()
        keys = sorted({ts_key for ts_key, _, _ in _plugins.get_plugin_toolsets()})
        try:
            portable = sorted(_plugins.get_plugin_manager().get_portable_mcp_servers())
        except Exception:  # health: allow BLE001 -- same fallback as the record this extends: no portable MCP names
            portable = []
        blob = {"toolset_keys": keys, "portable_mcp": portable, "version": SNAPSHOT_VERSION,
                "fingerprint": stamp, "builtin_collision": builtin_collision()}
        atomic_json_write(_cache_path(home), blob, indent=None, mode=0o600)  # type: ignore[arg-type]
    except Exception:
        logger.debug("plugin discovery snapshot write failed", exc_info=True)


# --- Hook presence -------------------------------------------------------------------------------
# [daveey] ``plugins.has_hook`` gates observer hooks (kanban ``on_kanban_task_updated`` after every
# assign) and, in a process that never discovered, used to import every plugin first: 38-57 s in a
# cold background-QoS helper on a loaded Mac. Every discovery now records the hook names its plugins
# handle, stamped with the same pre-sweep :func:`fingerprint`, in a file of its own (so its writers
# never invalidate the toolset record above). :func:`load_hooks` serves the set only while the
# fingerprint matches; a missing, corrupt, stale or incomplete record returns None and the caller
# discovers exactly as before. The set over-approximates: an over-reported hook only costs the
# discovery the caller's invoke runs anyway, while an under-reported one would drop an observer, so:
#
# * a sweep in which any plugin's load timed out (its register() never finished) does not vouch for
#   the set; it only adds to a complete record with the same stamp, never writes one of its own;
# * hooks a manifest declares (``provides_hooks``) count even when the plugin did not register them;
# * records with the same stamp are unioned, so processes that load the same inputs differently
#   (another surface, a config-owned shell hook) can only grow the set.
#
# Contract for plugins: whether register() subscribes a hook must depend only on fingerprinted
# inputs (plugin files, config.yaml, .env, the env flags above, installed packages); a hook gated on
# anything else (another env var, argv, the surface, a data file) must be listed in the manifest's
# ``provides_hooks``. A register() that raises (other than a timeout) is recorded as contributing
# only its declared hooks: a plugin that always fails would otherwise prevent any record.

HOOKS_VERSION = 1


def _hooks_path(home: Path) -> Path:
    return home / "cache" / "plugin_hooks.json"


def discovered_hooks(manager) -> Optional[set[str]]:
    """Hook names ``manager``'s sweep handles (registered or declared); None when a plugin's load
    timed out, since what it would have registered is unknown."""
    plugins = list(dict(getattr(manager, "_plugins", {}) or {}).values())
    if any(getattr(p, "load_timed_out", False) for p in plugins):
        return None
    names = {name for name, callbacks in dict(getattr(manager, "_hooks", {}) or {}).items() if callbacks}
    for plugin in plugins:
        declared = getattr(getattr(plugin, "manifest", None), "provides_hooks", None) or ()
        if isinstance(declared, (list, tuple)):
            names.update(h for h in declared if isinstance(h, str))
    return names


def _read_hooks_blob(home: Path) -> Optional[dict]:
    try:
        blob = json.loads(_hooks_path(home).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(blob, dict) or blob.get("version") != HOOKS_VERSION:
        return None
    stamp, hooks = blob.get("fingerprint"), blob.get("hooks")
    if not isinstance(stamp, str) or not isinstance(hooks, list) or not all(isinstance(h, str) for h in hooks):
        return None
    return blob


def record_hooks(stamp: Optional[str], manager, home: Optional[Path] = None) -> None:
    """Persist ``manager``'s hook set stamped with the pre-sweep ``stamp`` (best-effort; see above)."""
    if stamp is None:
        return
    try:
        from utils import atomic_json_write

        home = Path(home) if home is not None else get_hermes_home()
        names = discovered_hooks(manager)
        prior = _read_hooks_blob(home)
        prior_hooks = set(prior["hooks"]) if prior is not None and prior["fingerprint"] == stamp else None
        if names is None:
            if prior_hooks is None:
                return  # incomplete, and no complete record with these inputs to add to
            names = prior_hooks | {n for n, cbs in dict(getattr(manager, "_hooks", {}) or {}).items() if cbs}
        elif prior_hooks is not None:
            names |= prior_hooks
        if prior_hooks == names:
            return  # nothing new; spare the write
        blob = {"version": HOOKS_VERSION, "fingerprint": stamp, "hooks": sorted(names)}
        atomic_json_write(_hooks_path(home), blob, indent=None, mode=0o600)  # type: ignore[arg-type]
    except Exception:
        logger.debug("plugin hook record write failed", exc_info=True)


def load_hooks(home: Optional[Path] = None) -> Optional[frozenset[str]]:
    """The recorded hook set for ``home`` while its discovery inputs are unchanged; None otherwise."""
    try:
        home = Path(home) if home is not None else get_hermes_home()
        blob = _read_hooks_blob(home)
        if blob is None or fingerprint(home) != blob["fingerprint"]:
            return None
        return frozenset(blob["hooks"])
    except Exception:
        logger.debug("plugin hook record read failed", exc_info=True)
        return None


def served_hooks(manager) -> Optional[frozenset[str]]:
    """What ``plugins.has_hook`` answers from for an undiscovered ``manager``: its home's recorded
    hook set, or None when discovery must decide (already discovered, discovery suppressed, a
    background sweep in flight to join instead, or no usable record)."""
    from hermes_cli import plugins as _plugins
    from hermes_cli.plugins_discovery import plugin_discovery_suppressed

    if getattr(manager, "_discovered", True) or plugin_discovery_suppressed():
        return None
    t = _plugins._background_discovery_thread
    if t is not None and t.is_alive():
        return None
    home = getattr(manager, "home_path", None)
    return load_hooks(Path(home)) if home is not None else None


def load(home: Optional[Path] = None) -> Optional[Snapshot]:
    """The last recorded discovery for ``home`` when its inputs are unchanged and it showed no
    built-in collision; None otherwise (missing, corrupt, older format, stale, or colliding)."""
    home = Path(home) if home is not None else get_hermes_home()
    try:
        blob = json.loads(_cache_path(home).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(blob, dict) or blob.get("version") != SNAPSHOT_VERSION:
        return None
    stamp = blob.get("fingerprint")
    if not isinstance(stamp, str) or blob.get("builtin_collision") is not None:
        return None
    keys, portable = blob.get("toolset_keys"), blob.get("portable_mcp")
    if not all(isinstance(v, list) and all(isinstance(x, str) for x in v) for v in (keys, portable)):
        return None
    if fingerprint(home) != stamp:
        return None
    return Snapshot(frozenset(keys or ()), frozenset(portable or ()))
