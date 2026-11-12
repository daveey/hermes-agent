"""Name the host in the update-pause alarm and link sibling hosts' alarms for one refusal. [daveey]

The alarm key (:func:`alarm_key`) was ``sha256(install_root + unit)``. Every Board worker has the same
install root and gateway unit, so every worker produced one key: once a board-wide lookup or a replica
shows another worker's card, this host's alarm would be folded into that card and its own failure would
have no owner. The key now names the host as well.

The other half: on 2026-11-12 one fleet-wide cause (the gateway running as a child of a resident quota
supervisor) made worker-5, worker-1 and worker-6 each file an alarm. None could see the others, so three
owners investigated from scratch and two opened competing PRs (#1554, #1564). Before filing,
:func:`find_sibling` now looks board-wide (the live file, then the Board replica) for an open alarm for
the same unit and refusal class on another host. When one exists the new card names it as the likely
shared cause (:func:`sibling_section`) and the sibling hears about this host: directly when its card is
writable here (:func:`notify`), else from its own home on its next deferral (:func:`mirror_followers`),
because a card another gateway created can only be written there.
"""
from __future__ import annotations

import hashlib
import logging
import re
import socket
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Iterator, Optional

log = logging.getLogger(__name__)

AUTHOR = "update-pause"
TITLE = "hermes update keeps deferring on gateway "
CLOSED = ("done", "failed", "cancelled", "archived")
_HOST = re.compile(r"^On `([^`]+)`", re.MULTILINE)
_REFUSAL = re.compile(r"Latest refusal: (.*)$", re.MULTILINE)
_SIBLING = re.compile(r"^Sibling alarm: (t_\w+)", re.MULTILINE)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def alarm_key(host: str, root, unit: str) -> str:
    """The idempotency key of ``host``'s alarm for ``unit`` under the checkout ``root``."""
    return "update-pause-deferred:" + hashlib.sha256(f"{host}\0{root}\0{unit}".encode()).hexdigest()[:24]


def legacy_key(root, unit: str) -> str:
    """The pre-host key: only this host's own open card under it is still adopted."""
    return "update-pause-deferred:" + hashlib.sha256(f"{root}\0{unit}".encode()).hexdigest()[:24]


def host_identity(conn: sqlite3.Connection) -> tuple[str, frozenset]:
    """``(host, names)``: the Board gateway id this board file leases as (else the hostname), and every
    name a card filed on this host can carry (its ``created_on`` or the body's ``On `<host>``` line)."""
    from hermes_cli import kanban_capability_routing as kcr
    gateway, hostname = kcr._own_gateway(conn), socket.gethostname()
    return gateway or hostname, frozenset(n for n in (gateway, hostname) if n)


def refusal_class(unit: str, reason) -> str:
    """The refusal without its variable parts: the unit name, numbers (pids, seconds) and the detail
    after the first ``": "`` (stderr, exception text). Two hosts refused alike when the classes match."""
    text = str(reason or "")
    if unit:
        text = text.replace(unit, "<unit>")
    text = _NUMBER.sub("N", text.split(": ", 1)[0])
    return " ".join(text.lower().split())[:200]


def _db_path(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute("PRAGMA database_list").fetchone()
    return row[2] if row and row[2] else None


@contextmanager
def _replica(conn: sqlite3.Connection) -> Iterator[Optional[sqlite3.Connection]]:
    """The board's Board replica, read-only, or None (seam: tests replace it)."""
    from hermes_cli import kanban_foreign_card as kfc
    with kfc.open_replica(_db_path(conn)) as rep:
        yield rep


def _open_alarms(db: sqlite3.Connection, unit: str) -> list[dict]:
    cols = {r[1] for r in db.execute("PRAGMA table_info(tasks)")}
    on = "created_on" if "created_on" in cols else "NULL"
    marks = ",".join("?" * len(CLOSED))
    rows = db.execute(
        f"SELECT id, body, created_at, {on} FROM tasks WHERE created_by = ? AND title = ? "
        f"AND status NOT IN ({marks}) ORDER BY created_at, id", (AUTHOR, TITLE + unit, *CLOSED)).fetchall()
    out = []
    for task_id, body, created_at, created_on in rows:
        body = body or ""
        host = created_on or (m.group(1) if (m := _HOST.search(body)) else None)
        refusal = m.group(1).strip() if (m := _REFUSAL.search(body)) else ""
        sibling = m.group(1) if (m := _SIBLING.search(body)) else None
        out.append({"id": task_id, "host": host, "created_at": created_at, "refusal": refusal, "sibling": sibling})
    return out


def _board_alarms(conn: sqlite3.Connection, unit: str) -> list[dict]:
    """Open alarms for ``unit`` on this board: the live file's, then the replica's (the whole board)."""
    found = {a["id"]: dict(a, local=True) for a in _open_alarms(conn, unit)}
    try:
        with _replica(conn) as rep:
            if rep is not None:
                for a in _open_alarms(rep, unit):
                    found.setdefault(a["id"], dict(a, local=False))
    except sqlite3.Error as exc:  # an unreadable replica only narrows the lookup to the live file
        log.info("update-pause: replica unreadable for the sibling lookup (%s)", exc)
    return sorted(found.values(), key=lambda a: (a["created_at"] or 0, a["id"]))


def find_sibling(conn: sqlite3.Connection, unit: str, reason, own: frozenset) -> Optional[dict]:
    """The oldest open alarm another host filed for ``unit`` with this refusal's class, else None.
    Oldest, so a third host joins the first investigation rather than a follower of it."""
    want = refusal_class(unit, reason)
    for alarm in _board_alarms(conn, unit):
        if alarm["host"] and alarm["host"] not in own and refusal_class(unit, alarm["refusal"]) == want:
            return alarm
    return None


def sibling_section(sibling: dict, host: str) -> str:
    """Body text of a card filed while ``sibling`` is open: name it and park on it first."""
    sid = sibling["id"]
    return (f"Sibling alarm: {sid} (filed on `{sibling['host']}`) reports the same refusal for this gateway; "
            "the likely cause is shared, not local to this host.\n\n"
            f"First step: do not investigate from scratch. Read {sid}, its comments and the PR it opened, and "
            f"check that fix against this host (same code path, same unit setup). If it covers this host, park "
            f"this card on it (wake when the fix is in this host's checkout) and make sure {sid}'s rollout "
            f"includes `{host}`. Investigate here only when its diagnosis does not hold on this host, and say "
            f"why on both cards. If {sid} has no comment naming `{host}` yet, add one: it lives on another "
            "gateway, so use kanban_supervisor_request(ops=[{\"op\": \"operator\", \"action\": \"comment\", "
            f"\"task_id\": \"{sid}\", \"text\": \"...\"}}]).\n\n")


def _note(follower: str, host: str, at, refusal: str) -> str:
    return (f"Same refusal on `{host}` (at {at}): {refusal}\n"
            f"Its alarm {follower} names this card as the likely shared cause and parks on this fix; include "
            f"`{host}` in the rollout.")


def _noted(conn: sqlite3.Connection, task_id: str, follower: str) -> bool:
    return conn.execute("SELECT 1 FROM task_comments WHERE task_id = ? AND author = ? AND body LIKE ?",
                        (task_id, AUTHOR, f"%Its alarm {follower} %")).fetchone() is not None


def notify(conn: sqlite3.Connection, sibling: dict, follower: str, host: str, at, refusal: str) -> bool:
    """Comment on ``sibling`` when this board file holds it; True when written. A card another gateway
    created is not writable here: its home adds the comment on its next deferral (:func:`mirror_followers`)."""
    from hermes_cli import kanban_db as kb
    if not sibling.get("local") or _noted(conn, sibling["id"], follower):
        return False
    kb.add_comment(conn, sibling["id"], AUTHOR, _note(follower, host, at, refusal))
    return True


def mirror_followers(conn: sqlite3.Connection, own_id: str, unit: str, own: frozenset) -> list[str]:
    """Comment on ``own_id`` (this host's open alarm) for every other host's open alarm that names it as
    its sibling and is not noted yet; the follower ids commented."""
    from hermes_cli import kanban_db as kb
    added = []
    for alarm in _board_alarms(conn, unit):
        if alarm["sibling"] != own_id or alarm["host"] in own or _noted(conn, own_id, alarm["id"]):
            continue
        at = datetime.fromtimestamp(int(alarm["created_at"] or 0), UTC).isoformat(timespec="seconds")
        kb.add_comment(conn, own_id, AUTHOR, _note(alarm["id"], alarm["host"] or "another host", at, alarm["refusal"]))
        added.append(alarm["id"])
    return added
