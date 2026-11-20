"""Which chats a card belongs to, synced through the Board (RFC "Retiring the origin ledger", phase 5). [daveey]

``task_chat_links`` rows (card, chat gateway/profile/session, kind ``created``/``shepherd``, ``released``) travel
like any synced row, so every gateway's replica can answer "which cards does this chat have", on every gateway,
with one local query.

- :func:`reconcile` (run by the sync loop, idempotent, so its first run is the backfill) writes this gateway's
  links into a board file: chats that created a card here (``tasks.session_id``), chats that shepherd a card here
  (``tui`` subscriptions), and this gateway's chats' links to other gateways' cards from the origin-ledger
  projection, with its released state. Links it no longer finds are marked released, never deleted. As a card's
  owner it also publishes, insert-only, the link of each chat on another gateway its ``platform='hub'``
  subscriptions name (``_owner_hub_links``), so that chat's task list does not wait on that gateway's own sync.
- :func:`replica_cards` answers a chat lineage's cards on other gateways from the replicas; ``session_tasks`` AND the
  sidebar counts use it instead of the origin ledger when ``kanban.sync.links_reader`` is ``replica``, so the
  count and the task strip are one projection in each mode.
- :func:`links_diff` compares the two answers per chat, ``unknown`` unfinished obligations included, leaving out
  the chats the writer never links (machine sessions, placeholders: counted in ``machine_links``); readers
  switch only once it is clean. A card on a profile's own board (hermes-hub's qualified owner route, e.g. hub
  ``profiles/bb-tuner/kanban.db``) never reaches the Board, so both the reader and the diff take it from the ledger
  (``profile_board_links``).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from hermes_cli import kanban_chat_links_released as _released
from hermes_cli.kanban_replica_execution import read_runs, run_evidence
from hermes_cli.kanban_replica_upstream import fields as upstream_fields
from hermes_cli.kanban_run_machine import placement_select, run_machine
from hermes_cli.kanban_session_activity_release import (DONE_PENDING_RELEASE, TERMINAL_STATUSES, UNKNOWN,
                                                       unreleased_obligation)

_log = logging.getLogger(__name__)

KINDS = ("created", "shepherd")
OPEN_STATUSES = ("todo", "ready", "running", "scheduled", "blocked", "review", "triage")


def link_uid(task_id: str, gateway: str, profile: Optional[str], session: str, kind: str) -> str:
    return f"{task_id}:{gateway}:{profile or '-'}:{session}:{kind}"


def _worker_shepherd(session: Any, kind: str, gateway=None, profile=None) -> bool:
    """A headless execution receipt is provenance, never a chat destination. [daveey]"""
    from hermes_cli.kanban_db_notify import is_headless_session
    return is_headless_session(session, gateway=gateway, profile=profile)


def _worker_shepherds(sessions: Iterable[Any], gateway=None, profile=None) -> set[str]:
    """:func:`_worker_shepherd` for many sessions: the ``str(session or "")`` refs it is true for, from one
    ``scoped_sources`` read instead of one per row (t_01a1d2cc49634a78). [daveey]"""
    from hermes_cli.kanban_db_notify import headless_sessions
    return headless_sessions(sessions, gateway=gateway, profile=profile)


def _machine_by_profile(gateway: str, rows: Iterable[tuple[Any, Any]]) -> dict[tuple, set[str]]:
    """``machine_sessions`` once per exact chat profile for ``(profile, session)`` rows, keyed ``(type, value)``
    like :func:`_ledger_links`, so ``None`` and ``""`` stay distinct. A row is a machine session iff
    ``str(session or "") in out[(type(profile), profile)]``: the per-row ``is_machine_session`` verdict. [daveey]"""
    from hermes_cli.kanban_db_notify import machine_sessions, prefetch_sources
    groups: dict[tuple, tuple[Any, list[Any]]] = {}
    for profile, session in rows:
        groups.setdefault((type(profile), profile), (profile, []))[1].append(session)
    if len(groups) > 1:  # each home read once for every profile's refs, not once per profile
        prefetch_sources(s for _p, refs in groups.values() for s in refs)
    return {key: machine_sessions(gateway, refs, local_gateway=gateway, profile=profile)
            for key, (profile, refs) in groups.items()}


def _local_links(conn: sqlite3.Connection, gateway: str) -> dict[str, dict[str, Any]]:
    """Links from this board file's own data: creating chats and ``tui`` shepherd subscriptions.

    A machine session of this gateway (``is_machine_session``: a worker/cron/one-shot/subagent transcript) is no
    chat, so it is never wanted: :func:`reconcile` releases a link to it and never writes one back. [daveey]"""
    from hermes_cli.kanban_db_notify import machine_sessions, prefetch_sources
    from hermes_cli.kanban_session_activity import _counted_sub_clause
    now = int(time.time())
    out: dict[str, dict[str, Any]] = {}
    has_links = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone())
    created = [dict(r) for r in conn.execute(
        "SELECT l.* FROM task_chat_links l WHERE l.chat_gateway=? AND l.kind='created' "
        "AND EXISTS (SELECT 1 FROM task_events e WHERE e.task_id=l.task_id "
        "AND e.kind='creator_chats_resolved')", (gateway,))] if has_links else []
    # Cards this gateway owns (a picked-up foreign copy keeps the owner's answer, see _owner_written_releases).
    foreign = {r[0] for r in conn.execute(
        "SELECT id FROM tasks WHERE created_on IS NOT NULL AND created_on != '' AND created_on != ?", (gateway,))} \
        if created and any(c[1] == "created_on" for c in conn.execute("PRAGMA table_info(tasks)")) else set()
    sessions = [tuple(r) for r in conn.execute(
        "SELECT id, session_id, created_at FROM tasks WHERE session_id IS NOT NULL AND session_id != ''")]
    try:
        shepherds = [tuple(r) for r in conn.execute(
            f"SELECT s.task_id, s.chat_id FROM kanban_notify_subs s JOIN tasks t ON t.id = s.task_id "
            f"WHERE s.platform = 'tui' AND s.chat_id != '' AND {_counted_sub_clause(conn)}")]
    except sqlite3.Error:  # board predates notify subscriptions
        from hermes_cli.kanban_links_chat_key import degrade_unless_absent
        degrade_unless_absent(conn, "kanban_notify_subs")
        shepherds = []
    candidates = [r[1] for r in sessions] + [r[1] for r in shepherds]
    # [daveey] Each verdict below is classified in one batch, not per row: a per-row lookup reopened every home's
    # state.db once the 300 s source cache expired, so a pass on a 3k-card hub board ran for 15-28 min and held up
    # the replica pull and outbox push behind it (t_01a1d2cc49634a78). One prefetch reads each home once.
    prefetch_sources(candidates + [r["chat_session"] for r in created])
    machine = machine_sessions(gateway, candidates, local_gateway=gateway)
    headless = _worker_shepherds(candidates)
    created_machine = _machine_by_profile(gateway, ((r["chat_profile"], r["chat_session"]) for r in created))

    def add(task_id: str, session: str, kind: str, created_at: Any) -> None:
        from hermes_cli.kanban_placement import is_placeholder_session
        if is_placeholder_session(session) or str(session or "") in headless or session in machine:
            return
        uid = link_uid(task_id, gateway, None, session, kind)
        out[uid] = {"uid": uid, "task_id": task_id, "chat_gateway": gateway, "chat_profile": None,
                    "chat_session": session, "kind": kind, "released": 0, "created_at": int(created_at or now)}

    for row in created:
        if str(row["chat_session"] or "") not in created_machine[(type(row["chat_profile"]), row["chat_profile"])]:
            # [daveey] Wanted unreleased, like a ``tasks.session_id`` creator link: creator-chat ownership has no
            # release on this gateway but the machine/placeholder rules above. Keeping the stored flag let one
            # foreign reconcile (a named-profile gateway on an older core holding the host sync lock) release it
            # for good, and the shepherd link this link replaces was then released too: the chat lost an open
            # card it handed work to (hub chat 20261016_123957_6212f7 -> t_01a19084a3b36956, 2026-11-02).
            out[row["uid"]] = row if row["task_id"] in foreign else {**row, "released": 0}
    for task_id, session, created_at in sessions:
        add(task_id, session, "created", created_at)
    for task_id, session in shepherds:
        if link_uid(task_id, gateway, None, session, "created") not in out:
            add(task_id, session, "shepherd", None)
    return out


def _ledger_links(board: str, gateway: str) -> dict[str, dict[str, Any]]:
    """This gateway's chats' links to other gateways' cards on ``board``, from the origin-ledger projection.

    Machine sessions are classified once per origin profile (``machine_sessions``, keyed ``str(ref)``), not once
    per row: each per-row ``is_machine_session`` repeated profile discovery and the state.db reads. [daveey]"""
    from hermes_cli.kanban_db_notify import machine_sessions
    from hermes_cli.kanban_placement import is_placeholder_session
    from hermes_cli.kanban_session_activity import _remote_links
    now = int(time.time())
    out: dict[str, dict[str, Any]] = {}
    eligible: list[tuple[dict[str, Any], tuple]] = []
    groups: dict[tuple, tuple[Any, list[Any]]] = {}  # exact profile (type, value) -> (profile, refs)
    for card in _remote_links():
        if card.get("board") != board or card.get("origin_gateway") not in (None, gateway) or not card.get("session"):
            continue
        if is_placeholder_session(card["session"]):
            continue
        profile = card.get("origin_profile")
        key = (type(profile), profile)
        groups.setdefault(key, (profile, []))[1].append(card["session"])
        eligible.append((card, key))
    machine = {key: machine_sessions(gateway, refs, local_gateway=gateway, profile=profile)
               for key, (profile, refs) in groups.items()}
    for card, key in eligible:
        if str(card["session"]) in machine[key]:
            continue
        kind = "created" if card.get("via") == "origin" else "shepherd"
        profile = card.get("origin_profile")
        uid = link_uid(card["id"], gateway, profile, card["session"], kind)
        # [daveey] Two gateways can publish the same card id (a worker's copy of another gateway's card). The link is
        # one row, so it is released only when every copy is: an open copy keeps it, whatever order they arrive in.
        # It used to be last-copy-wins, and a stopped gateway's finished copy now stays released for good.
        released = 1 if card.get("released") and out.get(uid, {}).get("released", 1) else 0
        out[uid] = {"uid": uid, "task_id": card["id"], "chat_gateway": gateway, "chat_profile": profile,
                    "chat_session": card["session"], "kind": kind, "released": released, "created_at": now}
    return out


def _owner_written_releases(board: str, gateway: str, want: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Released copies of the ``created`` and ``shepherd`` links another gateway (the card's owner) wrote for this
    gateway's chats, once the replica shows the card closed (``done``/``archived``). [daveey]

    The owner writes them in ``kanban_creator_chats`` (``initialize_ownership`` / ``inherit_chats``) for a remote
    origin chat, and from its ``platform='hub'`` subscriptions (``_owner_hub_links``, ``created`` or ``shepherd``).
    The owner's reconcile manages only its own chats' links, so nothing released them: a done card
    whose origin ledger had no row for that chat (no ledger link with the same uid, no released shepherd sibling)
    stayed an ``unknown`` obligation in ``replica_cards`` forever (4 hub cards on macbook chat
    20261027_134353_5ab65c, 2026-11-01). The chat's gateway releases them by writing the same uid (the same row on
    the Board) with ``released = 1``, as it already does for a ledger ``origin`` row. Open cards and missing card
    rows are left alone, except a row whose chat session compressed into one this gateway links to the same card
    (``_compressed_away``); links ``want`` already carries keep the ledger's answer. A card reopened after the release
    stays released for that chat: the uid is one row, so the owner's original intent is not recoverable here.

    A link to a machine session of this gateway (``is_machine_session``) is released whatever the card's status:
    no human reads that session, so it owns nothing even on an open card (hub t_01a19864fc42f5b2 was owned by
    MacBook worker transcript 20261029_180356_84db5f, 2026-11-01)."""
    from hermes_cli import kanban_links_chat_key as chat_key, kanban_sync_client as ksc
    from hermes_cli.kanban_lockguard import connect
    from hermes_cli.kanban_placement import is_placeholder_session
    path = ksc.shadow_path(board)
    if not path.exists():
        return {}
    try:
        conn = connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute(
                "SELECT l.uid, l.task_id, l.chat_gateway, l.chat_profile, l.chat_session, l.kind, l.created_at, "
                "t.status FROM task_chat_links l JOIN tasks t ON t.id = l.task_id "
                "WHERE l.chat_gateway = ? AND l.kind IN ('created', 'shepherd') AND l.released = 0 "
                "AND l.uid IS NOT NULL AND t.created_on IS NOT NULL AND t.created_on != ?",
                (gateway, gateway))]
        finally:
            conn.close()
    except sqlite3.Error as exc:  # an unreadable replica releases nothing this round; the next one retries
        _log.info("kanban chat links: replica %s unreadable for owner-link releases (%s)", path, exc)
        chat_key.degrade()
        return {}
    out: dict[str, dict[str, Any]] = {}
    rows = [r for r in rows if r["uid"] not in want and not is_placeholder_session(r["chat_session"])]
    # One machine classification per chat profile for the open cards' rows, not one per row: this per-row
    # lookup was 60% of a 60 s profile of the hub's sync thread (t_01a1d2cc49634a78). [daveey]
    machine = _machine_by_profile(gateway, ((r["chat_profile"], r["chat_session"]) for r in rows
                                            if r["status"] not in TERMINAL_STATUSES))
    superseded = _compressed_away(rows, want, gateway)
    for r in rows:
        # [daveey] A worker's ``local-<sha256>`` is released like any machine session, not skipped as a shepherd:
        # another gateway proves a foreign hash chat by an unreleased ``created`` link (``proven_foreign_chats``),
        # and only this gateway can say the hash is a worker (review t_01a1cf9aab0349a9).
        if r.pop("status") not in TERMINAL_STATUSES and str(r["chat_session"] or "") not in machine[
                (type(r["chat_profile"]), r["chat_profile"])] and r["uid"] not in superseded:
            continue
        out[r["uid"]] = {**r, "released": 1, "created_at": int(r["created_at"] or time.time())}
    return out


def _compressed_away(rows: list[dict[str, Any]], want: dict[str, dict[str, Any]], gateway: str) -> set[str]:
    """Uids of owner-written rows on OPEN cards whose chat session is a compression ancestor of a session this
    gateway already links, unreleased, to the same card and chat profile. [daveey]

    The owner writes its link from the card's ``platform='hub'`` subscription (``_owner_hub_links``), which names
    the session that placed the card. A chat that compressed before its gateway's ``accept_binding`` (its Board
    sync down) has its ledger resolve the tip instead, so the card had two unreleased rows, the ancestor's from the
    owner and the tip's from here: ``session_tasks`` deduped them but ``links_diff`` listed the ancestor
    ``only_in_replica`` until the card closed (review of PR #2095). The descendant's row is the chat's link; the
    ancestor's is released, as a closed card's is. Only this gateway's ``state.db`` (the chat profile's home) knows
    the lineage; an unreadable one releases nothing this pass."""
    from hermes_cli.kanban_origin import _ro
    from hermes_cli.kanban_session_provenance import _local_homes
    from hermes_cli import kanban_links_chat_key as chat_key
    from hermes_state_compression import _CHAIN_CAP
    reads = chat_key.active()
    linked: dict[tuple[str, Any], set[str]] = {}
    for link in want.values():
        if link.get("task_id") and not link.get("released") and link.get("chat_gateway") == gateway:
            linked.setdefault((link["task_id"], link.get("chat_profile")), set()).add(str(link.get("chat_session")))
    candidates: dict[Any, list[dict[str, Any]]] = {}
    for r in rows:
        sessions = linked.get((r["task_id"], r["chat_profile"]))
        if r["status"] not in TERMINAL_STATUSES and sessions and str(r["chat_session"]) not in sessions:
            candidates.setdefault(r["chat_profile"], []).append(r)
    out: set[str] = set()
    for profile, group in candidates.items():
        for home in _local_homes(profile):
            path = home / "state.db"
            if not (path.exists() if reads is None else reads.answer(("exists", str(path)), path.exists)):
                continue
            try:
                db = _ro(path)
            except sqlite3.Error as exc:
                _log.info("kanban chat links: %s unreadable for compression lineage (%s)", path, exc)
                chat_key.degrade()
                continue
            try:
                for r in group:
                    sid, seen = str(r["chat_session"]), {str(r["chat_session"])}
                    for _ in range(_CHAIN_CAP):
                        step = chat_key.chain_step(db, path, sid)
                        if step is None or step in seen:
                            break
                        sid = step
                        seen.add(sid)
                        if sid in linked[(r["task_id"], r["chat_profile"])]:
                            out.add(r["uid"])
                            break
            except sqlite3.Error as exc:
                _log.info("kanban chat links: compression lineage unreadable in %s (%s)", path, exc)
                chat_key.degrade()
            finally:
                db.close()
    return out


def _owner_hub_links(conn: sqlite3.Connection, board: str, gateway: str) -> list[dict[str, Any]]:
    """Links this gateway (the card's owner) publishes for chats on OTHER gateways: one per counted ``platform='hub'``
    subscription ``<gw>/<profile>/<session>`` on an open card it owns, the same subscriptions the hermes-hub collector
    publishes as the card's ``sessions`` (so notices and the task list read one membership). [daveey]

    Before this only the chat's own gateway wrote that link, from its origin ledger, so while its Board sync was down
    (David-MBP gave up the sync role, SchemaAhead, 2026-11-13) six cards it placed elsewhere had no row on the Board:
    its task list, sidebar counts and ask-gate missed them although their done notices arrived through hub.db.

    The row is the one the chat's gateway writes for the same link (same uid: ``created`` for a courier-bound origin,
    whose ledger row is ``via: origin``; ``shepherd`` otherwise), so when that gateway's reconcile comes back it
    upserts this very row, never a second one. It is written once and never changed here: the chat's gateway owns
    its release (its ledger, ``_owner_written_releases``). Nothing is written while any row for that card and chat
    exists in this file or the replica, released included, so a release the chat's gateway already made is never
    reopened; an unreadable replica writes nothing this pass."""
    from hermes_cli import kanban_links_chat_key as chat_key, kanban_sync_client as ksc
    from hermes_cli.kanban_db_notify import machine_sessions
    from hermes_cli.kanban_lockguard import connect
    from hermes_cli.kanban_origin import OriginError, parse_origin
    from hermes_cli.kanban_origin_intent_carry import EVENT_KIND
    from hermes_cli.kanban_placement import is_placeholder_session
    from hermes_cli.kanban_session_activity import _counted_sub_clause
    task_cols = {c[1] for c in conn.execute("PRAGMA table_info(tasks)")}
    mine = "AND (t.created_on IS NULL OR t.created_on = '' OR t.created_on = ?)" if "created_on" in task_cols else ""
    try:
        subs = conn.execute(
            f"SELECT s.task_id, s.chat_id, s.created_at FROM kanban_notify_subs s JOIN tasks t ON t.id = s.task_id "
            f"WHERE s.platform = 'hub' AND s.chat_id != '' AND t.status NOT IN ({', '.join('?' * len(TERMINAL_STATUSES))}) "
            f"AND {_counted_sub_clause(conn)} {mine} ORDER BY s.task_id, s.chat_id",
            (*TERMINAL_STATUSES, *((gateway,) if mine else ()))).fetchall()
    except sqlite3.Error:  # board predates notify subscriptions
        chat_key.degrade_unless_absent(conn, "kanban_notify_subs")
        return []
    wanted: dict[tuple, dict[str, Any]] = {}
    for task_id, chat_id, created_at in subs:
        try:
            origin = parse_origin(chat_id)
        except OriginError:
            continue
        if origin.gateway == gateway or is_placeholder_session(origin.session):
            continue  # this gateway's own chats are its ``tui`` subscriptions (``_local_links``)
        wanted.setdefault((task_id, origin.gateway, origin.profile, origin.session),
                          {"created_at": int(created_at or time.time())})
    if not wanted:
        return []
    groups: dict[tuple[str, str], list[str]] = {}
    for _tid, gw, profile, session in wanted:
        groups.setdefault((gw, profile), []).append(session)
    # [daveey] The chat gateway's own verdict (``machine_sessions``, as its ``_owner_written_releases`` releases by):
    # worker hashes AND ``cron_`` sessions. With only the headless filter a cron chat's sub got a row that gateway
    # released on its next pass. A foreign ref is never looked up locally, so this reads no state.db.
    machine = {(gw, profile): machine_sessions(gw, sessions, local_gateway=gateway, profile=profile)
               for (gw, profile), sessions in groups.items()}
    wanted = {k: v for k, v in wanted.items() if k[3] not in machine[(k[1], k[2])]}
    task_ids = sorted({k[0] for k in wanted})
    have: set[tuple] = set()
    bound: set[tuple] = set()
    path = ksc.shadow_path(board)
    try:
        replica = connect(f"file:{path}?mode=ro", uri=True, timeout=5) if path.exists() else None
    except sqlite3.Error as exc:
        _log.info("kanban chat links: replica %s unreadable for owner hub links (%s)", path, exc)
        chat_key.degrade()
        return []
    try:
        for db in (conn, replica):
            if db is None:
                continue
            for i in range(0, len(task_ids), 500):
                chunk = task_ids[i:i + 500]
                have.update(tuple(r) for r in db.execute(
                    "SELECT task_id, chat_gateway, COALESCE(chat_profile, 'default'), chat_session FROM task_chat_links "
                    f"WHERE task_id IN ({', '.join('?' * len(chunk))})", chunk))
        for i in range(0, len(task_ids), 500):
            chunk = task_ids[i:i + 500]
            for task_id, payload in conn.execute(
                    f"SELECT task_id, payload FROM task_events WHERE kind = ? AND task_id IN "
                    f"({', '.join('?' * len(chunk))})", (EVENT_KIND, *chunk)):
                try:
                    origin = json.loads(json.loads(payload)["binding_json"])["origin"]
                    bound.add((task_id, origin["gateway"], origin["profile"], origin["session"]))
                except (KeyError, TypeError, ValueError):
                    continue
    except sqlite3.Error as exc:  # nothing is written from a partial read; the next pass retries
        _log.info("kanban chat links: owner hub links unreadable for %s (%s)", board, exc)
        chat_key.degrade()
        return []
    finally:
        if replica is not None:
            replica.close()
    out = []
    for key, extra in wanted.items():
        if key in have:
            continue
        task_id, gw, profile, session = key
        kind = "created" if key in bound else "shepherd"
        out.append({"uid": link_uid(task_id, gw, profile, session, kind), "task_id": task_id, "chat_gateway": gw,
                    "chat_profile": profile, "chat_session": session, "kind": kind, "released": 0, **extra})
    return out


def ledger_boards(gateway: str) -> set[str]:
    """Boards on which this gateway's chats have cards, per the origin-ledger projection."""
    from hermes_cli.kanban_session_activity import _remote_links
    return {c["board"] for c in _remote_links() if c.get("board") and c.get("origin_gateway") in (None, gateway)}


def _plan(conn: sqlite3.Connection, board: str, gateway: str, ledger: bool) -> tuple[dict, list]:
    """``(want, rows)``: the links this gateway wants in ``conn`` and the rows it has (read-only)."""
    from hermes_cli import loop_accounting as la
    want = la.timed("sync.links.local", _local_links, conn, gateway)
    if ledger:
        want.update(la.timed("sync.links.ledger", _ledger_links, board, gateway))
    want.update(la.timed("sync.links.owner_releases", _owner_written_releases, board, gateway, want))
    with la.pass_("sync.links.existing"):
        rows = conn.execute("SELECT uid, released, chat_session, kind, task_id, chat_profile FROM task_chat_links "
                            "WHERE chat_gateway = ?", (gateway,)).fetchall()
    return want, rows


def _machine_releases(want: dict, rows: list, gateway: str) -> list[dict[str, Any]]:
    """The links a reconcile pass releases because their chat is a machine session of this gateway: an unreleased
    row nothing wants any more, or an owner-written row it writes back released (``_owner_written_releases``)."""
    from hermes_cli.kanban_db_notify import machine_sessions
    from hermes_cli.kanban_placement import is_placeholder_session
    have = {r[0]: r for r in rows}
    out = [{"uid": r[0], "task_id": r[4], "chat_session": r[2], "kind": r[3]} for r in rows
           if not r[1] and r[0] not in want]
    out += [{"uid": uid, "task_id": l["task_id"], "chat_session": l["chat_session"], "kind": l["kind"]}
            for uid, l in want.items() if l["released"] and (uid not in have or not have[uid][1])]
    headless = _worker_shepherds(o["chat_session"] for o in out)
    out = [o for o in out if not is_placeholder_session(o["chat_session"])
           and str(o["chat_session"] or "") not in headless]
    machine = machine_sessions(gateway, [o["chat_session"] for o in out], local_gateway=gateway)
    return sorted((o for o in out if o["chat_session"] in machine), key=lambda o: o["uid"])


def machine_links(conn: sqlite3.Connection, board: str, gateway: str, *, ledger: bool = True) -> list[dict[str, Any]]:
    """Dry run: the links to this gateway's machine sessions the next :func:`reconcile` of ``board`` releases."""
    return _machine_releases(*_plan(conn, board, gateway, ledger), gateway)


def _write_release_receipt(board: str, gateway: str, released: list[dict[str, Any]]) -> None:
    """``~/.hermes/kanban/repairs/machine-links-<board>-<ts>.json``: the uids one pass released (audit trail)."""
    from hermes_cli import kanban_db
    try:
        path = kanban_db.kanban_home() / "kanban" / "repairs" / (
            f"machine-links-{board}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{len(released)}.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"board": board, "gateway": gateway, "released_at": int(time.time()),
                                    "released": released}, indent=1) + "\n", encoding="utf-8")
        _log.info("kanban chat links: %s released %d links to machine sessions (receipt %s)", board, len(released), path)
    except OSError as exc:  # the release itself is committed; only the audit copy is lost
        _log.warning("kanban chat links: machine-link receipt for %s not written (%s)", board, exc)


def reconcile(conn: sqlite3.Connection, board: str, gateway: str, *, ledger: bool = True) -> dict[str, int]:
    """Bring this gateway's links in ``conn`` (one board file) up to date. Writes go through the capture triggers,
    so every new or changed link is pushed to the service. Returns counts of added/changed/released links.

    A link to a machine session of this gateway (``kanban_db_notify.is_machine_session``) is released here, through
    the same captured writes; each pass that releases any writes a receipt (``_write_release_receipt``). [daveey]"""
    from hermes_cli.kanban_db_connect import write_txn
    from hermes_cli.kanban_creator_chats import backfill
    from hermes_cli import loop_accounting as la
    la.timed("sync.links.backfill", backfill, conn)
    want, rows = la.timed("sync.links.plan", _plan, conn, board, gateway, ledger)
    machine = la.timed("sync.links.machine_releases", _machine_releases, want, rows, gateway)
    from hermes_cli.kanban_placement import is_placeholder_session
    have = {r[0]: r[1] for r in rows}
    # A placement attribution id is not a chat: its link is deleted (captured, so the Board drops it too), never
    # merely released; a released one still counted in every diff and replica read. A worker transcript's
    # shepherd link is no chat either (its wakes are refused). [daveey]
    # A released ``created`` row to a worker hash is the release record of an owner-written link
    # (``_owner_written_releases``): deleting it would hand the owner's unreleased copy back. [daveey]
    with la.pass_("sync.links.classify"):
        shepherd_rows: dict[tuple, tuple[Any, list[Any]]] = {}  # exact chat profile -> (profile, sessions)
        for r in rows:
            shepherd_rows.setdefault((type(r[5]), r[5]), (r[5], []))[1].append(r[2])
        headless = {key: _worker_shepherds(sessions, gateway, profile)
                    for key, (profile, sessions) in shepherd_rows.items()}
        placeholders = {r[0] for r in rows if is_placeholder_session(r[2]) or (
            str(r[2] or "") in headless[(type(r[5]), r[5])] and not (r[1] and r[3] == "created"))}
    added = changed = released = retired = 0
    owner_links = la.timed("sync.links.owner_links", _owner_hub_links, conn, board, gateway)
    with la.pass_("sync.links.write"), write_txn(conn):
        for link in owner_links:
            # Insert-only, and only while no row for this card and chat exists (a concurrent write, or a pull that
            # brought the chat gateway's own row in, wins). See ``_owner_hub_links``.
            added += conn.execute(
                "INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, released, "
                "created_at) SELECT :uid, :task_id, :chat_gateway, :chat_profile, :chat_session, :kind, 0, :created_at "
                "WHERE NOT EXISTS (SELECT 1 FROM task_chat_links WHERE task_id = :task_id AND chat_gateway = "
                ":chat_gateway AND COALESCE(chat_profile, 'default') = :chat_profile AND chat_session = :chat_session)",
                link).rowcount
        for uid in placeholders - set(want):
            conn.execute("DELETE FROM task_chat_links WHERE uid = ?", (uid,))
            have.pop(uid)
            retired += 1
        for uid, link in want.items():
            if uid not in have:
                conn.execute(
                    "INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, released, "
                    "created_at) VALUES (:uid, :task_id, :chat_gateway, :chat_profile, :chat_session, :kind, :released, "
                    ":created_at)", link)
                added += 1
            elif have[uid] != link["released"]:
                conn.execute("UPDATE task_chat_links SET released = ? WHERE uid = ?", (link["released"], uid))
                changed += 1
        for uid, was_released in have.items():
            if uid not in want and not was_released:
                conn.execute("UPDATE task_chat_links SET released = 1 WHERE uid = ?", (uid,))
                released += 1
    if machine:
        _write_release_receipt(board, gateway, machine)
    return {"added": added, "changed": changed, "released": released, "retired": retired}


# Rows last read from each replica, so an unreadable replica keeps its rows (stale) instead of dropping them.
_last_good: dict[tuple[str, str, str], tuple[list[dict[str, Any]], Optional[float], dict[str, dict]]] = {}


def _reset_for_tests() -> None:
    global _owners_memo
    _last_good.clear()
    _replica_rows.clear()
    _owners_memo = None


def _stale_after_s() -> float:
    """A replica not pulled for this long is stale: the hub-sync window, or three sync periods if that is longer."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_session_activity import HUB_STALE_AFTER_S
    try:
        period = float(sync.sync_config().get("interval_seconds") or 0)
    except (TypeError, ValueError):
        period = 0.0
    return max(HUB_STALE_AFTER_S, 3 * period)


def _read_replica(path: Path, gateway: str,
                  profile: str) -> tuple[list[dict[str, Any]], Optional[float], dict[str, dict]]:
    """Every unreleased link of this gateway+profile's chats in one replica, with its card row (or none when the
    card row is missing), the replica's last successful pull, and the current-run terms of the linked running cards
    (``kanban_replica_execution.read_runs``). Raises ``sqlite3.Error`` when unreadable.

    [daveey] A released sibling link (same card and chat, other kind) no longer settles a closed card here. That
    reader rule (9d13eeb47e, 2026-11-01) assumed the released sibling carried a verified release; the writer fix
    for the same incident (``_owner_written_releases``) releases the owner's unmanaged ``created`` link itself, so
    the rule only ever fired where the chat's own ledger still held the other link unreleased: a done card whose
    release is unverified, whose owner-written ``shepherd`` sibling was released because the card closed. It hid 19
    such hub cards from chat 20261026_170127_f07c32 that the ledger lists as ``unknown`` (David-MBP links_diff,
    2026-11-13). Each link now carries its own answer, as ``replica_cards`` says: only a released link drops it."""
    from hermes_cli.kanban_activity_revision import SHADOW_TABLES
    from hermes_cli.kanban_session_activity import _board_content_key, cached_ro  # lazy: that module imports this one
    # [daveey] The link rows only change with the replica's tasks/task_chat_links content; a pull that only moves
    # ``pulled_at`` (every few seconds) re-reads the stamp, not the ~10k links. Rows are read-only to callers.
    content = _board_content_key(path, SHADOW_TABLES)
    with cached_ro(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = replica_link_rows(conn, path, gateway, profile, content)
        # Never memoised: a pulled heartbeat is bookkeeping, so ``content`` does not move for it, while the run's
        # newest liveness event is exactly what dates the card (review t_01a1d0f7c4cc4688).
        runs = read_runs(conn, gateway, profile)
        try:
            row = conn.execute("SELECT pulled_at FROM kanban_sync_state LIMIT 1").fetchone()
            pulled = float(row[0]) if row and row[0] is not None else None
        except sqlite3.OperationalError:  # replica written before pulls were stamped
            pulled = None
    return rows, pulled, runs


# (replica path, gateway, profile) -> (content key, link rows) of the last ``_read_replica``.
_replica_rows: dict[tuple[str, str, str], tuple[tuple, list[dict[str, Any]]]] = {}


def replica_link_rows(conn: sqlite3.Connection, path: Path, gateway: str, profile: str,
                      content: tuple) -> list[dict[str, Any]]:
    """[daveey] This origin's unreleased link rows in replica ``path`` (``_read_replica_links`` on ``conn``, whose
    ``row_factory`` must be ``sqlite3.Row``), memoised under ``content`` (the shadow's content key). Only a
    trigger-backed revision vouches for the rows; a stat key (no counter) always reads. Shared with the annotation
    holder's narrow key (``kanban_annotation_narrow``), so a shadow commit is read once for both."""
    from hermes_cli.kanban_activity_revision import is_revision
    memo_key = (str(path), gateway, profile)
    hit = _replica_rows.get(memo_key)
    if hit is not None and hit[0] == content and is_revision(content):
        return hit[1]
    rows = _read_replica_links(conn, gateway, profile)
    _replica_rows[memo_key] = (content, rows)
    return rows


def _read_replica_links(conn: sqlite3.Connection, gateway: str, profile: str) -> list[dict[str, Any]]:
    from hermes_cli.kanban_replica_upstream import enrich, optional_select
    rows = [dict(r) for r in conn.execute(
        "SELECT l.task_id AS id, t.id AS task_row, t.title, t.status, t.assignee, t.created_at, t.started_at, "
        f"t.priority, t.block_kind, t.human_request, t.created_on, {placement_select(conn, 't')}, "
        f"{placement_select(conn, 't', ('ready_since',))}, "
        f"l.chat_session, l.chat_profile, l.kind {optional_select(conn)} "
        "FROM task_chat_links l LEFT JOIN tasks t ON t.id = l.task_id "
        "WHERE l.chat_gateway = ? AND l.chat_profile = ? AND l.released = 0 "
        "AND (t.id IS NULL OR (t.created_on IS NOT NULL AND t.created_on != ?))",
        (gateway, profile, gateway)).fetchall()]
    return enrich(conn, rows)


def _ledger_owners(stale_ok: bool = False) -> dict[tuple[str, str], list[tuple[str, str, str, bool]]]:
    """``(board, id) -> [(owner gateway, owner profile, session, released)]`` from the origin-ledger projection. A
    synced board is one file per gateway with one row per id, shared by that gateway's profiles, so the replica
    row carries no owner profile: the ledger only labels it (``_owner_of``). ``stale_ok`` is ``_remote_links``'s:
    take the last projection instead of waiting on a rebuild."""
    return _ledger_index(stale_ok)[0]


def _ledger_index(stale_ok: bool = False, cards: Optional[list[dict[str, Any]]] = None) -> tuple:
    """``(_ledger_owners, the ledger's profile-board cards, owner-published waits)`` from one origin-ledger
    projection; ``({}, [], {})`` when it cannot be read (all are additions to the replica's answer, never a reason to
    hide it). Waits are keyed ``(owner gateway, owner profile, board, id)`` (``_ledger_wait``)."""
    from hermes_cli.kanban_session_activity import _remote_links
    global _owners_memo
    try:
        if cards is None:
            cards = _remote_links(stale_ok=True) if stale_ok else _remote_links()
    except Exception as exc:  # health: allow BLE001 -- the profile is a label; a failed ledger read must not hide cards
        _log.info("kanban chat links: owner profiles unavailable (%s: %s)", type(exc).__name__, exc)
        return {}, [], {}
    # [daveey] ``_remote_links`` hands out a new list of the SAME read-only card dicts until it re-projects: the same
    # cards (by identity; the memo holds them, so no id is reused) give the same owners. Rebuilding this index from
    # ~5k cards was ~15 ms of every count recompute on David-MBP.
    memo = _owners_memo
    if memo is not None and len(memo[0]) == len(cards) and all(a is b for a, b in zip(memo[0], cards)):
        return memo[1], memo[2], memo[3]
    out: dict[tuple[str, str], list[tuple[str, str, str, bool]]] = {}
    waits: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for c in cards:
        if c.get("gateway") and c.get("board") and c.get("id") and c.get("profile"):
            out.setdefault((str(c["board"]), str(c["id"])), []).append(
                (str(c["gateway"]), str(c["profile"]), str(c.get("session") or ""), bool(c.get("released"))))
            status = c.get("reported_status", c.get("status"))
            parked = status == "scheduled" or (status == "blocked" and c.get("block_kind") == "time")
            wait = {k: c[k] for k in LEDGER_WAIT_FIELDS if c.get(k)} if parked else {}
            if wait:
                waits[(str(c["gateway"]), str(c["profile"]), str(c["board"]), str(c["id"]))] = {
                    **wait, "_status": status}
    profile_cards = [c for c in cards if _is_profile_board_card(c)]
    _owners_memo = (cards, out, profile_cards, waits)
    return out, profile_cards, waits


# What a scheduled (or blocked kind=time) card waits on, published by its owner (``kanban_wait_replica.published_park``)
# and carried by the origin-ledger cards only: a Board replica row has no park. ``replica_cards`` copies them onto its
# row. Other rows' run-derived ``park`` is not a wait and is never copied. [daveey]
LEDGER_WAIT_FIELDS = ("park", "awaiting", "wake_check")


def _ledger_wait(index: tuple, owner: str, owner_profile: str, board: str, tid: str,
                 status: Any) -> dict[str, Any]:
    """The owner-published wait fields of one card from ``_ledger_index``'s third map, only when the ledger record
    reports the replica row's status (a park published for an earlier status is not this card's wait)."""
    waits = index[2] if len(index) > 2 else {}
    wait = waits.get((owner, owner_profile, board, tid))
    if not wait or wait.get("_status") != status:
        return {}
    return {k: v for k, v in wait.items() if k != "_status"}


# (cards, owners, profile-board cards, waits) of the last ``_ledger_index`` build. Callers treat them as read-only.
_owners_memo: tuple[list[dict[str, Any]], dict[tuple[str, str], list[tuple[str, str, str, bool]]],
                    list[dict[str, Any]], dict[tuple[str, str, str, str], dict[str, Any]]] | None = None


def _owner_of(known: list[tuple[str, str, str, bool]], owner: Optional[str],
              session: str) -> tuple[Optional[str], str, bool]:
    """The ONE (owner gateway, owner profile) for a replica card: the profile this chat's own unreleased ledger row
    names, else the only one any unreleased row names, else ``default`` (never several rows for one card). The third
    value says whether a ledger row named that profile; the ``default`` fallback is a label, not owner evidence."""
    if owner is not None:
        known = [k for k in known if k[0] == owner]
    for pick in ([k for k in known if k[2] == session and not k[3]], [k for k in known if not k[3]]):
        pairs = sorted({(k[0], k[1]) for k in pick})
        if len(pairs) == 1:
            return (*pairs[0], True)
    gateways = sorted({k[0] for k in known})
    return (owner if owner is not None else gateways[0] if len(gateways) == 1 else None), "default", False


def _ledger_fallback(board: str, gateway: str, profile: str, statuses: tuple[str, ...], *,
                     stale_ok: bool = False) -> list[dict[str, Any]]:
    """Cold start with an unreadable replica: the ledger's rows for that board, all stale (never "no cards")."""
    from hermes_cli.kanban_session_activity import _remote_links
    try:
        cards = _remote_links(stale_ok=True) if stale_ok else _remote_links()
    except Exception as exc:  # health: allow BLE001 -- log both-source failure, then preserve the failed-read contract
        _log.warning("kanban chat links: replica and ledger both unreadable for %s (%s)", board, exc)
        raise
    out = []
    for c in cards:
        if (c.get("board") != board or c.get("origin_gateway") not in (None, gateway)
                or c.get("origin_profile") not in (None, profile) or not c.get("session") or c.get("released")):
            continue
        if c.get("status") in statuses or unreleased_obligation(c):
            out.append({**c, "session_id": c["session"], "remote": True, "stale": True, "source": "ledger-fallback"})
    return out


PROFILE_BOARD_SOURCE = "ledger-profile-board"


def _is_profile_board_card(card: dict[str, Any]) -> bool:
    """A qualified ledger card whose owner profile is not ``default``: hermes-hub's qualified owner route keeps it
    in that profile's OWN kanban home (``<root>/profiles/<p>/kanban.db``, e.g. hub bb-tuner's ``t_d3e3f88a``). Board
    sync carries a gateway's shared kanban home only (``kanban_sync_client.board_paths``, ``kanban_db.kanban_home``),
    so the Board has no row for such a card and refuses its chat link (``not_found``)."""
    return card.get("ownership_state") == "qualified" and card.get("profile") not in (None, "", "default")


def _profile_board_cards(cards: list[dict[str, Any]], gateway: str, profile: str, statuses: tuple[str, ...],
                         replicas: list[tuple[str, Path]], read_ok: set[str],
                         ids: Optional[set[str]] = None) -> list[dict[str, Any]]:
    """[daveey] This gateway's chats' (``ids``, ``None`` = every chat) ledger obligations on a profile-own board
    (``_is_profile_board_card``) that no replica carries: the reader keeps them from the ledger instead of losing
    them. A card a readable replica DOES carry stays the replica's answer (its links, its release). ``cards``:
    ``_ledger_index``'s profile-board cards.

    - ``ids`` filters before the carry probe, so a lineage-scoped read never probes other chats' cards;
    - a reader that lists closed cards (``_lists_released``) also gets a released profile-board card as an explicit
      released row, as ``_released_links`` gives it for a released replica link;
    - ``owner_on_board`` (every-chat reads, ``ids`` ``None``, such as ``links_diff``): the replica DOES carry another
      ledger card of the same owner gateway and profile on that board, so that profile's cards reach the Board and
      this missing one is sync lag or loss, not a profile-own board. The reader still lists it (the union never
      hides a card); ``links_diff`` does not trust it."""
    from hermes_cli.kanban_session_activity import cached_ro
    closed = _lists_released(ids, statuses)
    wanted = [c for c in cards if c.get("session") and c.get("board") and c.get("id")
              and (ids is None or c["session"] in ids)
              and c.get("origin_gateway") in (None, gateway) and c.get("origin_profile") in (None, profile)
              and (_ledger_obligation(c, statuses) or (closed and _released_closed(c, statuses)))]
    if not wanted:
        return []
    paths = dict(replicas)
    carried: set[tuple[str, str]] = set()
    on_board: set[tuple[str, Any, Any]] = set()
    for board in {str(c["board"]) for c in wanted} & read_ok:
        mine = sorted({str(c["id"]) for c in wanted if c["board"] == board})
        try:
            with cached_ro(paths[board]) as conn:
                found = _present(conn, mine)
                carried |= {(board, i) for i in found}
                missing_owners = {(c.get("gateway"), c.get("profile")) for c in wanted
                                  if ids is None and c["board"] == board and str(c["id"]) not in found}
                for owner in missing_owners:  # every-chat reads only (links_diff): never other chats' cards
                    owned = {str(c["id"]) for c in cards if c.get("board") == board and c.get("id")
                             and (c.get("gateway"), c.get("profile")) == owner}
                    if owned & found or _present(conn, sorted(owned - set(mine)), first=True):
                        on_board.add((board, *owner))
        except sqlite3.Error:  # read a moment ago: treat as carried rather than guess
            carried |= {(board, i) for i in mine}
    out = []
    for c in wanted:
        if (str(c["board"]), str(c["id"])) in carried:
            continue
        row = {**c, "session_id": c["session"], "remote": True, "source": PROFILE_BOARD_SOURCE}
        if ids is None:
            row["owner_on_board"] = (str(c["board"]), c.get("gateway"), c.get("profile")) in on_board
        if c.get("released"):
            row.update(unfinished=False, released=True, release_reason=_released.REASON)
        out.append(row)
    return out


def _present(conn: sqlite3.Connection, ids: list[str], *, first: bool = False) -> set[str]:
    """The ``ids`` the replica has a task row for (``first``: stop at the first one), in bounded chunks."""
    found: set[str] = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        found |= {str(r[0]) for r in conn.execute(
            f"SELECT id FROM tasks WHERE id IN ({','.join('?' * len(chunk))}){' LIMIT 1' if first else ''}", chunk)}
        if first and found:
            break
    return found


def _lists_released(ids: Optional[set[str]], statuses: tuple[str, ...]) -> bool:
    """A lineage read that lists closed cards: the readers ``_released_links`` answers released links for."""
    return bool(ids) and bool(set(statuses) & set(TERMINAL_STATUSES))


def _released_closed(card: dict[str, Any], statuses: tuple[str, ...]) -> bool:
    """A released ledger row of a card closed in one of ``statuses`` (the ledger side of ``_released.read``)."""
    return bool(card.get("released")) and card.get("status") in TERMINAL_STATUSES and card.get("status") in statuses


def replica_cards(session_ids: Optional[Iterable[str]], gateway: str, statuses: tuple[str, ...] = OPEN_STATUSES,
                  replicas: Optional[list[tuple[str, Path]]] = None, *, now: Optional[float] = None,
                  ledger_fallback: bool = True, unreadable_out: Optional[list[str]] = None,
                  stale_ok: bool = False, origin_cards: Optional[list[dict[str, Any]]] = None) -> list[dict[str, Any]]:
    """This gateway's chats' (``session_ids``, one lineage; ``None`` = every chat) unreleased links to cards created
    on OTHER gateways, from every board's replica, shaped like the origin-ledger rows ``session_tasks`` appends.

    The sidebar counts read this too when ``links_reader`` is ``replica``, so both answers are one projection:
    - an unreleased link to a ``done``/``archived`` card, or to a card row the replica lacks, is an unfinished
      obligation (the ledger's done-without-verified-release); only a released link drops it. A ``done`` card reads
      ``done_pending_release`` (the owner releases the link once it has verified the completion), anything else
      ``unknown``;
    - only this origin profile's links (``chat_profile``) count;
    - ``stale``/``synced_at`` come from the replica's last successful pull;
    - an unreadable replica keeps its last rows, stale; on a cold start the ledger's rows for that board, stale.
      ``ledger_fallback=False`` (links_diff) skips both, so a diff never compares the ledger with itself;
      ``unreadable_out`` collects the boards whose replica could not be read;
    - one row per (chat, card); the key carries the owner profile the ledger names for it, else ``default``;
    - a card on a profile's own board (``_is_profile_board_card``), which the Board never carries, comes from the
      ledger (``source`` ``ledger-profile-board``) while no readable replica has its row, so the chat keeps it;
    - ``stale_ok`` (the Desktop linked-card strip): the owner labels come from the last ledger projection rather
      than waiting behind a rebuild of it. [daveey] Without it the strip's ``stale_ok`` stopped at this call and
      ``/api/session-kanban-tasks`` queued on ``_links_lock`` for every board write (3-7 s in a wake burst)."""
    from hermes_cli.profiles import get_active_profile_name
    ids = None if session_ids is None else set(str(s) for s in session_ids if s)
    if ids is not None and not ids:
        return []
    if replicas is None:
        from hermes_cli import kanban_sync_client as ksc
        replicas = [(slug, ksc.shadow_path(slug)) for slug, _path in ksc.board_paths()]
    profile = get_active_profile_name()
    now = time.time() if now is None else now
    stale_after = _stale_after_s()
    index: Optional[tuple[dict[tuple[str, str], list[tuple[str, str, str, bool]]], list[dict[str, Any]]]] = None
    out: dict[tuple[str, str], dict[str, Any]] = {}
    read_ok: set[str] = set()
    for board, path in replicas:
        memo_key = (str(path), gateway, profile)
        try:
            rows, pulled, runs = _read_replica(path, gateway, profile)
            _last_good[memo_key] = (rows, pulled, runs)
            read_ok.add(board)
            stale = pulled is None or now - pulled > stale_after
        except sqlite3.Error as exc:
            if unreadable_out is not None:
                unreadable_out.append(board)
            if not ledger_fallback:
                continue
            hit = _last_good.get(memo_key)
            _log.info("kanban chat links: replica %s unreadable (%s); %s", path, exc,
                      "keeping its last rows as stale" if hit else "falling back to the ledger, stale")
            if hit is None:
                for c in _ledger_fallback(board, gateway, profile, statuses, stale_ok=stale_ok):
                    if ids is None or c["session"] in ids:
                        out.setdefault((c["session"], c["key"]), c)
                continue
            rows, pulled, runs = hit
            stale = True
        if index is None and rows:
            index = _ledger_index(stale_ok) if origin_cards is None else _ledger_index(stale_ok, origin_cards)
        owners = index[0] if index is not None else None
        for r in rows:
            if ids is not None and r["chat_session"] not in ids:
                continue
            status = r["status"]
            missing = r["task_row"] is None
            unknown = missing or status in TERMINAL_STATUSES or status is None
            if not unknown and status not in statuses:
                continue
            tid = str(r["id"])
            owner, owner_profile, named = _owner_of((owners or {}).get((board, tid), []), r["created_on"],
                                                    r["chat_session"])
            if missing and owner == gateway:  # the ledger places the missing row on this gateway: a local card
                continue
            key = f"{owner or '?'}/{owner_profile}/{board}/{tid}"
            pair = (r["chat_session"], key)  # one row per chat and card: a card often has several chats
            if pair in out:
                continue
            out[pair] = {
                "title": r["title"], "status": (DONE_PENDING_RELEASE if not missing and status == "done" else UNKNOWN)
                if unknown else status, "assignee": r["assignee"],
                "created_at": r["created_at"], "started_at": r["started_at"], "priority": r["priority"],
                "ready_since": r.get("ready_since"),  # ms, when the Board last saw it become ready
                "block_kind": r["block_kind"], "human_request": _decoded_request(r["human_request"]), "id": tid,
                "board": board,
                "gateway": owner, "key": key, "session": r["chat_session"], "session_id": r["chat_session"],
                "via": "origin" if r["kind"] == "created" else "shepherd", "remote": True,
                "profile": owner_profile, "owner_profile": owner_profile, "origin_gateway": gateway,
                "origin_profile": r["chat_profile"],
                "ownership_state": "qualified" if owner is not None else "unknown", "unfinished": True,
                "released": False, "reported_status": status, "synced_at": pulled, "stale": stale,
                "source": "replica",
            }
            out[pair].update(upstream_fields(r))
            if named and owner is not None and not unknown and index is not None:
                out[pair].update(_ledger_wait(index, owner, owner_profile, board, tid, status))
            if machine := run_machine(r):
                out[pair]["run_machine"] = machine
            # Executor-stamped liveness of the current run (kanban_replica_execution), never the pull clock.
            if named and owner is not None and not unknown and (run := run_evidence(
                    {"status": status, **runs.get(tid, {})})) is not None:
                out[pair]["_replica_run"] = run
        # [daveey] A reader that lists closed cards keeps a released link's card as an explicit released row.
        for r in _released_links(path, board, gateway, profile, ids, statuses):
            tid = str(r["id"])
            if index is None:
                index = _ledger_index(stale_ok) if origin_cards is None else _ledger_index(stale_ok, origin_cards)
            owner, owner_profile, _named = _owner_of(index[0].get((board, tid), []), r["created_on"],
                                                     r["chat_session"])
            key = f"{owner or '?'}/{owner_profile}/{board}/{tid}"
            out.setdefault((r["chat_session"], key), _released.row(
                r, board=board, gateway=gateway, key=key, owner=owner, owner_profile=owner_profile, pulled=pulled,
                stale=stale))
    # One ledger read per call serves the owner labels and the profile-board cards.
    index = index if index is not None else _profile_board_index(stale_ok)
    _merge_profile_board(out, _profile_board_cards(index[1], gateway, profile, statuses, replicas, read_ok, ids), ids)
    return list(out.values())


def _profile_board_index(stale_ok: bool) -> tuple[dict[tuple[str, str], list[tuple[str, str, str, bool]]],
                                                  list[dict[str, Any]]]:
    """[daveey] ``_ledger_index`` for a ``replica_cards`` call with no replica rows, read only for its profile-board
    cards: while the last projection had none, take it without waiting on a rebuild (``stale_ok``; the rebuild runs
    in the background), so such a call no longer queues on the projection (review 5894115299 of PR #2372)."""
    return _ledger_index(stale_ok or (_owners_memo is not None and not _owners_memo[2]))


def _merge_profile_board(out: dict[tuple[str, str], dict[str, Any]], cards: list[dict[str, Any]],
                         ids: Optional[set[str]]) -> None:
    """Add each of this lineage's profile-board cards to ``replica_cards``'s answer unless it already has the card
    (same chat, owner gateway, board and id, whatever owner label the replica row got)."""
    have = {(c["session"], c["gateway"], c["board"], c["id"]) for c in out.values()}
    for c in cards:
        ref = (c["session"], c.get("gateway"), c["board"], c["id"])
        if (ids is None or c["session"] in ids) and ref not in have:
            have.add(ref)
            out[(c["session"], c["key"])] = c


def _released_links(path: Path, board: str, gateway: str, profile: str, ids: Optional[set[str]],
                    statuses: tuple[str, ...]) -> list[dict[str, Any]]:
    """``kanban_chat_links_released.read`` for one lineage; an unreadable replica adds nothing (logged)."""
    if not ids or not _lists_released(ids, statuses):
        return []
    try:
        return _released.read(path, gateway, profile, ids, statuses)
    except sqlite3.Error as exc:
        _log.info("kanban chat links: released links of %s unreadable (%s)", path, exc)
        return []


def _decoded_request(value: Any) -> Any:
    """[daveey] The replica stores ``human_request`` as JSON text; the ledger rows (and ``needs_human``) carry the dict.
    Undecoded, a card blocked on the user read as open work and its chat painted waiting instead of red. Malformed
    text is no evidence (None), the same verdict ``needs_human`` gives it."""
    if isinstance(value, (bytes, str)):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def _ledger_obligation(card: dict[str, Any], statuses: tuple[str, ...]) -> bool:
    """An unreleased ledger row the sidebar counts: open, or closed and unfinished (``unreleased_obligation``)."""
    if card.get("released"):
        return False
    return card.get("status") in statuses or unreleased_obligation(card)


def _unlinked_chats(gateway: str, profiles: dict[str, set[Any]]) -> set[str]:
    """The chats (``session -> origin profiles``) the link writer never links: a placeholder id, or a machine
    session of this gateway under any of its origin profiles (``_ledger_links`` skips them, ``reconcile``
    releases them), classified through :func:`_machine_by_profile` as ``_ledger_links`` does."""
    from hermes_cli.kanban_placement import is_placeholder_session
    out = {s for s in profiles if is_placeholder_session(s)}
    rows = [(name, s) for s, names in profiles.items() if s not in out for name in names]
    machine = _machine_by_profile(gateway, rows)
    return out | {str(s) for name, s in rows if str(s or "") in machine[(type(name), name)]}


def links_diff(gateway: str, statuses: tuple[str, ...] = OPEN_STATUSES, *, settle_s: float = 0.0,
               poll_s: Optional[float] = None) -> dict[str, Any]:
    """:func:`_links_snapshot`, re-read for up to ``settle_s`` so that only differences every read lists count
    (``kanban_chat_links_settle``: the two stores propagate asynchronously, so a card created a moment ago is in
    one only; such entries go to ``in_flight``). ``settle_s=0`` is one raw snapshot."""
    if settle_s <= 0:
        return _links_snapshot(gateway, statuses)
    from hermes_cli import kanban_chat_links_settle as settle
    return settle.settle(lambda: _links_snapshot(gateway, statuses), settle_s,
                         settle.default_poll_s() if poll_s is None else poll_s)


def _links_snapshot(gateway: str, statuses: tuple[str, ...] = OPEN_STATUSES) -> dict[str, Any]:
    """Per chat, the remote obligations the origin ledger lists against the ones the replicas list, including
    ``unknown`` unfinished rows (done cards without a verified release). Clean when the two answers match for
    every chat either side knows.

    [daveey] Chats the link writer never links (``_unlinked_chats``: machine sessions, placeholders) are left out
    of both sides and counted in ``machine_links``: their ledger rows were differences by construction, never a
    reader regression. On David-MBP they were 250 of the 270 only-in-ledger links (137 of 140 chats, all kanban
    worker transcripts, 2026-11-13), which hid the real difference among them.

    [daveey] A card on a profile's own board (``_is_profile_board_card``) has no Board row by design, so the replica
    side answers it from the ledger like the reader does and counts it in ``profile_board_links``: it is the
    reader's own answer, not a difference (hub bb-tuner ``t_d3e3f88a``, the last David-MBP row, 2026-11-13).
    ``profile_board`` lists those trusted rows per chat, like ``only_in_ledger``, so a reader of the report sees
    which ones it took on the ledger's word. One whose owner gateway and profile has OTHER cards on that board's
    replica (``owner_on_board``) is not trusted: that profile syncs through the shared board, so the missing row is
    a difference (only in the ledger), as it is for a ``default`` owner (review 5894115299 of PR #2372)."""
    from hermes_cli.kanban_session_activity import _remote_links
    ledger: dict[str, set[tuple[str, str, str]]] = {}
    profiles: dict[str, set[Any]] = {}
    for card in _remote_links():
        if card.get("origin_gateway") not in (None, gateway) or not card.get("session"):
            continue
        if not _ledger_obligation(card, statuses):
            continue
        ledger.setdefault(card["session"], set()).add((card["gateway"], card["board"], card["id"]))
        profiles.setdefault(card["session"], set()).add(card.get("origin_profile"))
    replica: dict[str, set[tuple[str, str, str]]] = {}
    profile_board: dict[str, set[tuple[str, str, str]]] = {}
    unreadable: list[str] = []
    from hermes_cli import kanban_sync_client as ksc
    replicas = [(slug, ksc.shadow_path(slug)) for slug, _path in ksc.board_paths()]
    for card in replica_cards(None, gateway, statuses, replicas, ledger_fallback=False, unreadable_out=unreadable):
        ref = (card["gateway"], card["board"], card["id"])
        if card.get("source") == PROFILE_BOARD_SOURCE:
            if card.get("owner_on_board"):  # that owner's cards reach the Board: a missing one is a difference
                continue
            profile_board.setdefault(card["session"], set()).add(ref)
        replica.setdefault(card["session"], set()).add(ref)
        profiles.setdefault(card["session"], set()).add(card.get("origin_profile"))
    skipped = _unlinked_chats(gateway, profiles)
    # Popped before ``ledger_boards`` on purpose (the writer never links these chats): a missing replica whose
    # only ledger cards are unlinked chats leaves the diff clean, and a session that is machine under any one of
    # its origin profiles is dropped as a whole.
    machine_links = sum(len(side.pop(s, ())) for side in (ledger, replica) for s in skipped)
    for s in skipped:
        profile_board.pop(s, None)
    # A replica that exists but cannot be read, or that is missing while the ledger has cards on its board, answers
    # nothing: the diff is never clean while one does (readers must not switch on it). A missing replica of a board
    # with no ledger cards has nothing to compare.
    exists = {slug for slug, path in replicas if path.exists()}
    ledger_boards = {b for cards in ledger.values() for _gw, b, _tid in cards}
    unreadable = sorted({b for b in unreadable if b in exists or b in ledger_boards})
    sessions = set(ledger) | set(replica)
    only_ledger = {s: sorted(ledger.get(s, set()) - replica.get(s, set())) for s in sessions}
    only_replica = {s: sorted(replica.get(s, set()) - ledger.get(s, set())) for s in sessions}
    only_ledger = {s: v for s, v in only_ledger.items() if v}
    only_replica = {s: v for s, v in only_replica.items() if v}
    # An unreadable replica answers nothing (see above).
    return {"clean": not only_ledger and not only_replica and not unreadable, "chats": len(sessions),
            "ledger_links": sum(len(v) for v in ledger.values()), "replica_links": sum(len(v) for v in replica.values()),
            "machine_links": machine_links, "profile_board_links": sum(len(v) for v in profile_board.values()),
            "profile_board": {s: sorted(v) for s, v in profile_board.items() if v},
            "only_in_ledger": only_ledger, "only_in_replica": only_replica,
            "unreadable": unreadable}
