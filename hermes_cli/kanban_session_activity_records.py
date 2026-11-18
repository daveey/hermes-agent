"""Which replica record speaks for a card: legacy-alias folding and retiring rows a fresh replica proves moved.

Split from ``kanban_session_activity`` (its file cap); ``_project_replica`` and ``_legacy_remote_links`` call these.
[daveey]
"""
from __future__ import annotations

from typing import Any


def _moved_evidence(tasks: dict[tuple, dict[str, Any]], heartbeats: dict[tuple[str, str, str], tuple[float, float]],
                    now: float) -> dict[str, Any]:
    """What a fresh replica proves about where each card lives (``_retire_moved``): the gateways publishing a
    profile-qualified record per ``(board, id, created_at)``, and the owner scopes whose collector read cleanly within
    that owner's window (``heartbeats`` values are ``(observed_at, window)``, ``kanban_scope_heartbeat.scope_heartbeats``). [daveey]"""
    published: dict[tuple[str, str, Any], set[str]] = {}
    for (gw, owner_profile, board, tid), t in tasks.items():
        if owner_profile is not None and t.get('created_at') is not None:
            published.setdefault((board, tid, t.get('created_at')), set()).add(gw)
    live = {scope for scope, (beat, window) in heartbeats.items() if 0 <= now - beat <= window}
    return {'published': {k: frozenset(v) for k, v in published.items()}, 'live_scopes': frozenset(live)}


def _retire_moved(card: dict[str, Any], moved: dict[str, Any]) -> bool:
    """A remembered row whose card a fresh replica proves lives on ANOTHER gateway: retire it, never release it.

    A worker node runs another gateway's card from a copy in its own board file, and its collector used to publish
    that copy as its own (``hub/default/default/t_1f359ff1`` for a MacBook card, 10-29). Once the copy was evicted
    the record vanished, and absence is never closure, so the origin kept the row stale and open forever, naming an
    owner that has no such card. It is retired only on proof: the named owner is publishing that exact
    profile/board scope right now without the card, and another gateway publishes a qualified record with the
    same board, id and ``created_at`` (a copy keeps its ``created_at``). The real owner's record then speaks for
    the card: its own links, or this gateway's local board when the card is local. [daveey]"""
    if card.get('ownership_state') != 'qualified' or card.get('created_at') is None:
        return False
    scope = (card.get('gateway'), card.get('owner_profile'), card.get('board'))
    if scope not in moved.get('live_scopes', ()):
        return False
    gateways = moved.get('published', {}).get((card.get('board'), card.get('id'), card.get('created_at')), ())
    return any(gw != card.get('gateway') for gw in gateways)


_INFERRED = ('owner_removed', 'aged_out')


def _release_removed_owners(carried: list[dict[str, Any]], moved: dict[str, Any]) -> list[dict[str, Any]]:
    """Settle remembered rows a fresh replica can no longer attest record by record. [daveey]

    The Board drops a removed machine's records, and a collector stops publishing a card archived past its recent
    window. Such cards stayed remembered rows for good: 1,231 finished cards in 68 hub chats and 1,409 in 28
    MacBook chats after workers 2-8 were removed (2026-11-18), then 44 "open" copies and 20 closed leftovers.
    Three cases, for qualified unreleased rows, never on a stale replica (``moved`` is empty then):
    - owner removed (no ``gateway/info`` row, no task record, no fresh scope heartbeat; an offline owner keeps its
      info row and its records) and
      the row last reported done or archived: released as reported (``owner_removed``);
    - owner removed and another gateway publishes the same card (board, id, created_at): the row is retired,
      whatever it reported;
    - owner alive with a clean fresh read of the row's exact scope, nobody publishing the card, and the row last
      reported done or archived: released as reported (``aged_out``).
    Absence of ONE record of an owner that is otherwise present still closes nothing for a card last reported open
    (``_retire_moved`` covers the proven move). Evidence without ``known_gateways`` cannot tell removed from offline,
    so then nothing counts as removed. Publication lookups are the bulk ones ``remembered_publications`` resolved."""
    if not moved:
        return carried
    # ``owner_removed`` and ``aged_out`` are inferences, re-made on every fresh read, never completion receipts: such
    # a row is put back to its unresolved state first, so an owner that publishes or beats again (a reprovisioned
    # machine, even one republishing this very card as running with its session links dropped) takes the release back.
    rows = [({**{k: v for k, v in c.items() if k not in _INFERRED}, 'status': 'unknown', 'unfinished': True,
              'released': False} if any(c.get(k) for k in _INFERRED) else c) for c in carried]
    # Removed, not offline: an offline owner keeps its info row; a machine the Board has dropped has none (and no
    # fresh heartbeat). Evidence without ``known_gateways`` cannot tell them apart, so nothing counts as removed.
    known = moved.get('known_gateways')
    live = moved.get('live_scopes', ())
    listed = None if known is None else set(known) | {scope[0] for scope in live} | set(moved.get('publishing', ()))
    published = moved.get('published', {})

    def publishers(c):
        """The gateways publishing a qualified record of this exact card (board, id, created_at), resolved in bulk
        by ``remembered_publications``; None for a row with no usable created_at, which proves nothing."""
        key = (c.get('board'), c.get('id'), c.get('created_at'))
        try:
            return frozenset(published.get(key, ())) if c.get('created_at') is not None else None
        except TypeError:  # an unhashable created_at
            return None

    out = []
    for c in rows:
        gw = c.get('gateway')
        closed = c.get('status') == 'unknown' and c.get('reported_status') in ('done', 'archived')
        if c.get('released') or not gw or c.get('ownership_state') != 'qualified':
            out.append(c)
        elif listed is not None and gw not in listed:
            # The owner is gone. Another gateway publishing the same card is its real owner: this row was a worker
            # node's copy, and that record speaks for the card (``_retire_moved``, which needs the named owner
            # alive to say so). Hub, 2026-11-18: 44 cards listed open "on" removed workers; 36 were done or
            # archived and 7 were live hub cards.
            if any(other != gw for other in publishers(c) or ()):
                continue
            out.append({**c, 'status': c['reported_status'], 'unfinished': False, 'released': True,
                        'owner_removed': True} if closed else c)
        elif closed and (gw, c.get('owner_profile'), c.get('board')) in live and publishers(c) == frozenset():
            # The owner is alive and has just read this exact scope cleanly, and nobody publishes the card any
            # more (a record of it that merely dropped this chat's link would still be published). A collector
            # publishes every card of a scope except those archived past its recent window, so a card last
            # reported done or archived that has left the scope is archived or deleted: closed.
            out.append({**c, 'status': c['reported_status'], 'unfinished': False, 'released': True, 'aged_out': True})
        else:
            out.append(c)
    return out


def _fold_legacy_aliases(tasks: dict[tuple, dict[str, Any]], raw: dict[tuple, dict[str, Any]]) -> None:
    """Merge each unqualified ``board/id`` record into its proven profile-qualified twin, in place.

    Hub-sync used to key records ``board/id``; it now publishes ``profile/board/id`` and the old rows linger,
    so every pre-qualification card was both counted twice and stuck ``unknown`` forever. A legacy record is
    folded only when exactly one profile on the same owner gateway publishes that board/id AND both records
    carry the same non-null ``created_at`` and ``created_by`` (identity, not an ID match). A finished twin takes its
    shepherd links, so no session misses the terminal release. A twin still open publishes every live link itself:
    the frozen record's extra links are ones the owner released, and folding them would keep those chats waiting on
    the card for good. Anything ambiguous stays a separate ``unknown`` obligation. [daveey]
    """
    qualified: dict[tuple[str, str, str], list[tuple]] = {}
    for key in tasks:
        gw, owner_profile, board, tid = key
        if owner_profile is not None:
            qualified.setdefault((gw, board, tid), []).append(key)
    for key in [k for k in tasks if k[1] is None]:
        gw, _, board, tid = key
        twins = qualified.get((gw, board, tid), [])
        if len(twins) != 1:
            continue
        old, new = raw[key], raw[twins[0]]
        if (old.get('created_at') is None or old.get('created_at') != new.get('created_at')
                or old.get('created_by') != new.get('created_by')):
            continue
        twin = tasks[twins[0]]
        links = [*(twin.get('sessions') if isinstance(twin.get('sessions'), list) else [])]
        if twin.get('status') in ('done', 'archived'):
            links += [link for link in (old.get('sessions') if isinstance(old.get('sessions'), list) else [])
                      if isinstance(link, dict) and link.get('rel') == 'shepherd']
        tasks[twins[0]] = {**twin, 'sessions': links, '_legacy_key': f'{gw}/{board}/{tid}'}
        del tasks[key]
