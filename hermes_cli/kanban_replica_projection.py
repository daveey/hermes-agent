"""Incremental projection of a replica's task rows into this origin's remote cards. [daveey]

``_project_replica`` (``kanban_session_activity``) projects every replica row on every call. On hermes-hub that is
~13k rows per remote-links memo miss, and the replica changes every ~30 s or faster, so the projection ran under
``_links_lock`` most of the time chats were waiting on it. This module keeps each row's clock-free projection
(decode, key parse, record-level release evidence, links to this origin) keyed by ``(gw, key)`` and the
aggregates built from them, and on a miss re-projects only the rows ``kanban_replica_rows`` reports changed. The
clock-dependent inputs (hub staleness, each owner scope's freshness verdict) are the state's key: when one of them
changes, the aggregates are rebuilt from the kept rows without decoding any. ``project`` answers exactly what
``_project_replica`` does for the same rows; ``tests/hermes_cli/test_kanban_replica_incremental.py`` pins that.

A kept row holds its whole decoded record only when it links a session of this origin (``sids``): only such a record
can become a card, and a card reads the whole record (hash-bound owner evidence, receipts, run, wait fields). Every
other row keeps ``SLIM_FIELDS``, all the release, fold and moved logic reads, and is decoded again from its packed
text in the rare case a legacy alias folds a link of this origin into it. Keeping every record whole was ~150 MB of
each gateway/dashboard/serve heap for ~13.5k replica cards of which 54 linked the mini (t_01a1cf67a93c05b6).

One state is kept per ``(replica, gateway, origin profile)``. A Desktop ``serve`` answers every profile of its home
(``profile=all`` polls, the annotation holder's scopes), and with one state per replica each profile's read threw
away the previous profile's state and decoded every row again: 18,264 rows and 976 ms of CPU per read on the mini's
replica, where a single profile's read costs 31 ms, and the holder ran five such reads per 2 s pass
(t_01a1d6d42877f719). A row that does not link the reading origin projects the same for every profile, so those
stages are decoded once per replica and shared by its states (``_shared``); only a row linking the reading origin
is decoded for that state, which keeps its whole record.
"""
from __future__ import annotations

import threading
from collections import ChainMap
from dataclasses import dataclass, field
from typing import Any

from hermes_cli import kanban_scope_heartbeat as _scope_hb
from hermes_cli.kanban_session_activity_records import _fold_legacy_aliases
from hermes_cli.kanban_session_activity_release import record_attests_terminal

LINK_RELS = ('shepherd', 'cross_task', 'create-task', 'adopt-task')
# Every field a kept record without links to this origin is read for after ``_stage``: the unknown-status copy and
# ``_fold_legacy_aliases`` (status, sessions, created_at, created_by), and the record's own identity. Release
# evidence (``record_attests_terminal``) is computed in ``_stage`` from the whole record before it is slimmed.
SLIM_FIELDS = ('id', 'status', 'profile', 'board', 'gateway', 'created_at', 'created_by', 'sessions')
_REMOTE_FIELDS = ("title", "status", "block_kind", "human_request", "assignee", "created_at", "started_at", "priority", "triage_entered_at")


def linked_sessions(task: dict[str, Any], me: str, origin_profile: str) -> tuple[str, ...]:
    """The sessions of this origin (gateway ``me``, profile ``origin_profile``) the record links, sorted."""
    links = task.get("sessions")
    if not isinstance(links, list):
        links = []
    return tuple(sorted({str(link['session']) for link in links
                         if isinstance(link, dict) and link.get('session') and link.get('gw') == me
                         and link.get('profile') == origin_profile and link.get('rel') in LINK_RELS}))


def historical_fields(raw: dict[str, Any], tkey: tuple, evidence: dict) -> dict[str, Any]:
    from hermes_cli.kanban_completion_receipt_projection import remote_fields
    from hermes_cli.kanban_terminal_release_snapshot import observation
    gw, owner_profile, board, tid = tkey
    return {**remote_fields(raw, gateway=gw, profile=owner_profile, board=board, task_id=tid),
            **observation(raw, tkey, evidence)}


def card_rows(tkey: tuple, t: dict[str, Any], *, sids, closed: bool, synced, stale: bool, me: str,
              origin_profile: str, legacy_record, historical: dict[str, Any]) -> list[tuple[tuple, dict]]:
    """``((session, ref), card)`` for each linked session of one projected record (``_project_replica``)."""
    from hermes_cli.kanban_session_activity import LISTED_STATUSES, _owner_evidence_of, _park_of

    gw, owner_profile, board, tid = tkey
    prefix = f'{gw}/{owner_profile}/{board}' if owner_profile is not None else f'{gw}/{board}'
    ref = f'{prefix}/{tid}'
    out = []
    for sid in sids:
        out.append(((sid, ref), {
            **historical,
            **({"blocked_upstream": t["blocked_upstream"]} if t.get("blocked_upstream") else {}),
            **{f: t.get(f) for f in _REMOTE_FIELDS},
            "id": tid, "board": board, "gateway": gw, "key": ref, "session": sid,
            "via": "shepherd", "root": ref, "remote": True,
            "profile": owner_profile, "owner_profile": owner_profile,
            "origin_gateway": me, "origin_profile": origin_profile,
            "ownership_state": 'unknown' if owner_profile is None else 'qualified',
            "unfinished": not closed and (t.get("status") in LISTED_STATUSES or bool(t.get("unfinished"))),
            "released": closed,
            **({"closed_uncompleted": True} if closed and t.get("status") == "archived"
               and _owner_evidence_of(t).get("verified") is not True else {}),
            "reported_status": t.get('reported_status', t.get('status')),
            "synced_at": synced, "stale": stale,
            "legacy_record": legacy_record,
            "legacy_key": t.get('_legacy_key'),
            # [daveey] What the card waits on is recomputed at read time from these (``_remote_wait``).
            "park": _park_of(t),
            **{k: t[k] for k in ("awaiting", "wake_check") if t.get(k)},
        }))
    return out


class _Stage:
    """One row's clock-free projection; ``error`` set instead when the row is malformed."""
    __slots__ = (
        'attest',
        'cards',
        'error',
        'evidence',
        'historical',
        'legacy',
        'links',
        'order',
        'owner3',
        'published',
        'row',
        'seen',
        'sids',
        'slim',
        't',
        'tkey',
        'unknown',
        'verified',
    )


def link_targets(task: dict[str, Any]) -> tuple:
    """Every ``(gw, profile)`` whose sessions the record links: ``linked_sessions(task, gw, profile)`` is empty
    for exactly the pairs not listed here."""
    links = task.get("sessions")
    out: list = []
    for link in links if isinstance(links, list) else ():
        if isinstance(link, dict) and link.get('session') and link.get('rel') in LINK_RELS:
            pair = (link.get('gw'), link.get('profile'))
            if pair not in out:
                out.append(pair)
    return tuple(out)


def _shareable(st: _Stage, row: tuple, me: str, origin_profile: str) -> bool:
    """Whether ``st``, staged for some origin, is this origin's stage of ``row`` too: the same row text, and a
    record that links no session of this origin (or a malformed row, which no origin projects)."""
    if not (st.row is row or st.row == row):
        return False
    return st.error is not None or (me, origin_profile) not in st.links


def _stage(row: tuple, me: str, origin_profile: str, shared: dict | None = None) -> _Stage:
    from hermes_cli.kanban_replica_rows import text
    from hermes_cli.kanban_session_activity import LISTED_STATUSES, _decode_replica_task

    gw, record_key, data, updated, version = row
    rk = (gw, record_key)
    if shared is not None:
        kept = shared.get(rk)
        if kept is not None and _shareable(kept, row, me, origin_profile):
            stats["shared"] += 1
            return kept
    stats["projected"] += 1
    st = _Stage()
    st.row, st.error, st.order, st.slim, st.links = row, None, (str(gw), str(record_key)), False, ()
    try:
        t, digest, evidence = _decode_replica_task(record_key, text(data))
        parts = str(record_key).split('/')
        if len(parts) == 3:
            owner_profile, board, tid = parts
        elif len(parts) == 2:
            owner_profile = None
            board, tid = parts
        else:
            raise ValueError('invalid replica task key')
        if not all(parts) or tid != str(t['id']):
            raise ValueError('replica task identity mismatch')
    except ValueError as exc:
        st.error = exc
        if shared is not None:
            shared[rk] = st
        return st
    gw = str(gw)
    st.tkey = (gw, owner_profile, board, tid)
    st.evidence = evidence
    st.sids = linked_sessions(t, me, origin_profile)
    st.links = link_targets(t)
    # [daveey] Release evidence reads the whole record (``record_attests_terminal`` reads origin_bindings and
    # more), so it is computed before the record is slimmed.
    st.attest = ((gw, owner_profile, board) if record_attests_terminal(t, gw, owner_profile, board, evidence)
                 else None)
    # [daveey] Verified completion (``kanban_origin_ledger._verified_terminal``): only such a record expires a
    # retained link it no longer carries (``_legacy_remote_links``).
    st.verified = st.attest is not None and evidence.get('verified') is True
    if not st.sids:
        st.slim = True
        t = {f: t[f] for f in SLIM_FIELDS if f in t}
    st.t = t
    st.legacy = {'key': record_key, 'version': version, 'digest': digest} if owner_profile is None else None
    st.seen = (updated or 0) / 1000.0
    st.unknown = ({**t, 'reported_status': t.get('status'), 'status': 'unknown', 'unfinished': True}
                  if owner_profile is None or t.get('status') not in LISTED_STATUSES else None)
    created = t.get('created_at')
    st.published = (board, tid, created) if owner_profile is not None and created is not None else None
    st.owner3 = (gw, board, tid)
    st.historical = st.cards = None
    if shared is not None:
        if st.slim:  # links nothing of this origin: every other such origin's stage as well
            shared[rk] = st
        else:  # this origin keeps its own whole record; an earlier text's stage is nobody's any more
            shared.pop(rk, None)
    return st


def _record(st: _Stage) -> dict[str, Any]:
    """The stage's whole decoded record: kept for a record linking this origin, else decoded again from its text."""
    if not st.slim:
        return st.t
    from hermes_cli.kanban_replica_rows import text
    from hermes_cli.kanban_session_activity import _decode_replica_task

    stats["rehydrated"] += 1
    return _decode_replica_task(st.row[1], text(st.row[2]))[0]


def _whole(st: _Stage, base: dict[str, Any], t: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(record, t)`` with the whole record: ``base`` is what the state holds for the stage (its record or its
    unknown-status copy) and ``t`` is ``base`` or a fold of it, which reads ``base`` with links added."""
    record = _record(st)
    if record is st.t:
        return record, t
    whole = record
    if base is st.unknown:
        whole = {**record, 'reported_status': record.get('status'), 'status': 'unknown', 'unfinished': True}
    return record, {**whole, **{k: v for k, v in t.items() if k not in base or base[k] is not v}}


@dataclass
class _State:
    """Aggregates of every kept row under one clock key (``stale``, fresh scopes)."""
    tag: str
    gen: int | None
    key: tuple
    stages: dict
    errors: dict
    tasks: dict
    raw: dict
    released: set
    legacy: dict
    seen: dict
    seen_dirty: set
    seen_rows: dict  # gw -> {(gw, key): seen}
    owners: dict
    published: dict  # (board, id, created_at) -> {gw: count}
    published_out: dict  # same keys -> frozenset(gw)
    qualified: dict  # (gw, board, id) -> {qualified tkey}
    legacy_keys: set
    linked: set
    order: dict  # tkey -> its (gw, key) in ``stages``
    closed: set  # released tkeys whose owner verified the completion
    bystanders: dict = field(default_factory=dict)


_MAX_STATES = 16
_lock = threading.Lock()
# (replica path, this gateway, origin profile) -> the state of that origin's reads, most recently read last.
_states: dict[tuple, _State] = {}
# replica path -> (gw, key) -> the stage of a row that links no session of the origin it was staged for, shared by
# every state of that replica whose origin it does not link either (``_shareable``). Dropped with the path's last state.
_shared: dict[str, ChainMap] = {}
stats = {"projected": 0, "rebuilt": 0, "historical": 0, "considered": 0, "rehydrated": 0, "shared": 0}


def _new_state(tag: str, key: tuple) -> _State:
    return _State(tag, None, key, {}, {}, {}, {}, set(), {}, {}, set(), {}, {}, {}, {}, {}, set(), set(), {}, set())


def _apply(s: _State, rk: tuple, st: _Stage) -> None:
    s.stages[rk] = st
    if st.error is not None:
        s.errors[rk] = st
        return
    stale, fresh = s.key[2], s.key[3]
    tk = st.tkey
    s.raw[tk] = st.t
    s.order[tk] = rk
    if st.legacy is not None:
        s.legacy[tk] = st.legacy
        s.legacy_keys.add(tk)
    else:
        s.qualified.setdefault(st.owner3, set()).add(tk)
    gw = tk[0]
    s.seen_rows.setdefault(gw, {})[rk] = st.seen
    if gw not in s.seen_dirty:
        s.seen[gw] = max(s.seen.get(gw, 0.0), st.seen)
    if st.attest is not None and not stale and _scope_hb.attests(fresh, st.attest, st.seen):
        s.released.add(tk)
        if st.verified:
            s.closed.add(tk)
        s.tasks[tk] = st.t
    else:
        s.tasks[tk] = st.unknown if st.unknown is not None else st.t
    s.owners[st.owner3] = s.owners.get(st.owner3, 0) + 1
    if st.published is not None:
        gws = s.published.setdefault(st.published, {})
        gws[gw] = gws.get(gw, 0) + 1
        s.published_out[st.published] = frozenset(gws)
    if st.sids:
        s.linked.add(tk)


def _unapply(s: _State, rk: tuple) -> None:
    st = s.stages.pop(rk)
    if st.error is not None:
        del s.errors[rk]
        return
    tk = st.tkey
    for d in (s.raw, s.order, s.tasks, s.legacy):
        d.pop(tk, None)
    s.released.discard(tk)
    s.closed.discard(tk)
    s.legacy_keys.discard(tk)
    s.linked.discard(tk)
    if st.legacy is None:
        twins = s.qualified[st.owner3]
        twins.discard(tk)
        if not twins:
            del s.qualified[st.owner3]
    gw_rows = s.seen_rows[tk[0]]
    del gw_rows[rk]
    if not gw_rows:
        del s.seen_rows[tk[0]]
    if s.seen.get(tk[0]) == st.seen:
        s.seen_dirty.add(tk[0])  # it may have been this gateway's newest row
    n = s.owners[st.owner3] - 1
    if n:
        s.owners[st.owner3] = n
    else:
        del s.owners[st.owner3]
    if st.published is not None:
        gws = s.published[st.published]
        gws[tk[0]] -= 1
        if not gws[tk[0]]:
            del gws[tk[0]]
        if gws:
            s.published_out[st.published] = frozenset(gws)
        else:
            del s.published[st.published], s.published_out[st.published]


def _settle_seen(s: _State) -> None:
    for gw in s.seen_dirty:
        rows = s.seen_rows.get(gw)
        if rows:
            s.seen[gw] = max(0.0, *rows.values())
        else:
            s.seen.pop(gw, None)
    s.seen_dirty.clear()


def _advance(s: _State, view, me: str, origin_profile: str) -> None:
    """Bring ``s`` to ``view``'s rows, re-projecting only rows whose stored text changed."""
    # Unfiltered structural views predate the optional scalar projection fields.
    rows = view.relevant if getattr(view, 'me', None) is not None else view.by_key
    bystanders = getattr(view, 'bystanders', {})
    shared_view = _shared.setdefault(s.tag, ChainMap({}, {}))
    # Count actual reuse of scalar projections, just as _stage counts reused stages.
    prior = shared_view.maps[1]
    stats['shared'] += sum(1 for rk, value in bystanders.items()
                           if s.bystanders.get(rk) != value and prior.get(rk) == value)
    shared_view.maps[1] = bystanders
    s.bystanders = bystanders
    if s.gen is not None and view.base_gen == s.gen:
        removed, candidates = view.removed, view.changed
    else:  # not the view ``s`` was advanced from: compare every row (no decode for unchanged ones)
        removed = s.stages.keys() - rows.keys()
        candidates = rows.keys()
    shared = shared_view.maps[0]
    # A new scope has no removed rows of its own. Reconcile the shared cache against the replica itself,
    # or deleted rows survive indefinitely when their older scopes are evicted before reading again.
    for rk in shared.keys() - rows.keys():
        del shared[rk]
    for rk in removed:
        if rk in s.stages:
            _unapply(s, rk)
    for rk in candidates:
        if rk not in rows:
            if rk in s.stages:
                _unapply(s, rk)
            continue
        row = rows[rk]
        old = s.stages.get(rk)
        if old is not None and (old.row is row or old.row == row):  # (gw, profile) is in the state's key
            continue
        if old is not None:
            _unapply(s, rk)
        _apply(s, rk, _stage(row, me, origin_profile, shared))
    s.gen = view.gen


def _rekeyed(old: _State, key: tuple) -> _State:
    """``old``'s rows under a new clock key: no row is decoded again."""
    stats["rebuilt"] += 1
    s = _new_state(old.tag, key)
    for rk, st in old.stages.items():
        _apply(s, rk, st)
    s.gen = old.gen
    s.bystanders = old.bystanders
    return s


def project(view, now: float, me: str, info_rows: list, last_ok: float | None, offline: float | None, *,
            fresh: frozenset, qualified_out: set | None = None, owners_out: set | None = None,
            seen_out: dict | None = None, moved_out: dict | None = None,
            closed_out: set | None = None, aggregates_out: dict | None = None) -> list[dict[str, Any]]:
    """``_project_replica(path, now, me, view.rows(), info_rows, last_ok, offline, ...)``, incrementally.
    ``fresh`` is ``fresh_scopes`` of ``info_rows``' heartbeats at ``now``: all the projection reads of them."""
    from hermes_cli.profiles import get_active_profile_name

    from hermes_cli.kanban_replica_scalar_rows import SnapshotRows
    if isinstance(view, SnapshotRows):
        view = view.consume()
    origin_profile = get_active_profile_name()
    stale = offline is not None or last_ok is None or now - last_ok > _hub_stale_after()
    key = (me, origin_profile, stale, fresh)
    slot = (view.path, me, origin_profile)
    with _lock:
        s = _states.pop(slot, None)
        # A profile that is not polled again must not pin a now-bystander payload.
        # Keep its generation unchanged so its next read still reconciles normally.
        for other in _states.values():
            if other.tag == view.path and other.key[0] == me:
                for rk in other.stages.keys() & getattr(view, 'bystanders', {}).keys():
                    _unapply(other, rk)
        try:
            if s is None:
                s = _new_state(view.path, key)
            elif s.key != key:
                s = _rekeyed(s, key)
            _advance(s, view, me, origin_profile)
        except BaseException:
            _forget_unused_shared()
            raise
        _states[slot] = s
        while len(_states) > _MAX_STATES:
            _states.pop(next(iter(_states)))
        _forget_unused_shared()
        return _emit(s, me, origin_profile, last_ok, stale, fresh, qualified_out=qualified_out,
                     owners_out=owners_out, seen_out=seen_out, moved_out=moved_out, closed_out=closed_out,
                     aggregates_out=aggregates_out)


def _forget_unused_shared() -> None:
    """Caller holds ``_lock``: shared stages live only as long as a state of their replica does."""
    live = {tag for tag, _me, _profile in _states}
    for tag in [tag for tag in _shared if tag not in live]:
        del _shared[tag]


def _hub_stale_after() -> float:
    from hermes_cli.kanban_session_activity import HUB_STALE_AFTER_S
    return HUB_STALE_AFTER_S


def _emit(s: _State, me: str, origin_profile: str, last_ok, stale: bool, fresh: frozenset, *, qualified_out,
          owners_out, seen_out, moved_out, closed_out=None, aggregates_out=None) -> list[dict[str, Any]]:
    if s.errors:
        raise s.errors[min(s.errors, key=lambda rk: s.errors[rk].order)].error
    _settle_seen(s)
    from hermes_cli.kanban_replica_aggregates import snapshot
    aggregates = snapshot(s)
    if seen_out is not None:
        seen_out.update(aggregates['seen'])
    if owners_out is not None:
        owners_out.update(aggregates['owners'])
    if closed_out is not None:  # ``s.closed`` is empty on a stale replica (``_apply``)
        closed_out.update(aggregates['closed'])
    if moved_out is not None:
        moved_out.update(aggregates['moved'])
    if aggregates_out is not None:
        aggregates_out.update(aggregates)
    # Fold only the legacy records and every qualified record sharing their (gw, board, id): exactly the inputs
    # ``_fold_legacy_aliases`` reads for them when given every record.
    sub = {tk: s.tasks[tk] for tk in s.legacy_keys}
    for gw, _, board, tid in list(sub):
        for twin in s.qualified.get((gw, board, tid), ()):
            sub[twin] = s.tasks[twin]
    before = dict(sub)
    _fold_legacy_aliases(sub, s.raw)
    folded = {tk for tk in before if tk not in sub}
    if qualified_out is not None:
        qualified_out.update((gw, board, tid) for gw, _, board, tid in folded)
    if aggregates_out is not None:
        aggregates_out['qualified'] = frozenset((gw, board, tid) for gw, _, board, tid in folded)
    changed = {tk for tk, t in sub.items() if t is not before[tk]}
    found: dict[tuple, dict] = {}
    for tk in sorted((s.linked | changed) - folded, key=lambda tk: s.stages[s.order[tk]].order):
        stats["considered"] += 1
        gw, owner_profile, board, tid = tk
        if gw == me and owner_profile == origin_profile:
            continue
        t = sub.get(tk, s.tasks[tk])
        st = s.stages[s.order[tk]]
        sids = st.sids if tk not in changed else linked_sessions(t, me, origin_profile)
        if not sids:
            continue
        record, t = _whole(st, s.tasks[tk], t)  # only a slim twin a legacy alias folded links into decodes here
        if st.historical is None:
            stats["historical"] += 1
            st.historical = historical_fields(record, tk, st.evidence)
        owner_seen = aggregates['seen'].get(gw) or None
        synced = min(owner_seen, last_ok) if (owner_seen and last_ok) else (owner_seen or last_ok)
        inputs = (t, sids, tk in s.released, synced, stale, me, origin_profile)
        cached = st.cards
        if cached is None or cached[0] is not t or cached[1:-1] != inputs[1:]:
            # Cards are read-only (as in ``_links_cache``), so an unchanged record's cards are shared.
            cached = st.cards = (*inputs, card_rows(
                tk, t, sids=sids, closed=inputs[2], synced=synced, stale=stale, me=me,
                origin_profile=origin_profile, legacy_record=s.legacy.get(tk), historical=st.historical))
        found.update(cached[-1])
    return list(found.values())


def reset() -> None:
    with _lock:
        _states.clear()
        _shared.clear()
    for k in stats:
        stats[k] = 0
