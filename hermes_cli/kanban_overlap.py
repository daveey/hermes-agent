"""Refuse a second owner for work an open card already does (kanban_create). [daveey]

A chat counted owners by its OWN chat links, found none, and created "Stage B" on the default board while
Stage B was already running on board ``unconventional-collab`` from another chat. Before creating, this scans
open cards on every board of this gateway (live board DBs) and every other gateway (the hermes-hub replica,
``hub.db``), so the answer no longer depends on which chat asks or which board the card lands on.

An open card matches when:
- the new card names its board, is not being created on that board, and the titles share a word, or
- both titles share at least half of the longer title's significant words (3 or more).

Naming a board means an explicit reference (``/<slug>``, ``board <slug>``, ``<slug> board``) in the
title or body, or the slug itself in the title when the slug is not a single plain word
(``unconventional-collab``, not ``runners`` or ``hermes``). A bare plain word that equals a board
slug does not count.

Never matched: closed cards, the creator's own card, the new card's parents, cards the same chat or the same
worker created (fan-out), the waiting card a capability-owner create quotes, and another capability-owner
card for that same waiting card. A missing or unreadable replica is skipped and logged, never a block.
``kanban.create_overlap_check: false`` turns the check off.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Callable, Iterable, Optional

_log = logging.getLogger(__name__)

EVENT = "create_overlap"
OPEN = ("todo", "ready", "running", "scheduled", "blocked", "review", "triage")
MAX_SHOWN = 8
MIN_SHARED = 3
_STOP = frozenset("""
a an and are as at be by for from in into is it its of on or the then this that to via with without after before
when not no all new card task fix add run make use using update ensure follow up followup self heal
""".split())


def enabled() -> bool:
    from hermes_cli.config import cfg_get, load_config
    return bool(cfg_get(load_config(), "kanban", "create_overlap_check", default=True))


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 2 and w not in _STOP}


def _plain_word_slug(slug: str) -> bool:
    """True when the slug is one alphabetic word (``runners``, ``hermes``, ``bb``).

    A hyphen, underscore or digit (``unconventional-collab``) is specific enough that the
    slug itself in the title still names the board. A plain word is an ordinary word.
    """
    return bool(re.fullmatch(r"[a-z]+", (slug or "").lower()))


def _explicit_board_ref(slug: str, text: str) -> bool:
    """``/<slug>`` (``hub/runners``, ``Metta-AI/<slug>``), ``board <slug>`` or ``<slug> board``."""
    s = re.escape((slug or "").lower())
    if not s:
        return False
    return bool(re.search(
        rf"(?:/|\bboard[\s:=`'\\\"]+){s}(?![a-z0-9_-])"
        rf"|(?<![a-z0-9_-]){s}(?![a-z0-9_-])[\s:=`'\\\"]+board\b",
        (text or "").lower(),
    ))


def _names_board(slug: str, title: str, body: str) -> bool:
    if not slug or slug == "default":
        return False
    if _explicit_board_ref(slug, title) or _explicit_board_ref(slug, body):
        return True
    # A bare slug token in the title names the board only when it is not a plain word.
    # "runners" in "bb-linux runners can't run jobs" is not a board reference.
    if _plain_word_slug(slug):
        return False
    s = re.escape(slug.lower())
    return bool(re.search(rf"(?<![a-z0-9_-]){s}(?![a-z0-9_-])", (title or "").lower()))


def _reasons(card: dict, new_words: set[str], title: str, body: str, target_board: str) -> tuple[list[str], float]:
    reasons, score = [], 0.0
    theirs = words(card.get("title") or "")
    shared = new_words & theirs
    # A board named in passing (a path, a log line) is not enough: the titles must share a word too.
    if (card["board"] != target_board and shared - words(card["board"])
            and _names_board(card["board"], title, body)):
        reasons.append(f"the new card names board {card['board']!r}")
    if new_words and theirs:
        score = len(shared) / max(len(new_words), len(theirs))
        if len(shared) >= MIN_SHARED and score >= 0.5:
            reasons.append(f"titles share {len(shared)} words ({', '.join(sorted(shared))})")
    return reasons, score


def _local_cards(self_gw: Optional[str]) -> Iterable[dict]:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_boards import list_boards
    from hermes_cli.kanban_session_activity import cached_ro
    marks = ",".join("?" * len(OPEN))
    for meta in list_boards(include_archived=False):
        slug = meta.get("slug") or kb.DEFAULT_BOARD
        path = kb.kanban_db_path(board=slug)
        if not path.exists():
            continue
        try:
            with cached_ro(path) as c:
                rows = c.execute(
                    "SELECT t.id, t.title, t.status, t.session_id, (SELECT json_extract(e.payload, "
                    "'$.creator_task_id') FROM task_events e WHERE e.task_id = t.id AND e.kind = 'created' "
                    f"LIMIT 1), t.idempotency_key FROM tasks t WHERE t.status IN ({marks})", OPEN).fetchall()
                try:
                    links = c.execute("SELECT task_id, chat_session FROM task_chat_links WHERE released = 0"
                                      ).fetchall()
                except sqlite3.OperationalError:
                    links = []
        except sqlite3.Error as exc:
            _log.warning("kanban create overlap: board %s unreadable, skipped (%s)", slug, exc)
            continue
        chats: dict[str, set[str]] = {}
        for tid, sess in links:
            chats.setdefault(tid, set()).add(sess)
        for tid, title, status, sess, creator, key in rows:
            yield {"id": tid, "board": slug, "gateway": self_gw or "local", "status": status, "title": title,
                   "session_id": sess, "creator_task_id": creator, "local": True, "key_family": key_family(key),
                   "chats": sorted(chats.get(tid, set()) | ({sess} if sess else set()))}


def _replica_cards(self_gw: Optional[str]) -> Iterable[dict]:
    from hermes_cli import kanban_capability_registry as reg
    from hermes_cli.kanban_session_activity import cached_ro
    path = reg.hub_db_path()
    if not path.exists():
        _log.info("kanban create overlap: no hermes-hub replica at %s; other gateways not checked", path)
        return
    marks = ",".join("?" * len(OPEN))
    try:
        with cached_ro(path) as c:
            rows = c.execute(f"SELECT gw, data FROM replica WHERE coll = 'tasks' AND gw != ? "
                             f"AND json_extract(data, '$.status') IN ({marks})", (self_gw or "", *OPEN)).fetchall()
    except sqlite3.Error as exc:
        _log.warning("kanban create overlap: replica %s unreadable, other gateways skipped (%s)", path, exc)
        return
    seen = set()
    for gw, raw in rows:
        try:
            t = json.loads(raw)
        except ValueError:
            continue
        key = (gw, t.get("board"), t.get("id"))
        if not t.get("id") or key in seen:
            continue
        seen.add(key)
        sessions = [s for s in (t.get("sessions") or []) if isinstance(s, dict)]
        yield {"id": t["id"], "board": t.get("board") or "default", "gateway": t.get("gateway") or gw,
               "status": t.get("status"), "title": t.get("title") or "", "local": False,
               "session_id": next((s.get("session") for s in sessions if s.get("rel") == "created"), None),
               "creator_task_id": None, "key_family": None,
               "chats": sorted({str(s["session"]) for s in sessions if s.get("session")})}


def key_family(key: Optional[str]) -> Optional[str]:
    """An idempotency key's family: the text before the first ':' (``coach-fix:<slug>`` -> ``coach-fix``), else
    the whole key. One automated creator (coach / self-heal cron) keys all its cards in one family."""
    key = (key or "").strip()
    return key.split(":", 1)[0] if key else None


_OWNER_TITLE_RE = re.compile(
    r"^Capability owner: \S+ for [A-Za-z0-9._-]+/(t_[0-9a-f]{8,16}) \(")
_OWNER_BODY_RE = re.compile(
    r"^Capability owner card for waiting card \S+/(t_[0-9a-f]{8,16})\b")


def capability_owner_waiting_id(title: str, body: str) -> Optional[str]:
    """The waiting card a capability-owner create quotes, when the title and body name the same id.

    The owner title embeds that card's title, so a word-overlap check matches the card the owner
    exists to serve (hub t_01a18669308f507f). Both shapes must agree, so a title that merely starts
    with the prefix does not skip anything.
    """
    title_m = _OWNER_TITLE_RE.match(title or "")
    body_m = _OWNER_BODY_RE.match(body or "")
    if title_m and body_m and title_m.group(1) == body_m.group(1):
        return title_m.group(1)
    return None


def _sibling_owner(card_title: str, waiting_id: str) -> bool:
    found = _OWNER_TITLE_RE.match(card_title or "")
    return bool(found and found.group(1) == waiting_id)


def find(*, title: str, body: str, target_board: str, session_id: Optional[str], self_tid: Optional[str],
         parents: Iterable[str], family: Optional[str] = None) -> list[dict]:
    """Open cards elsewhere that already cover this work, best match first. Never raises. ``family`` (the new
    card's idempotency-key family) skips cards from the same keyed creator: its keys already dedupe them."""
    try:
        from hermes_cli import kanban_capability_registry as reg
        self_gw = reg.self_gateway()
        skip = set(parents) | ({self_tid} if self_tid else set())
        waiting = capability_owner_waiting_id(title, body)
        new_words = words(title)
        out = []
        for source in (_local_cards(self_gw), _replica_cards(self_gw)):
            for card in source:
                if (card["id"] in skip or card["id"] == waiting
                        or (waiting and _sibling_owner(card.get("title") or "", waiting))
                        or (session_id and card["session_id"] == session_id)
                        or (self_tid and card["creator_task_id"] == self_tid)
                        or (family and card.get("key_family") == family)):
                    continue
                reasons, score = _reasons(card, new_words, title, body, target_board)
                if reasons:
                    out.append({**{k: card[k] for k in ("id", "board", "gateway", "status", "title", "chats")},
                                "why": "; ".join(reasons), "_score": score, "_local": card["local"]})
        out.sort(key=lambda m: (-m["_score"], m["status"] not in ("running", "ready", "scheduled"), m["id"]))
        return out
    except Exception:  # health: allow BLE001 -- a broken check must never block creating a card
        _log.warning("kanban create overlap check failed; creating without it", exc_info=True)
        return []


def public(matches: list[dict]) -> list[dict]:
    return [{k: v for k, v in m.items() if not k.startswith("_")} for m in matches[:MAX_SHOWN]]


def _retry_ask(entry: str, uncovered: list[str]) -> str:
    if entry == "cli":
        flags = " ".join(f"--distinct-from {i}" for i in uncovered)
        return f"run `hermes kanban create` again with {flags} --distinct-reason '<why it is separate>'"
    if entry == "rest":
        return (f"POST again with \"distinct_from\": {json.dumps(uncovered)} and "
                "\"distinct_reason\": \"<why it is separate>\"")
    return f"call kanban_create again with distinct_from={uncovered} and distinct_reason='<why it is separate>'"


def refusal(matches: list[dict], distinct_from: list[str], reason: str, entry: str = "tool") -> dict:
    shown = public(matches)
    uncovered = [m["id"] for m in shown if m["id"] not in distinct_from]
    ask = ("pass distinct_reason (why this is separate work) with distinct_from" if distinct_from and not reason
           else _retry_ask(entry, uncovered))
    named = ", ".join(f"{m['id']} on {m['gateway']}/{m['board']} ({m['status']})" for m in shown[:3])
    note = (f"No card was created: {len(matches)} open card(s) already look like this work ({named}). "
            "Count owners by board/project, not by your chat's links: continue the existing card "
            f"(read it, comment on it, or message its chat), or if this really is separate work {ask}.")
    return {"ok": False, "created": False, "code": EVENT, "matches": shown, "total_matches": len(matches),
            "note": note}


def covered(matches: list[dict], distinct_from: list[str], reason: str) -> bool:
    return bool(reason.strip()) and all(m["id"] in distinct_from for m in matches[:MAX_SHOWN])


def record(conn: Optional[sqlite3.Connection], task_id: Optional[str], *, outcome: str, title: str,
           matches: list[dict], reason: str = "", session_id: Optional[str] = None) -> None:
    """``create_overlap`` event (the detection signal): on the new card for an override; for a refusal on the
    creator's own card, else on the best LOCAL match's board. Never raises."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    payload = {"outcome": outcome, "title": title, "reason": reason or None, "session_id": session_id,
               "matches": [{"id": m["id"], "board": m["board"], "gateway": m["gateway"], "status": m["status"]}
                           for m in public(matches)]}
    _log.info("kanban create overlap: %s %r (%d matches)", outcome, title, len(matches))
    try:
        if conn is not None and task_id:
            with kb.write_txn(conn):
                kb._append_event(conn, task_id, EVENT, payload)
            return
        local = next((m for m in matches if m["_local"]), None)
        if local is None:
            return
        with kbc.connect_closing(board=local["board"]) as other, kb.write_txn(other):
            kb._append_event(other, local["id"], EVENT, payload)
    except Exception:  # health: allow BLE001 -- the event is telemetry; the refusal itself already happened
        _log.warning("kanban create overlap: could not record the %s event", outcome, exc_info=True)


def _no_override(conn: sqlite3.Connection, task_id: str) -> None:
    return None


def check(conn: sqlite3.Connection, args: dict, *, courier: bool, title: str, session_id: Optional[str],
          self_tid: Optional[str], parents: list[str], entry: str = "tool",
          ) -> tuple[Optional[str], Callable[[sqlite3.Connection, str], None]]:
    """``(refusal_json, _)`` stops the create; otherwise ``(None, after_create)``, where ``after_create(conn,
    new_task_id)`` records an accepted ``distinct_from`` override on the new card (a no-op when nothing
    overlapped). A courier-applied card (``courier``) was already checked on the gateway that asked for it.
    ``entry`` (tool | cli | rest) only words the refusal's retry hint for the caller's entry path."""
    from hermes_cli import kanban_db as kb
    if courier or not enabled():
        return None, _no_override
    key = args.get("idempotency_key")
    if key and conn.execute("SELECT 1 FROM tasks WHERE idempotency_key = ? AND status != 'archived'",
                            (key,)).fetchone():
        return None, _no_override  # a retry returns the existing card; nothing new is created
    distinct = [str(x) for x in (args.get("distinct_from") or [])]
    reason = str(args.get("distinct_reason") or "")
    matches = find(title=title, body=str(args.get("body") or ""), target_board=args.get("board") or kb.DEFAULT_BOARD,
                   session_id=session_id, self_tid=self_tid, parents=parents, family=key_family(key))
    if not matches:
        return None, _no_override
    if covered(matches, distinct, reason):
        return None, lambda c, tid: record(c, tid, outcome="override", title=title, matches=matches,
                                           reason=reason, session_id=session_id)
    own = bool(self_tid) and not args.get("board")  # the creator's card is on the board this call opened
    record(conn if own else None, self_tid, outcome="refused", title=title, matches=matches,
           session_id=session_id)
    return json.dumps(refusal(matches, distinct, reason, entry)), _no_override
