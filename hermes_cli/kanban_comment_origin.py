"""Which gateway a Kanban comment came from, and waits that use it instead of the author. [daveey]

A comment's ``author`` is free text: the hermes-hub relay writes ``hub:gw:<gateway>``, other paths pass
``--author macbook``, ``macbook-chat``, ``macbook/t_...`` or even a card title. A wake check that counts
comments ``WHERE author = 'macbook'`` therefore never sees the relay's ``hub:gw:macbook`` reply: on
2026-10-27 t_01a17f81686694ec stayed parked 38 h after the Mac answered (self-heal desktop-00c4a3bda2).

* ``task_comments.origin_gateway`` (nullable, machine-local): the relay's sender gateway, else the gateway
  an author spelling names (``hub:gw:<x>``, ``<x>/t_...``), else this gateway's own name;
* ``hermes kanban wait-comment <task> --after-id N [--from-gateway X] [--contains TEXT]``: the wake probe
  for "a reply arrived": exit 0 on a match, 1 not yet, 2 cannot tell. A gateway matches on
  ``origin_gateway`` or on any author spelling containing it;
* ``author_probe``: the park guard refuses a wake check that filters ``task_comments`` by author
  (``=``, ``like``, ``glob``, ``in``; ``!=`` to skip one's own notes is fine) and names the wait-comment
  command to use. Off switch: ``kanban.author_probe_guard: false``;
* ``answered_request``: the detector behind ``wake_probe_stale``: a parked card whose reason or probe
  names a hermes-hub request whose owner card finished, while its probe still says "not yet".
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any, Optional

logger = logging.getLogger(__name__)

AUTHOR_PROBE = "author_probe"
WAKE_PROBE_STALE_EVENT = "wake_probe_stale"

_RELAY_AUTHOR = re.compile(r"^hub:gw:([A-Za-z0-9][A-Za-z0-9._-]{0,63})$")
_CARD_AUTHOR = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]{0,63})/t_[0-9a-f]{6,}$")
_GATEWAY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def gateway_from_author(author: Optional[str]) -> Optional[str]:
    """The gateway an author spelling names for certain (relay sender, ``<gw>/t_...``), else None."""
    text = (author or "").strip()
    m = _RELAY_AUTHOR.match(text) or _CARD_AUTHOR.match(text)
    return m.group(1) if m else None


def _local_gateway() -> Optional[str]:
    try:
        from hermes_cli.kanban_origin import gateway_names
        me = gateway_names()[0]
    except (ImportError, OSError, sqlite3.Error, ValueError):
        logger.debug("origin_gateway: this gateway's name is unknown", exc_info=True)
        return None
    return me if me and _GATEWAY_NAME.match(me) else None


def resolve_origin(author: Optional[str], explicit: Optional[str] = None) -> Optional[str]:
    """``origin_gateway`` for a new comment: explicit (the relay's sender), the author's gateway, else this one."""
    if explicit and str(explicit).strip():
        name = str(explicit).strip()
        if not _GATEWAY_NAME.match(name):
            raise ValueError(f"origin gateway {name!r} is not a gateway name")
        return name
    return gateway_from_author(author) or _local_gateway()


def ensure_origin_column(conn: sqlite3.Connection) -> None:
    """Migration: add ``task_comments.origin_gateway`` to an older board and back-fill it once."""
    from hermes_cli.sqlite_util import add_column_if_missing
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_comments'").fetchone() is None:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(task_comments)")}
    if "origin_gateway" not in cols and add_column_if_missing(conn, "task_comments", "origin_gateway",
                                                              "origin_gateway TEXT"):
        backfill(conn)


def backfill(conn: sqlite3.Connection) -> int:
    """Set ``origin_gateway`` on old rows whose author names a gateway for certain. Idempotent."""
    n = 0
    rows = conn.execute("SELECT id, author FROM task_comments WHERE origin_gateway IS NULL "
                        "AND (author LIKE 'hub:gw:%' OR author LIKE '%/t\\_%' ESCAPE '\\')").fetchall()
    for cid, author in rows:
        gw = gateway_from_author(author)
        if gw:
            conn.execute("UPDATE task_comments SET origin_gateway = ? WHERE id = ?", (gw, cid))
            n += 1
    return n


# --- wait-comment -----------------------------------------------------------------------------

def find_comment(conn: sqlite3.Connection, task_id: str, *, after_id: int = 0, from_gateway: Optional[str] = None,
                 contains: Optional[str] = None) -> Optional[dict]:
    """The first comment on ``task_id`` after ``after_id`` from ``from_gateway`` containing ``contains``."""
    sql = "SELECT id, author, origin_gateway, body FROM task_comments WHERE task_id = ? AND id > ?"
    params: list[Any] = [task_id, int(after_id or 0)]
    if from_gateway:
        gw = from_gateway.strip().lower()
        sql += " AND (lower(coalesce(origin_gateway, '')) = ? OR instr(lower(author), ?) > 0)"
        params += [gw, gw]
    if contains:
        sql += " AND instr(lower(body), ?) > 0"
        params.append(contains.lower())
    row = conn.execute(sql + " ORDER BY id LIMIT 1", params).fetchone()
    if row is None:
        return None
    return {"id": row[0], "author": row[1], "origin_gateway": row[2], "body": row[3]}


def run_wait_comment(args) -> int:
    """CLI ``hermes kanban wait-comment``: 0 a matching comment exists, 1 not yet, 2 cannot tell."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    try:
        with kbc.connect_closing() as conn:
            if kb.get_task(conn, args.task_id) is None:
                print(f"error: no task {args.task_id}")
                return 2
            hit = find_comment(conn, args.task_id, after_id=args.after_id, from_gateway=args.from_gateway,
                               contains=args.contains)
    except sqlite3.Error as exc:
        print(f"error: {exc}")
        return 2
    if hit is None:
        print(f"not yet: no comment on {args.task_id} after {args.after_id}"
              + (f" from {args.from_gateway}" if args.from_gateway else "")
              + (f" containing {args.contains!r}" if args.contains else ""))
        return 1
    body = " ".join(str(hit["body"]).split())
    print(f"comment {hit['id']} by {hit['author']} (gateway {hit['origin_gateway'] or '?'}): {body[:300]}")
    return 0


# --- park guard -------------------------------------------------------------------------------

_COMMENTS = re.compile(r"\btask_comments\b", re.IGNORECASE)
_AUTHOR_FILTER = re.compile(
    r"\bauthor\b\s*(?:==?(?!=)|\bnot\s+like\b|\blike\b|\bglob\b|\bin\s*\(|\bis\b(?!\s+not))", re.IGNORECASE)
_TASK_ID = re.compile(r"\bt_[0-9a-f]{6,}\b")


def author_probe(wake_check: Optional[str]) -> bool:
    """True when the probe reads ``task_comments`` and filters on who wrote them by name."""
    if not isinstance(wake_check, str) or not _COMMENTS.search(wake_check):
        return False
    return bool(_AUTHOR_FILTER.search(wake_check))


def _author_probe_guard_enabled() -> bool:
    try:
        from hermes_cli.config import load_config_readonly
        cfg = (load_config_readonly() or {}).get("kanban", {}) or {}
        return cfg.get("author_probe_guard", True) is not False
    except Exception:  # an unreadable config keeps the guard on
        logger.debug("kanban.author_probe_guard: config unreadable, guard stays on", exc_info=True)
        return True


def author_probe_error(task_id: str, wake_check: str, conn: Optional[sqlite3.Connection] = None):
    from hermes_cli.kanban_awaiting import AwaitingError
    after = 0
    if conn is not None:
        try:
            row = conn.execute("SELECT max(id) FROM task_comments WHERE task_id = ?", (task_id,)).fetchone()
            after = int(row[0] or 0) if row else 0
        except sqlite3.Error:
            after = 0
    target = task_id
    m = _TASK_ID.search(wake_check or "")
    if m:
        target = m.group(0)
    return AwaitingError(AUTHOR_PROBE, (
        "this wake_check filters task_comments by author. Authors are free text: a reply relayed through "
        "hermes-hub arrives as 'hub:gw:<gateway>' (others as '<gateway>', '<gateway>-chat', '<gateway>/t_...'), "
        "so an author match can miss the reply forever (t_01a17f81686694ec slept 38 h that way). Wait on the "
        f"sending gateway instead: `hermes kanban wait-comment {target} --after-id {after} --from-gateway "
        "<gateway>` (add --contains '<text>' to require a marker). It matches the comment's origin_gateway or "
        "any author spelling of that gateway."))


def check_park(conn: Optional[sqlite3.Connection], task_id: str, wake_check: Optional[str]) -> None:
    """Raise ``AwaitingError(author_probe)`` for an author-filtering comment probe."""
    if wake_check and author_probe(wake_check) and _author_probe_guard_enabled():
        raise author_probe_error(task_id, wake_check, conn)


# --- wake_probe_stale -------------------------------------------------------------------------

_REQUEST_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{4,95}")


def _park_text(conn: sqlite3.Connection, task_id: str) -> str:
    row = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind IN ('scheduled','continued') "
                       "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    try:
        p = json.loads(row[0] or "{}") if row else {}
    except ValueError:
        p = {}
    if not isinstance(p, dict):
        return ""
    raw = p.get("awaiting")
    awaiting: dict = raw if isinstance(raw, dict) else {}
    return " ".join(str(x) for x in (p.get("reason"), p.get("checkpoint"), awaiting.get("ref"),
                                     awaiting.get("why")) if x)


def answered_request(conn: sqlite3.Connection, task_id: str, cmd: str) -> Optional[dict]:
    """A hermes-hub request this park names (reason or probe) whose owner card is done, or None.

    Read-only on ``hub.db``; any error means "cannot tell" (None). A request already reported by a
    ``wake_probe_stale`` event on this card is skipped, so each answered request wakes the card once."""
    if "capability-owner" in (cmd or ""):
        return None  # that probe already reads the same request state
    text = f"{_park_text(conn, task_id)} {cmd or ''}"
    tokens = {t for t in _REQUEST_TOKEN.findall(text) if "-" in t and not t.startswith("t_")}
    if not tokens:
        return None
    try:
        from hermes_cli import kanban_capability_registry as reg
        from hermes_cli.kanban_capability_routing import owner_status
        path = reg.hub_db_path()
        if path is None or not path.is_file():
            return None
        db = reg._ro(path)
        try:
            ph = ",".join("?" * len(tokens))
            ids = [r[0] for r in db.execute(f"SELECT id FROM sent WHERE id IN ({ph}) AND state = 'done'",
                                            sorted(tokens))]
        finally:
            db.close()
    except (ImportError, OSError, sqlite3.Error):
        logger.debug("wake_probe_stale: hermes-hub replica unreadable", exc_info=True)
        return None
    if not ids:
        return None
    seen = set()
    for (payload,) in conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
                                   (task_id, WAKE_PROBE_STALE_EVENT)):
        try:
            seen.add(json.loads(payload or "{}").get("request"))
        except (ValueError, AttributeError):
            continue
    for rid in sorted(ids):
        if rid in seen:
            continue
        rc, msg = owner_status(rid)  # never raises: "cannot tell" is rc 2
        if rc == 0:
            return {"request": rid, "owner": msg}
    return None


def stale_note(hit: dict, cmd: str) -> str:
    return (f"wake_probe_stale: hermes-hub request {hit['request']} that this park waits on is answered "
            f"({hit['owner']}), but the wake_check still exits 1 (not yet). Woken once so the reply is not "
            f"missed. Read the reply (comments, the owner card), then finish or re-park with a probe that "
            f"matches it, e.g. `hermes kanban wait-comment <task> --after-id <last id> --from-gateway <gw>`. "
            f"Probe was: {cmd[:300]}")
