"""Throughput counters for the Board's machine registry: tokens and sessions summed over every profile on this host.

The Board samples these cumulative totals each minute and stores per-machine deltas (the Desktop throughput panel).
Sources, per ``state.db`` (the installation root's and every ``profiles/*/state.db``):

- ``tokens_io_total`` / ``tokens_cache_total``: ``session_model_usage`` input+output and cache read+write. That
  ledger's main-turn rows (``task = ''``) match ``sessions``' totals; its other rows are auxiliary calls
  (compression, titles, approval, coach, goal judge ...), about 12% of the hub's tokens, which ``sessions`` never
  counts. A database without the table (older schema) falls back to the ``sessions`` columns.
- ``sessions_started_total`` / ``sessions_ended_total``: ``count(*)`` and ``count(ended_at)`` of ``sessions``.
- ``tokens_by_model``: the same token columns grouped by model, merged across profiles,
  normalized to 64-character names and bounded to 32 keys (overflow becomes ``other``).
  Legacy schemas contribute their token totals under ``unknown``.

:func:`provider_counters` (not part of :func:`counters`) groups the same token columns by
``session_model_usage.billing_provider`` for the LLM pacing capacity model; a row with no billing provider (or a
schema without the column) is attributed by :func:`provider_for_model`. It is a full grouped read, so it is cached
for :data:`PROVIDER_TTL_S` and kept off the 10 s metrics path, whose incremental reads are history-bounded. It is not
sent to the Board (a Board that predates a field rejects the whole sample).

Totals only grow while rows are kept; deleting sessions (prune) lowers them, which a consumer treats as a reset.
Read-only URI connections with a 1 s busy timeout. The result (success or failure) is cached for :data:`CACHE_TTL_S`
because ``collect()`` runs every 10 s. Any unreadable database omits all throughput fields: a partial sum would read as
a drop, and 0 would read as a reset.
"""
from __future__ import annotations

import sqlite3
import time
from copy import deepcopy
from pathlib import Path
from threading import RLock
from typing import Callable, Optional

from hermes_cli import machine_throughput_incremental as incremental

_lock = RLock()

FIELDS = ("tokens_io_total", "tokens_cache_total", "sessions_started_total", "sessions_ended_total")
CACHE_TTL_S = 60.0

_USAGE_SQL = ("SELECT coalesce(sum(input_tokens + output_tokens), 0), "
              "coalesce(sum(cache_read_tokens + cache_write_tokens), 0) FROM {table}")
_SESSIONS_SQL = "SELECT count(*), count(ended_at) FROM sessions"

_cache: Optional[tuple[str, float, dict]] = None  # (root key, taken at, value)


_PREFIXES = (("claude", "anthropic"), ("gpt-", "openai-codex"), ("codex", "openai-codex"), ("o1", "openai-codex"),
             ("o3", "openai-codex"), ("o4", "openai-codex"), ("grok", "xai-oauth"))
_PROVIDER_SQL = ("SELECT billing_provider, model, coalesce(sum(input_tokens + output_tokens), 0), "
                 "coalesce(sum(cache_read_tokens + cache_write_tokens), 0) FROM session_model_usage "
                 "GROUP BY billing_provider, model")


def provider_for_model(model: object) -> str:
    """The subscription pool a model name belongs to by prefix (``claude-`` Anthropic, ``gpt-`` Codex, ``grok-``
    xAI), else ``unknown``. Ambiguous for a model served through OpenRouter: the billing provider wins when known."""
    name = str(model or "").strip().lower().rsplit("/", 1)[-1]
    return next((provider for prefix, provider in _PREFIXES if name.startswith(prefix)), "unknown")


def _provider_rows(path: Path, providers: dict[str, list[int]]) -> None:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        columns = {row[1] for row in db.execute("PRAGMA table_info(session_model_usage)")}
        if not columns:
            return
        sql = _PROVIDER_SQL if "billing_provider" in columns else _PROVIDER_SQL.replace(
            "billing_provider, model", "'', model")
        for provider, model, io, cache in db.execute(sql):
            name = str(provider or "").strip().lower()[:64] or provider_for_model(model)
            target = providers.setdefault(name, [0, 0])
            target[0] += max(0, int(io or 0))
            target[1] += max(0, int(cache or 0))
    finally:
        db.close()


def state_db_paths(root: Optional[Path] = None) -> list[Path]:
    if root is None:
        from hermes_constants import get_default_hermes_root
        root = Path(get_default_hermes_root())
    root = Path(root)
    return [p for p in (root / "state.db", *sorted((root / "profiles").glob("*/state.db"))) if p.exists()]


def _bounded_models(models: dict[str, list[int]]) -> dict[str, list[int]]:
    """Normalize before ranking; reserve the last slot for the merged tail.

    Saturate each host-wide token axis at the wire limit, assigning its budget in
    rank order. Deriving the scalars from this same map preserves exact equality
    even for corrupt negative counts or counters beyond the wire limit.
    """
    ranked = sorted(models.items(), key=lambda item: (-sum(item[1]), item[0]))
    folded: dict[str, list[int]] = {}
    for index, (model, values) in enumerate(ranked):
        key = "other" if len(ranked) > 32 and index >= 31 else model
        target = folded.setdefault(key, [0, 0])
        for axis in range(2):
            target[axis] += values[axis]
    remaining = [10**15, 10**15]
    for values in folded.values():
        for axis in range(2):
            values[axis] = min(remaining[axis], max(0, int(values[axis])))
            remaining[axis] -= values[axis]
    return folded


def _read(path: Path, models: Optional[dict[str, list[int]]] = None) -> tuple[int, int, int, int]:
    with _lock:
        return _read_locked(path, models)


def _read_locked(path: Path, models=None):
    path = path.resolve()
    key = str(path)
    before = path.stat()
    identity = (before.st_dev, before.st_ino)
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        db.execute("BEGIN")
        result = incremental.read(db, key, identity)
        if result is not None:
            rows, started, ended = result
        else:
            rows, started, ended = _full_read(db)
        after = path.stat()
        if (after.st_dev, after.st_ino) != identity:
            raise OSError("throughput database replaced during read")
        io = cache = 0
        for model, model_io, model_cache in rows:
            io += int(model_io)
            cache += int(model_cache)
            if models is not None:
                target = models.setdefault(str(model or "unknown")[:64], [0, 0])
                target[0] += int(model_io)
                target[1] += int(model_cache)
        return int(io), int(cache), int(started), int(ended)
    except (OSError, sqlite3.Error, TypeError, ValueError):
        incremental.forget(key)
        raise
    finally:
        db.close()


def _full_read(db):
    has_usage = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_model_usage'"
                           ).fetchone()
    if has_usage:
        rows = db.execute(_USAGE_SQL.replace("SELECT ", "SELECT model, ", 1).format(
            table="session_model_usage") + " GROUP BY model").fetchall()
    else:
        rows = [("unknown", *db.execute(_USAGE_SQL.format(table="sessions")).fetchone())]
    started, ended = db.execute(_SESSIONS_SQL).fetchone()
    return rows, started, ended


def counters(root: Optional[Path] = None) -> dict:
    """Host totals and model breakdown, or ``{}`` if any database is unreadable (or absent)."""
    with _lock:
        return _counters_locked(root)


def _counters_locked(root):
    totals, seen = [0, 0, 0, 0], set()
    models: dict[str, list[int]] = {}
    try:
        paths = state_db_paths(root)
        incremental.prune(paths)
        for path in paths:
            stat = path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if identity in seen:
                continue
            seen.add(identity)
            for i, value in enumerate(_read(path, models)):
                totals[i] += value
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return {}
    if not seen:
        return {}
    bounded = _bounded_models(models)
    totals[:2] = [sum(v[axis] for v in bounded.values()) for axis in range(2)]
    return {**dict(zip(FIELDS, totals)), "tokens_by_model": bounded}


PROVIDER_TTL_S = 240.0
_provider_cache: Optional[tuple[str, float, dict]] = None


def provider_counters(root: Optional[Path] = None, *, clock: Callable[[], float] = time.monotonic) -> dict:
    """``{"tokens_by_provider": {provider: [io, cache]}}`` over every ``state.db`` of this host, cached
    :data:`PROVIDER_TTL_S`; ``{}`` when any database is unreadable (a partial sum would read as a drop)."""
    global _provider_cache
    with _lock:
        now, key = clock(), str(root)
        if _provider_cache is not None and _provider_cache[0] == key and 0 <= now - _provider_cache[1] < PROVIDER_TTL_S:
            return deepcopy(_provider_cache[2])
        providers: dict[str, list[int]] = {}
        seen = set()
        try:
            for path in state_db_paths(root):
                stat = path.stat()
                if (stat.st_dev, stat.st_ino) in seen:
                    continue
                seen.add((stat.st_dev, stat.st_ino))
                _provider_rows(path, providers)
            value = {"tokens_by_provider": providers} if seen else {}
        except (OSError, sqlite3.Error, TypeError, ValueError):
            value = {}
        _provider_cache = (key, now, value)
        return deepcopy(value)


def cached_counters(root: Optional[Path] = None, *, clock: Callable[[], float] = time.monotonic) -> dict:
    with _lock:
        return _cached_locked(root, clock)


def _cached_locked(root, clock):
    global _cache
    now, key = clock(), str(root)
    if _cache is not None and _cache[0] == key and 0 <= now - _cache[1] < CACHE_TTL_S:
        return deepcopy(_cache[2])
    value = counters(root)
    _cache = (key, now, value)
    return deepcopy(value)


def reset_cache() -> None:
    global _cache, _provider_cache
    with _lock:
        _cache = _provider_cache = None
        incremental.reset()
