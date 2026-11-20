"""Hub observer for overdue home-owned wake/readiness transitions. [daveey]

The existing overdue_wake diagnostic is a per-card display, not an incident
producer. This pass reads the full replica even when the target home is stopped.
Only the hub files: alarm provenance names the observer, not the stopped home.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from types import SimpleNamespace
from typing import Any

GRACE_SECONDS = 30 * 60
_HOME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_LOCAL_WORK = re.compile(
    r"unpublished|unpushed|uncommitted|machine.only|only (?:on|in)|local.only|"
    r"(?:/home/|/Users/|~/|worktree|scratch|\.tar\.gz)", re.IGNORECASE)


def _scheduled_since(replica, row) -> int | None:
    from hermes_cli.kanban_wake_summary import wakes
    summary = wakes(replica, SimpleNamespace(**dict(row)), check_loaded=True)
    if not summary or summary["blocks_for_human"]:
        return None
    return summary["backstop_at"]


def _parents_done_since(replica, task_id: str) -> int | None:
    rows = replica.execute(
        "SELECT p.id, p.status, p.completed_at FROM task_links l "
        "LEFT JOIN tasks p ON p.id = l.parent_id WHERE l.child_id = ?", (task_id,)).fetchall()
    if not rows or any(r["status"] != "done" or not r["completed_at"] for r in rows):
        return None  # missing, incomplete or undated parents are not proven ready
    return max(int(r["completed_at"]) for r in rows)


def checkpoint_evidence(replica, task_id: str) -> str:
    """Conservative report, never a portability authorization inferred from prose."""
    row = replica.execute(
        "SELECT summary FROM task_runs WHERE task_id = ? AND summary IS NOT NULL "
        "AND outcome = 'continued' ORDER BY ended_at DESC, id DESC LIMIT 1", (task_id,)).fetchone()
    if not row:
        return "unknown (no continuation checkpoint); inspect history before moving"
    text = row[0]
    if _LOCAL_WORK.search(text):
        return "possible machine-only WIP; verify publication/artifact custody: " + text[:1500]
    return "not named in latest checkpoint (not proof of portability): " + text[:1500]


def overdue(replica, machines: dict, *, now: float) -> dict[str, list[dict[str, Any]]]:
    """Group open scheduled/todo rows by held, revoked, or event-silent home."""
    recent_homes = {r[0] for r in replica.execute(
        "SELECT DISTINCT origin_gateway FROM task_events WHERE created_at >= ?",
        (now - GRACE_SECONDS,))}
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in replica.execute("SELECT * FROM tasks WHERE status IN ('scheduled', 'todo')"):
        home = row["created_on"]
        if not isinstance(home, str) or not _HOME.fullmatch(home):
            continue
        entry = machines.get(home) or {}
        held = bool(entry.get("hold"))
        revoked = entry.get("state") == "revoked"
        silent = home not in recent_homes
        if not (held or revoked or silent):
            continue
        since = (_scheduled_since(replica, row) if row["status"] == "scheduled"
                 else _parents_done_since(replica, row["id"]))
        if since is None or now - since <= GRACE_SECONDS:
            continue
        groups.setdefault(home, []).append({
            "id": row["id"], "title": row["title"], "tenant": row["tenant"],
            "status": row["status"], "since": since,
            "home_evidence": (f"held={held}, revoked={revoked}" if held or revoked
                              else "no Board event within 30 minutes"),
            "checkpoint": checkpoint_evidence(replica, row["id"]),
        })
    return {home: sorted(cards, key=lambda c: (c["since"], c["id"]))
            for home, cards in sorted(groups.items())}


HOWTO = (
    "Inspect every listed card's latest checkpoint and artifact custody. Move every card without "
    "machine-only WIP to a live home using the supported hub-admin batched rehome route "
    "(hermes-hub PR #142), only when that route's eligibility checks permit it. Preserve status, "
    "wake time, wake_check, parents, children and comments; verify readback and a subsequent wake "
    "or promotion. List all remaining cards with reasons, including machine-only WIP, unknown "
    "custody, human requests, superseding continuations, or transfer API refusals. PR #142 requires "
    "revoked/dead origins and is not authorization to transfer active held origins. Never invent "
    "death/portability evidence, revoke, restart, contact, or unhold a held machine to make a "
    "transfer pass. Changing runs_on alone does not move home-owned readiness."
)


_NEXT_SCAN: dict[tuple, float] = {}


def observe(conn, *, now_ms: int) -> list[str]:
    """Observe independently of local pickup admission and isolate failures."""
    from hermes_cli import kanban_pickup_stall as kps
    from hermes_cli import kanban_worker_node as wn
    try:
        own = wn._own(conn)
        if own != kps.OPERATOR_LANE:
            return []
        replica = wn.open_replica(conn)
        if replica is None:
            return []
        try:
            return periodic_sweep(conn, replica, own, now_ms=now_ms)
        finally:
            replica.close()
    except Exception:  # health: allow BLE001 -- observer failure must not suppress ready-card alarms
        kps._log.warning("kanban dispatcher: home-stall observation failed", exc_info=True)
        return []


def periodic_sweep(conn, replica, own: str, *, now_ms: int) -> list[str]:
    """Bound the full event aggregation to once a minute per observer board."""
    from hermes_cli.kanban_sweep_gate import file_identity
    if own != "hub":
        return []
    identity = file_identity(replica)
    tick = time.monotonic()
    if identity is not None and tick < _NEXT_SCAN.get(identity, 0):
        return []
    result = sweep(conn, replica, own, now_ms=now_ms)
    if identity is not None:
        if len(_NEXT_SCAN) >= 128:
            _NEXT_SCAN.clear()
        _NEXT_SCAN[identity] = tick + 60
    return result


def _fresh(replica, now: float) -> bool:
    """Silence is meaningful only after a recent completed Board pull."""
    try:
        row = replica.execute("SELECT pulled_at FROM kanban_sync_state LIMIT 1").fetchone()
    except sqlite3.Error:
        return False
    from hermes_cli.kanban_replica_freshness import FRESH_SECONDS
    # A pull may finish after the dispatcher captured now_ms, before this read.
    return row is not None and type(row[0]) in (int, float) and -5 <= now - row[0] <= FRESH_SECONDS


# Pick: a closed incident keeps covering the same home and same overdue membership for 12 h (the repair
# completes after a no-op inspect, so the 1 h REPLACE_AFTER_SECONDS floor still minted ~24 cards/day for a
# silent Mac; 12 h gives <= 2/day). A changed membership files a fresh incident at once.
# Open incidents always win over a closed sibling. New bodies carry membership on their first
# line, before all title/checkpoint prose. The listing parser is only for legacy incidents.
# archive_task leaves completed_at NULL; such incidents cover indefinitely until membership changes.
HOME_REPLACE_AFTER_SECONDS = 12 * 3600
_MEMBERS = "Home-overdue membership v1: "
_LISTED = re.compile(
    r"^- (\S+) \(.*\), (?:scheduled|todo), \d+ minutes overdue", re.MULTILINE)


def _listed_ids(body: str) -> set[str]:
    first = (body or "").partition("\n")[0]
    if first.startswith(_MEMBERS):
        try:
            members = json.loads(first[len(_MEMBERS):])
        except ValueError:
            return set()
        return set(members) if isinstance(members, list) and all(isinstance(x, str) for x in members) else set()
    return set(_LISTED.findall(body or ""))


def _recently_closed(conn, key: str, card_ids: list[str], now_ms: int) -> str | None:
    from hermes_cli import kanban_pickup_stall as kps
    row = conn.execute(
        "SELECT id, body, completed_at FROM tasks WHERE created_by = ? "
        "AND idempotency_key >= ? AND idempotency_key < ? AND status IN ('done', 'archived') "
        "ORDER BY COALESCE(completed_at, created_at) DESC, id LIMIT 1", (kps.DETECTOR, key, key + "\U0010ffff")
    ).fetchone()
    if row is None:
        return None
    closed = row["completed_at"]
    if closed is not None and now_ms / 1000 - closed >= HOME_REPLACE_AFTER_SECONDS:
        return None
    return row["id"] if _listed_ids(row["body"] or "") == set(card_ids) else None


def sweep(conn, replica, own: str, *, now_ms: int) -> list[str]:
    from hermes_cli import kanban_pickup_stall as kps
    from hermes_cli import kanban_worker_node as wn
    if own != kps.OPERATOR_LANE:
        return []  # one observer; source-authenticated keys cannot impersonate a remote home
    if not _fresh(replica, now_ms / 1000):
        return []
    registry = wn.registry(require_fresh=True)
    if registry is None:
        return []  # cannot establish current held/revoked registry evidence
    ids = []
    for home, cards in overdue(replica, registry.get("machines") or {}, now=now_ms / 1000).items():
        listed = "\n".join(
            f"- {c['id']} ({c['title']}), {c['status']}, "
            f"{kps._minutes(c['since'] * 1000, now_ms)} minutes overdue "
            f"(since {kps._stamp(c['since'] * 1000)}); machine-only WIP: {c['checkpoint']}"
            for c in cards)
        body = (f"Home-owned transitions overdue on {home}: {len(cards)} card(s), threshold strictly "
                f"greater than 30 minutes. {cards[0]['home_evidence']}. A todo clock starts when the "
                "last parent completed; a scheduled clock uses its current wake/backstop.\n\n" + listed
                + "\n\n" + HOWTO)
        body = _MEMBERS + json.dumps(sorted(c["id"] for c in cards)) + "\n\n" + body
        key = f"{kps.KEY_PREFIX}{own}:home-overdue-{home}:"
        if not kps._covering_repair(conn, key, now_ms):
            recent = _recently_closed(conn, key, [c["id"] for c in cards], now_ms)
            if recent is not None:
                ids.append(recent)  # silence: same overdue set was just repaired
                continue
        ids.append(kps._create(
            conn, key=key,
            title=f"Home transitions overdue on {home} ({len(cards)} cards)", body=body,
            tenant=cards[0]["tenant"], own=own, now_ms=now_ms, howto=HOWTO))
    return ids
