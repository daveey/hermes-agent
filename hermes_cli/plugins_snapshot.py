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
    try:
        st = path.stat()
    except OSError as exc:
        return [label, "missing", exc.errno]
    return [label, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def _walk_root(root: Path, label: str, out: list) -> None:
    if not root.is_dir():
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
        # The directory's own entry changes when a file is added, removed or renamed.
        out.append([f"{label}:{rel}/", st.st_dev, st.st_ino, st.st_mtime_ns])
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
        repo = Path(__file__).resolve().parent.parent
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
            parts.append(_stat_entry(Path(entry), f"syspath:{entry}"))
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
