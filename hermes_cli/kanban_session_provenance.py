"""Classify chat destinations from gateway/profile-scoped creation provenance.

Local transport hashes identify both interactive chats and headless runs. Unknown
hashes remain suppressed; a foreign ref never borrows a local session's proof.
"""
from __future__ import annotations

from contextlib import closing
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Optional

_HEADLESS_SESSION = re.compile(r"local-[0-9a-f]{64}")
_INTERACTIVE_SOURCES = frozenset({"desktop", "cli", "tui", "acp", "gui"})
MACHINE_SESSION_SOURCES = frozenset({"kanban", "cron", "oneshot", "subagent"})
_MISSING_TTL_S = 300.0
_source_cache: dict[tuple[str, str], tuple[Optional[str], float]] = {}


def session_creation_source(row) -> Optional[str]:
    """Machine provenance wins; local transport alone is not interactive proof."""
    source, created = row.get("source"), row.get("created_source")
    if source in MACHINE_SESSION_SOURCES or created in MACHINE_SESSION_SOURCES:
        return source if source in MACHINE_SESSION_SOURCES else created
    return created if source == "local" else source


def _local_session_sources(sessions: Iterable[str], *, home: Optional[Path] = None) -> dict[str, Optional[str]]:
    """Read effective creation sources, without creating a store or migrating it."""
    from hermes_constants import get_hermes_home
    path = str((home if home is not None else get_hermes_home()) / "state.db")
    now = time.monotonic()
    out: dict[str, Optional[str]] = {}
    todo = []
    for sid in dict.fromkeys(sessions):
        hit = _source_cache.get((path, sid))
        if hit is not None and now - hit[1] < _MISSING_TTL_S:
            out[sid] = hit[0]
        else:
            todo.append(sid)
    if not todo:
        return out
    found: dict[str, Optional[str]] = {}
    readable = False
    if Path(path).exists():
        try:
            uri = Path(path).as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:  # noqa: kanban-lockguard (state.db)
                columns = {r[1] for r in db.execute("PRAGMA table_info(sessions)")}
                created = "created_source" if "created_source" in columns else "NULL"
                for i in range(0, len(todo), 500):
                    chunk = todo[i:i + 500]
                    for sid, source, provenance in db.execute(
                            f"SELECT id, source, {created} FROM sessions WHERE id IN ({','.join('?' * len(chunk))})", chunk):
                        found[sid] = session_creation_source({"source": source, "created_source": provenance})
            readable = True
        except sqlite3.Error:
            pass
    for sid in todo:
        source = found.get(sid)
        out[sid] = str(source) if source is not None else None
        if readable:
            _source_cache[(path, sid)] = (out[sid], now)
    return out


def _local_homes(profile: Optional[str]) -> list[Path]:
    """Explicit profiles read only that store; legacy bare board IDs search local profiles.

    Boards and legacy tui subscriptions are host-wide and often omit a profile.
    Do not bind those rows to whichever profile happens to run reconciliation.
    """
    from hermes_constants import get_hermes_home
    from hermes_cli.profiles import get_active_profile_name, get_profile_dir, list_profile_names
    active = get_active_profile_name()
    if profile is not None:
        try:
            return [get_hermes_home() if profile == active else get_profile_dir(profile)]
        except ValueError:
            return []
    return list(dict.fromkeys([get_hermes_home(), *(get_profile_dir(p) for p in list_profile_names())]))


def scoped_sources(gateway: Any, sessions: Iterable[Any], *, local_gateway=None, profile=None) -> dict[str, Optional[str]]:
    """No network lookup: foreign/ambiguous provenance is unknown, never human proof."""
    from hermes_cli.kanban_origin import gateway_names
    me = local_gateway if local_gateway is not None else gateway_names()[0]
    grouped: dict[Optional[str], dict[str, str]] = {}
    out: dict[str, Optional[str]] = {str(s or ""): None for s in sessions}
    for ref in out:
        parts = ref.split("/")
        gw, prof = gateway, profile
        if len(parts) == 3:
            gw, prof, sid = parts
            if (gateway is not None and gateway != gw) or (profile is not None and profile != prof):
                continue
        elif len(parts) == 1:
            sid = ref
        else:
            continue
        if not sid or (gw is not None and (not me or gw != me)):
            continue
        grouped.setdefault(prof, {})[ref] = sid
    for prof, refs in grouped.items():
        observations = [_local_session_sources(refs.values(), home=home) for home in _local_homes(prof)]
        for ref, sid in refs.items():
            sources = {value for row in observations if (value := row.get(sid)) is not None}
            machine = sources & MACHINE_SESSION_SOURCES
            # Conflicting profile rows are not affirmative proof (legacy IDs can collide).
            out[ref] = sorted(machine)[0] if machine else next(iter(sources)) if len(sources) == 1 else None
    return out


def is_headless_session(chat_id: Any, *, gateway=None, profile=None, local_gateway=None) -> bool:
    """A receipt-shaped destination lacking authoritative interactive provenance."""
    ref = str(chat_id or "")
    if not _HEADLESS_SESSION.fullmatch(ref.rsplit("/", 1)[-1]):
        return False
    return scoped_sources(gateway, (ref,), local_gateway=local_gateway, profile=profile)[ref] not in _INTERACTIVE_SOURCES


def machine_sessions(gateway: Any, sessions: Iterable[Any], *, local_gateway=None, profile=None) -> set[str]:
    """Known machines plus unknown receipt hashes; preserve legacy ordinary-ID behavior."""
    sources = scoped_sources(gateway, sessions, local_gateway=local_gateway, profile=profile)
    return {ref for ref, source in sources.items() if ref.rsplit("/", 1)[-1].startswith("cron_")
            or source in MACHINE_SESSION_SOURCES
            or (_HEADLESS_SESSION.fullmatch(ref.rsplit("/", 1)[-1]) and source not in _INTERACTIVE_SOURCES)}


def is_machine_session(gateway: Any, session: Any, *, local_gateway=None, profile=None) -> bool:
    return bool(machine_sessions(gateway, (session,), local_gateway=local_gateway, profile=profile))
