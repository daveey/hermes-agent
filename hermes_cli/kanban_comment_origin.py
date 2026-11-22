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
import os
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


def synced_origin(author: Optional[str], writer: Optional[str]) -> Optional[str]:
    """``origin_gateway`` for a comment pulled from the Board: the author's gateway, else the writing gateway.

    Unlike :func:`resolve_origin` it never falls back to this gateway: a pulled row was written elsewhere, and an
    unknown sender stays NULL rather than impersonating the local gateway (t_01a1ccfca36ad3e3 slept through two
    worker-1 replies authored ``default`` because the pulled copies kept no sender at all)."""
    found = gateway_from_author(author)
    if found:
        return found
    name = str(writer or "").strip()
    return name if _GATEWAY_NAME.match(name) else None


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

def _from_card(author: Optional[str], card: str) -> bool:
    """A relayed card comment is authored ``<gateway>/<task id>`` (``worker-1/t_01a1...``); a bare id also counts."""
    a, c = (author or "").strip().lower(), card.strip().lower()
    return bool(c) and (a == c or a.endswith("/" + c))


def _writer_tasks(conn: sqlite3.Connection, task_id: str) -> dict:
    """``{comment key: writing card}`` from the ``commented`` events of ``task_id`` (``writer_task``, set when a
    worker comments on another card). Keys are the portable ``comment_uid`` and the local ``("id", n)``."""
    out: dict = {}
    try:
        rows = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'commented' "
                            "AND payload LIKE '%writer_task%'", (task_id,)).fetchall()
    except sqlite3.Error:
        return out
    for (raw,) in rows:
        try:
            p = json.loads(raw or "{}")
        except ValueError:
            continue
        wt = str(p.get("writer_task") or "").strip().lower()
        if not wt:
            continue
        # The integer id is the WRITER board's: it names a row here only when the event has no portable uid
        # (an unsynced board). A synced event is matched by uid alone, never by a coinciding local integer.
        if p.get("comment_uid"):
            out[str(p["comment_uid"])] = wt
        elif p.get("comment_id") is not None:
            try:
                out[("id", int(p["comment_id"]))] = wt
            except (TypeError, ValueError):
                continue
    return out


SYSTEM_AUTHOR_PREFIX = "kanban-"  # kernel writers: kanban-park, kanban-dispatcher, kanban-supervisor, ...
# Kernel writers that predate the prefix (kanban_host_reboot, placement, sweeps, detectors).
SYSTEM_AUTHORS = frozenset({"dispatcher", "placement", "worker-placement", "triage-sweep", "respawn-guard",
                            "quota-detector", "oom-detector", "dup-detector", "gateway"})


def is_system_author(author: Optional[str]) -> bool:
    name = (author or "").strip().lower()
    return name.startswith(SYSTEM_AUTHOR_PREFIX) or name in SYSTEM_AUTHORS


def gateway_spellings(gateway: Optional[str]) -> list[str]:
    """Every name of ``gateway``, lowercased, the given one first: a gateway renamed in place wrote its earlier
    comments under the old name (``origin_gateway`` ``mini``, author ``hub:gw:mini``) and writes the new one
    after, and a probe parked on either name must match both (``kanban.placement.aliases``, no network)."""
    name = (gateway or "").strip().lower()
    if not name:
        return []
    try:
        from hermes_cli import kanban_gateway_alias
        return kanban_gateway_alias.spellings(name) or [name]
    except ImportError:
        return [name]


def _names_gateway(spellings: list[str], origin: Optional[str], author: Optional[str]) -> bool:
    """Whether a comment's ``origin_gateway`` is one of ``spellings`` or its author contains one."""
    origin, author = (origin or "").lower(), (author or "").lower()
    return any(gw == origin or gw in author for gw in spellings)


def find_comment(conn: sqlite3.Connection, task_id: str, *, after_id: int = 0, from_gateway: Optional[str] = None,
                 contains: Optional[str] = None, from_card: Optional[str] = None,
                 skip_system: bool = False) -> Optional[dict]:
    """The first comment on ``task_id`` after ``after_id`` from ``from_gateway`` / ``from_card`` containing
    ``contains``. ``from_card``: written by that card's worker (the ``writer_task`` on its ``commented`` event)
    or authored ``<gateway>/<card>`` by the relay. ``skip_system``: ignore kernel writers (``kanban-*``)."""
    if from_card and from_card.strip():
        card, writers = from_card.strip().lower(), _writer_tasks(conn, task_id)
        try:
            rows = conn.execute("SELECT id, author, origin_gateway, body, uid FROM task_comments WHERE task_id = ? "
                                "AND id > ? ORDER BY id", (task_id, int(after_id or 0))).fetchall()
        except sqlite3.OperationalError:  # unsynced board: no uid column
            rows = [(*r, None) for r in conn.execute(
                "SELECT id, author, origin_gateway, body FROM task_comments WHERE task_id = ? AND id > ? "
                "ORDER BY id", (task_id, int(after_id or 0))).fetchall()]
        gws = gateway_spellings(from_gateway)
        for row in rows:
            writer = writers.get(str(row[4])) if row[4] else writers.get(("id", int(row[0])))
            if writer != card and not _from_card(row[1], card):
                continue
            if skip_system and is_system_author(row[1]):
                continue
            if gws and not _names_gateway(gws, row[2], row[1]):
                continue
            if contains and contains.lower() not in (row[3] or "").lower():
                continue
            return {"id": row[0], "author": row[1], "origin_gateway": row[2], "body": row[3]}
        return None
    if from_card is not None:
        return None  # an empty card id matches nothing
    sql = "SELECT id, author, origin_gateway, body FROM task_comments WHERE task_id = ? AND id > ?"
    params: list[Any] = [task_id, int(after_id or 0)]
    if skip_system:
        sql += (" AND substr(lower(trim(author)), 1, ?) != ? AND lower(trim(author)) NOT IN ("
                + ",".join("?" * len(SYSTEM_AUTHORS)) + ")")
        params += [len(SYSTEM_AUTHOR_PREFIX), SYSTEM_AUTHOR_PREFIX, *sorted(SYSTEM_AUTHORS)]
    gws = gateway_spellings(from_gateway)
    if gws:
        sql += " AND (" + " OR ".join(
            ["lower(coalesce(origin_gateway, '')) = ? OR instr(lower(author), ?) > 0"] * len(gws)) + ")"
        params += [x for gw in gws for x in (gw, gw)]
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
                               contains=args.contains, from_card=getattr(args, "from_card", None),
                               skip_system=bool(getattr(args, "skip_system", False)))
    except sqlite3.Error as exc:
        print(f"error: {exc}")
        return 2
    if hit is None:
        print(f"not yet: no comment on {args.task_id} after {args.after_id}"
              + (f" from {args.from_gateway}" if args.from_gateway else "")
              + (f" from card {args.from_card}" if getattr(args, "from_card", None) else "")
              + (f" containing {args.contains!r}" if args.contains else ""))
        return 1
    body = " ".join(str(hit["body"]).split())
    print(f"comment {hit['id']} by {hit['author']} (gateway {hit['origin_gateway'] or '?'}): {body[:300]}")
    return 0


# --- comment-wake note ------------------------------------------------------------------------

def _wait_comment_clauses(cmd: str, task_id: str) -> Optional[list[dict]]:
    """The filters of a wake check that is ONLY ``hermes kanban wait-comment <task_id> ...`` alternatives joined
    by ``||``; None for anything else (``&&``, ``;``, a pipe, a subshell, ``!``, another command, another card,
    an unparsable quote): a probe this function cannot fully read is never judged."""
    import shlex
    if any(ch in (cmd or "") for ch in "$`#*?~{}\\\n"):
        return None  # expansion, globbing, a comment or an escape: bash and this parser may read it differently
    try:
        lex = shlex.shlex(cmd or "", posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = ""
        tokens = list(lex)
    except ValueError:
        return None
    clauses: list[list[str]] = [[]]
    for tok in tokens:
        if tok == "||":
            clauses.append([])
        elif tok and set(tok) <= set("();<>|&"):
            return None
        else:
            clauses[-1].append(tok)
    out: list[dict] = []
    for words in clauses:
        if words[:3] != ["hermes", "kanban", "wait-comment"]:
            return None
        clause: dict = {"contains": None, "from_gateway": None, "from_card": None, "task": None}
        rest, i = words[3:], 0
        while i < len(rest):
            w = rest[i]
            name, eq, val = w.partition("=")
            key = {"--contains": "contains", "--from-gateway": "from_gateway", "--from-card": "from_card",
                   "--after-id": "after_id"}.get(name)
            if key is None:
                if w.startswith("-") or clause["task"] is not None:
                    return None
                clause["task"] = w
            elif eq:
                clause[key] = val
            elif i + 1 < len(rest):
                clause[key] = rest[i + 1]
                i += 1
            else:
                return None
            i += 1
        if clause["task"] != task_id:
            return None
        out.append(clause)
    return out or None


def comment_wake_note(conn: sqlite3.Connection, task_id: str, author: str, body: str,
                      origin_gateway: Optional[str] = None) -> Optional[str]:
    """What to tell the sender of a comment on a card parked on ``wait-comment``: nothing when the comment
    satisfies one alternative, else the filters it missed. [daveey]

    Regression for t_01a194b9de51e620 (2026-11-18): a source card's "bounded SOURCE GO REQUEST" sat five hours
    because the owner's wake check wanted the phrase ``BOUNDED exact-head SOURCE GO REQUEST``; the sender could
    not see that its line would not wake the reader. Judged only when the card is ``scheduled`` and its whole
    probe is wait-comment alternatives on this card (:func:`_wait_comment_clauses`); the after-id is the
    reader's, and a new comment is always after it. Never raises: the comment is already written."""
    try:
        row = conn.execute(
            "SELECT w.cmd FROM task_wake_checks w JOIN tasks t ON t.id = w.task_id "
            "WHERE w.task_id = ? AND w.purpose = 'wake' AND t.status = 'scheduled'", (task_id,)).fetchone()
        if row is None or not row[0]:
            return None
        clauses = _wait_comment_clauses(str(row[0]), task_id)
        if not clauses:
            return None
        try:
            gw = (resolve_origin(author, origin_gateway) or "").lower()   # what add_comment stored
        except ValueError:
            gw = ""
        low, a = (body or "").lower(), (author or "").lower()
        own = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()            # add_comment's writer_task
        own = own if re.fullmatch(r"t_[0-9a-f]{6,}", own) else ""
        missed: list[str] = []
        for c in clauses:
            why: list[str] = []
            if c["contains"] and c["contains"].lower() not in low:
                why.append(f"the text {c['contains']!r}")
            if c["from_card"] is not None:
                card = c["from_card"].strip().lower()
                if not card or (own != card and not _from_card(author, card)):
                    why.append(f"a comment from card {c['from_card']}")
            if c["from_gateway"]:
                if not _names_gateway(gateway_spellings(c["from_gateway"]), gw, a):
                    why.append(f"a comment from gateway {c['from_gateway']}")
            if not why:
                return None
            missed.append(" and ".join(why))
    except (sqlite3.Error, TypeError, ValueError, AttributeError):
        return None
    return ("This comment will NOT wake the card: it is parked on wait-comment and needs "
            + "; or ".join(missed) + ". It stays parked until its backstop unless a comment matches.")


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
