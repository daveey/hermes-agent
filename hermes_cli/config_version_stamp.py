"""The config schema-version stamp: raw reads of ``_config_version`` from config.yaml.

Split out of :mod:`hermes_cli.config` (code-health size ratchet); ``hermes_cli.config``
late-imports these and re-exports them, and :mod:`scripts.docker_config_migrate` reads the
raw stamp directly. ``_coerce_config_version`` stays module-private here.
"""

from __future__ import annotations

import os
import threading
from typing import Optional, Tuple

from hermes_cli.config_defaults import DEFAULT_CONFIG


def _coerce_config_version(value) -> int:
    """Return a safe integer config version, treating invalid values as legacy."""
    if isinstance(value, bool):
        return 0
    try:
        version = int(value)
    except (TypeError, ValueError):
        return 0
    return max(version, 0)


# [daveey] path -> (file_signature, root type name or None for a mapping, stamp or _NO_KEY).
# ``/api/status`` reads the stamp on every poll; re-parsing a ~2500-line config.yaml there held the
# GIL ~40 ms per call and, in a Desktop wake burst, stretched every other request queued behind it
# (2026-10-29: /api/sessions/<id>, /api/session-kanban-tasks past the 5 s client timeout). Only
# successful parses are cached; a parse error re-reads, warns and raises exactly as before.
_NO_KEY = object()
_stamp_cache: dict[str, tuple[tuple, Optional[str], object]] = {}
_stamp_cache_lock = threading.Lock()


def _parsed_stamp(config_path) -> tuple[Optional[str], object]:
    """``(non-mapping root type name or None, stamp value or _NO_KEY)`` for config.yaml, re-parsed
    only when its file signature changes. Parse errors propagate and are not cached."""
    # Upstream routes config reads through hermes_cli.config_backend; the cache keys on its
    # parse-cache signature and parses through it, so non-file backends work too.
    from hermes_cli.config_backend import config_cache_version, read_config_doc

    try:
        sig = config_cache_version(config_path)
    except OSError:
        sig = None
    key = str(config_path)
    if sig is not None:
        with _stamp_cache_lock:
            hit = _stamp_cache.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1], hit[2]
    config = read_config_doc(config_path)
    if config is None:
        config = {}  # empty file / bare document: valid first-run state
    if isinstance(config, dict):
        result: tuple[Optional[str], object] = (None, config.get("_config_version", _NO_KEY))
    else:
        result = (type(config).__name__, _NO_KEY)
    if sig is not None:
        with _stamp_cache_lock:
            _stamp_cache[key] = (sig, result[0], result[1])
    return result


def read_config_version_stamp(*, raise_on_parse_error: bool = False) -> tuple[Optional[int], int]:
    """Single raw read behind ``check_config_version()``: ``(stamp, latest_version)`` where
    *stamp* is ``None`` when config.yaml parsed but carries no ``_config_version`` key (a
    never-stamped current-schema file, not an ancient install — ``migrate_config()`` gives it only
    the legacy-key steps). A missing file, or malformed YAML under a tolerant caller, reads as
    ``latest`` exactly as ``check_config_version()`` always reported it. The parse is cached on
    the file's signature (``_parsed_stamp``), so a rewrite is seen on the next call."""
    from hermes_cli.config import InvalidUserConfigError, _warn_config_parse_failure, get_config_path
    from hermes_cli.config_backend import config_exists

    latest = _coerce_config_version(DEFAULT_CONFIG.get("_config_version", 1)) or 1
    config_path = get_config_path()
    if not config_exists(config_path):
        return latest, latest

    try:
        non_mapping, value = _parsed_stamp(config_path)
    except Exception as e:
        _warn_config_parse_failure(config_path, e)
        if raise_on_parse_error:
            raise InvalidUserConfigError(
                f"Cannot inspect {config_path}: config.yaml is not valid YAML ({e})"
            ) from e
        return latest, latest

    if non_mapping is not None:
        # A list/scalar root parses fine but is just as unusable as broken YAML: save_config()
        # would refuse it later, after .env was already rewritten. Strict callers see it up front.
        if raise_on_parse_error:
            raise InvalidUserConfigError(
                f"Cannot inspect {config_path}: config.yaml top-level value must be "
                f"a mapping, got {non_mapping}"
            )
        return None, latest
    if value is _NO_KEY:
        return None, latest
    return _coerce_config_version(value), latest


def check_config_version(*, raise_on_parse_error: bool = False) -> tuple[int, int]:
    """Return ``(current_version, latest_version)`` from the raw on-disk config.
    Reads the raw file rather than ``load_config()``: the deep-merge would make a file lacking
    ``_config_version`` inherit the latest version, hiding that the schema was never migrated.
    Invalid YAML gets a parse warning, not an automatic schema rewrite. Tolerant runtime status
    callers keep the historical latest/latest fallback for malformed YAML; mutation and explicit
    validation paths set ``raise_on_parse_error`` so a parse failure or a non-mapping root cannot
    be mistaken for an up-to-date config. A file with no version key reads as 0."""
    stamp, latest = read_config_version_stamp(raise_on_parse_error=raise_on_parse_error)
    return (0 if stamp is None else stamp), latest


def note_first_stamp(home, on_disk: dict, to_write: dict) -> list[str]:
    """``save_config`` is about to give *home*'s config.yaml (*on_disk*: missing or never stamped) its first
    ``_config_version`` outside a migration, so the steps up to that version never see this home. Do into
    *to_write* what they would have done with the home's pre-split files: record the left-core rows in use
    (``left_core_migration.record_core_use``), or the stamp would make a home that ran the core feature look
    newer than the split, and move its AGENT_BROWSER_* settings (``config_migrations_browser``), which
    nothing reads any more. Returns those browser moves."""
    if "_config_version" in on_disk or "_config_version" not in to_write:
        return []
    from agent.safe_worker_policy import worker_config_snapshot
    if worker_config_snapshot() is not None:
        return []  # a frozen worker never reads the profile's files; its owner migrates the home
    from hermes_cli.config_migrations_browser import adopt_agent_browser_env
    from hermes_cli.left_core_migration import LEFT_CORE, record_core_use
    for version in sorted({feature.left_at for feature in LEFT_CORE if feature.left_at}):
        record_core_use(home, to_write, version)
    return adopt_agent_browser_env(home, to_write)


def stamp_missing_config() -> bool:
    """A missing config.yaml reads as current (:func:`read_config_version_stamp`), so no config step ever
    runs on the home. When its .env still carries AGENT_BROWSER_* settings, create config.yaml with its
    first stamp (:func:`note_first_stamp`) now, before callers trust that shortcut, or the browser those
    settings chose is lost. A home without them stays without a config.yaml. Returns whether one was written."""
    from hermes_cli.config import _secure_file, atomic_config_write, get_config_path, is_managed
    from hermes_cli.config_backend import config_exists
    path = get_config_path()
    if config_exists(path) or is_managed():
        return False
    config = {"_config_version": _coerce_config_version(DEFAULT_CONFIG.get("_config_version", 1)) or 1}
    if not note_first_stamp(path.parent, {}, config):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_config_write(path, config)
    _secure_file(path)
    return True
