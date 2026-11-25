"""Classify chat destinations from gateway/profile-scoped creation provenance.

Local transport hashes identify both interactive chats and headless runs. Unknown
hashes remain suppressed; a foreign ref never borrows a local session's proof.
"""
from __future__ import annotations

from contextlib import closing
import json
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


def _read_sources(path: str, todo: list[str]) -> tuple[dict[str, Optional[str]], Optional[bool]]:
    """``(found, readable)`` from one state.db; ``readable`` is None when the file does not exist."""
    found: dict[str, Optional[str]] = {}
    if not Path(path).exists():
        return found, None
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
    except sqlite3.Error:
        return {}, False
    return found, True


def _local_session_sources(sessions: Iterable[str], *, home: Optional[Path] = None) -> dict[str, Optional[str]]:
    """Read effective creation sources, without creating a store or migrating it.

    Inside a links pass (``kanban_links_chat_key``) the 300 s cache is not consulted: each id is read once per pass
    and recorded as that board's key; an unreadable store marks the pass degraded (no key). [daveey]"""
    from hermes_constants import get_hermes_home
    from hermes_cli import kanban_links_chat_key as chat_key
    path = str((home if home is not None else get_hermes_home()) / "state.db")
    now = time.monotonic()
    reads = chat_key.active()
    out: dict[str, Optional[str]] = {}
    todo = []
    for sid in dict.fromkeys(sessions):
        if reads is not None:
            if (value := reads.recall(("source", path, sid))) is not chat_key._MISSING:
                out[sid] = value
                continue
        elif (hit := _source_cache.get((path, sid))) is not None and now - hit[1] < _MISSING_TTL_S:
            out[sid] = hit[0]
            continue
        todo.append(sid)
    if not todo:
        return out
    found, readable = _read_sources(path, todo)
    for sid in todo:
        source = found.get(sid)
        out[sid] = str(source) if source is not None else None
        if readable:
            _source_cache[(path, sid)] = (out[sid], now)
        if reads is not None and readable is not False:
            reads.record(("source", path, sid), out[sid])
    if readable is False:
        chat_key.degrade()
    return out


def _local_homes(profile: Optional[str]) -> list[Path]:
    """Explicit profiles read only that store; legacy bare board IDs search local profiles.

    Boards and legacy tui subscriptions are host-wide and often omit a profile.
    Do not bind those rows to whichever profile happens to run reconciliation.
    Inside a links pass the answer is part of the board's key (``kanban_links_chat_key``).
    """
    from hermes_cli import kanban_links_chat_key as chat_key
    reads = chat_key.active()
    if reads is None:
        return _discover_homes(profile)
    return [Path(h) for h in reads.answer(("homes", profile), lambda: tuple(str(h) for h in _discover_homes(profile)))]


def _discover_homes(profile: Optional[str]) -> list[Path]:
    from hermes_constants import get_hermes_home
    from hermes_cli.profiles import get_active_profile_name, get_profile_dir, list_profile_names
    active = get_active_profile_name()
    if profile is not None:
        try:
            return [get_hermes_home() if profile == active else get_profile_dir(profile)]
        except ValueError:
            return []
    return list(dict.fromkeys([get_hermes_home(), *(get_profile_dir(p) for p in list_profile_names())]))


def _local_scope(gateway: Any, profile: Any, ref: str, me: Any) -> Optional[tuple[Any, str]]:
    """``(profile, sid)`` when :func:`scoped_sources` looks ``ref`` up in this host's state.db, else None (answered
    unknown without a read): a malformed or empty ref, a ``gw/profile/sid`` ref whose outer gateway or profile
    contradicts the scope, or another gateway's ref. [daveey]"""
    parts = ref.split("/")
    gw, prof = gateway, profile
    if len(parts) == 3:
        gw, prof, sid = parts
        if (gateway is not None and gateway != gw) or (profile is not None and profile != prof):
            return None
    elif len(parts) == 1:
        sid = ref
    else:
        return None
    if not sid or (gw is not None and (not me or gw != me)):
        return None
    return prof, sid


def _resolve_local(local_gateway: Any) -> Any:
    """This host's gateway name when ``local_gateway`` is None, else ``local_gateway`` unchanged ("" = unnamed host).
    The one rule every local-scope check here resolves None by, so their answers agree for the same call. [daveey]"""
    if local_gateway is not None:
        return local_gateway
    from hermes_cli.kanban_origin import gateway_names
    return gateway_names()[0]


def scoped_sources(gateway: Any, sessions: Iterable[Any], *, local_gateway=None, profile=None) -> dict[str, Optional[str]]:
    """No network lookup: foreign/ambiguous provenance is unknown, never human proof."""
    me = _resolve_local(local_gateway)
    grouped: dict[Optional[str], dict[str, str]] = {}
    out: dict[str, Optional[str]] = {str(s or ""): None for s in sessions}
    for ref in out:
        if (scope := _local_scope(gateway, profile, ref, me)) is not None:
            grouped.setdefault(scope[0], {})[ref] = scope[1]
    for prof, refs in grouped.items():
        observations = [_local_session_sources(refs.values(), home=home) for home in _local_homes(prof)]
        for ref, sid in refs.items():
            sources = {value for row in observations if (value := row.get(sid)) is not None}
            machine = sources & MACHINE_SESSION_SOURCES
            # Conflicting profile rows are not affirmative proof (legacy IDs can collide).
            out[ref] = sorted(machine)[0] if machine else next(iter(sources)) if len(sources) == 1 else None
    return out


def prefetch_sources(refs: Iterable[Any], *, local_gateway=None) -> None:
    """Read every local home's state.db ONCE for the bare session ids of ``refs`` (``sid`` or ``gw/profile/sid``),
    filling the source cache so the classifications that follow in the same pass are cache hits, whatever profile
    they scope to. Verdicts are unchanged: an entry that expires before use only costs a read. A qualified ref of
    another gateway is skipped: :func:`scoped_sources` never looks it up locally. [daveey]"""
    me = _resolve_local(local_gateway)
    sids = [p[-1] for r in dict.fromkeys(str(r or "") for r in refs)
            if (len(p := r.split("/")) == 1 or (len(p) == 3 and me and p[0] == me)) and p[-1]]
    if sids:
        for home in _local_homes(None):
            _local_session_sources(sids, home=home)


def reads_local_store(gateway: Any, session: Any, *, local_gateway: Any, profile: Any = None) -> bool:
    """Whether :func:`scoped_sources` (``local_gateway=local_gateway, profile=profile``) may open a state.db for
    ``session`` scoped to ``gateway``. It shares that function's routing (:func:`_local_scope`) and None
    resolution (:func:`_resolve_local`), so a ref answered unknown without a read (another gateway's, or a
    qualified ref whose outer gateway/profile contradicts the scope) is never reported as a read. [daveey]"""
    return _local_scope(gateway, profile, str(session or ""), _resolve_local(local_gateway)) is not None


def machine_verdicts(keys: Iterable[tuple[Any, Any, Any]], *, local_gateway) -> dict[tuple, bool]:
    """``is_machine_session(gateway, session, profile=profile)`` for every ``(gateway, profile, session)`` key, with
    each home read once (one :func:`prefetch_sources`) and one :func:`machine_sessions` call per (gateway, profile).
    Keys are ``(gateway, profile, str(session or ""))``. A caller computes this BEFORE taking a lock, so the
    per-row path under the lock looks verdicts up instead of reading state.db (t_01a1d2f6289c3fe7). [daveey]"""
    groups: dict[tuple, list[str]] = {}
    for gw, prof, session in keys:
        groups.setdefault((gw, prof), []).append(str(session or ""))
    prefetch_sources([s for (gw, prof), refs in groups.items() for s in refs
                      if reads_local_store(gw, s, local_gateway=local_gateway, profile=prof)],
                     local_gateway=local_gateway)
    out: dict[tuple, bool] = {}
    for (gw, prof), refs in groups.items():
        machine = machine_sessions(gw, refs, local_gateway=local_gateway, profile=prof)
        out.update({(gw, prof, s): s in machine for s in refs})
    return out


def headless_sessions(chat_ids: Iterable[Any], *, gateway=None, profile=None, local_gateway=None) -> set[str]:
    """The ``str(chat_id or "")`` refs :func:`is_headless_session` calls headless, from ONE ``scoped_sources`` read.

    A reconcile pass classifies thousands of rows; a per-row call reopened every home's state.db for each one
    once the 300 s source cache expired (hub sync rounds of 15-28 min, t_01a1d2cc49634a78). [daveey]"""
    refs = [r for r in dict.fromkeys(str(c or "") for c in chat_ids) if _HEADLESS_SESSION.fullmatch(r.rsplit("/", 1)[-1])]
    if not refs:
        return set()
    sources = scoped_sources(gateway, refs, local_gateway=local_gateway, profile=profile)
    return {r for r in refs if sources[r] not in _INTERACTIVE_SOURCES}


def is_headless_session(chat_id: Any, *, gateway=None, profile=None, local_gateway=None) -> bool:
    """A receipt-shaped destination lacking authoritative interactive provenance."""
    ref = str(chat_id or "")
    return ref in headless_sessions((ref,), gateway=gateway, profile=profile, local_gateway=local_gateway)


def machine_sessions(gateway: Any, sessions: Iterable[Any], *, local_gateway=None, profile=None) -> set[str]:
    """Known machines plus unknown receipt hashes; preserve legacy ordinary-ID behavior."""
    sources = scoped_sources(gateway, sessions, local_gateway=local_gateway, profile=profile)
    return {ref for ref, source in sources.items() if ref.rsplit("/", 1)[-1].startswith("cron_")
            or source in MACHINE_SESSION_SOURCES
            or (_HEADLESS_SESSION.fullmatch(ref.rsplit("/", 1)[-1]) and source not in _INTERACTIVE_SOURCES)}


def is_machine_session(gateway: Any, session: Any, *, local_gateway=None, profile=None) -> bool:
    return bool(machine_sessions(gateway, (session,), local_gateway=local_gateway, profile=profile))


def _ancestors(conn, task_ids: Iterable[str], max_depth: int) -> list[str]:
    """The cards and their creator/dependency ancestors (bounded, cycle-safe)."""
    seen: dict[str, None] = {}
    frontier = [t for t in dict.fromkeys(task_ids) if t]
    for _depth in range(max_depth):
        frontier = [t for t in frontier if t not in seen]
        if not frontier:
            break
        seen.update(dict.fromkeys(frontier))
        q = ",".join("?" * len(frontier))
        following = [r[0] for r in conn.execute(f"SELECT parent_id FROM task_links WHERE child_id IN ({q})", frontier)]
        for (payload,) in conn.execute(
                f"SELECT payload FROM task_events WHERE task_id IN ({q}) AND kind='created'", frontier):
            try:
                creator = (json.loads(payload or "{}") or {}).get("creator_task_id")
            except (TypeError, ValueError, AttributeError):
                creator = None
            if creator:
                following.append(str(creator))
        frontier = following
    return list(seen)


def proven_foreign_chats(conn, task_ids: Iterable[str], *, local_gateway=None, max_depth: int = 16) -> frozenset[str]:
    """``gateway/profile/session`` refs of other gateways' ``local-<sha256>`` chats this board proves own a card.

    ``machine_sessions`` cannot read a foreign gateway's state.db, so it suppresses every foreign hash (a
    Desktop chat and a worker receipt share the shape). This lifts that only for an exact qualified ref with an
    unreleased ``created`` chat link on one of ``task_ids`` or an ancestor (creator edge or dependency parent).
    Only the chat's own gateway can tell a Desktop chat from a worker receipt, and it says so in that row: its
    reconcile writes an origin card's ``created`` link only for an interactive chat (``_ledger_links``) and
    releases every link to its own machine sessions, owner-written ones included (``_owner_written_releases``).
    The row is synced, so worker replicas see it. A courier binding (``origin_creation_intents``) or a
    ``reason: origin`` hub sub proves nothing alone: ``hermes-hub create-task``/``adopt-task`` default the origin
    to the caller's ``$HERMES_SESSION_ID`` (a worker's own hash inside a Kanban worker), and neither row is ever
    released. Legacy NULL profiles mean default, as in the renewal reader; an empty profile remains invalid.
    Observer links, released links and an unqualified id prove nothing; this
    gateway's own sessions keep their state.db verdict; no network read. [daveey]"""
    if local_gateway is None:
        from hermes_cli.kanban_origin import gateway_names
        local_gateway = gateway_names()[0]
    if not local_gateway or not conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone():
        return frozenset()
    ids = _ancestors(conn, task_ids, max_depth)
    q = ",".join("?" * len(ids))
    refs = conn.execute(
        f"SELECT chat_gateway, chat_profile, chat_session FROM task_chat_links WHERE task_id IN ({q}) "
        "AND kind='created' AND released=0", ids).fetchall() if ids else []
    return frozenset(f"{gw}/{prof if prof is not None else 'default'}/{sid}" for gw, prof, sid in refs
                     if gw and (prof is None or prof) and gw != local_gateway
                     and _HEADLESS_SESSION.fullmatch(str(sid or "")))
