"""Hub observer for overdue home-owned wake/readiness transitions. [daveey]

The existing overdue_wake diagnostic is a per-card display, not an incident
producer. This pass reads the full replica even when the target home is stopped.
Only the hub files: alarm provenance names the observer, not the stopped home.
"""
from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

GRACE_SECONDS = 30 * 60
_HOME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_LOCAL_WORK = re.compile(
    r"unpublished|unpushed|uncommitted|machine.only|only (?:on|in)|local.only|"
    r"(?:/home/|/Users/|~/|worktree|scratch|\.tar\.gz)", re.IGNORECASE)


def _scheduled_since(replica, row) -> int | None:
    from hermes_cli.kanban_continuation import park_state
    from hermes_cli.kanban_wake_summary import wakes
    park = park_state(replica, row["id"])
    if park.get("event_id") is None or park.get("superseded"):
        return None  # no unspent park in this replica: do not invent a deadline
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
    last_events = dict(replica.execute(
        "SELECT origin_gateway, MAX(created_at) FROM task_events GROUP BY origin_gateway").fetchall())
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in replica.execute("SELECT * FROM tasks WHERE status IN ('scheduled', 'todo')"):
        home = row["created_on"]
        if not isinstance(home, str) or not _HOME.fullmatch(home):
            continue
        entry = machines.get(home) or {}
        held = bool(entry.get("hold"))
        revoked = entry.get("state") == "revoked"
        seen = last_events.get(home)
        silent = seen is None or now - seen > GRACE_SECONDS
        if not (held or revoked or silent):
            continue
        since = (_scheduled_since(replica, row) if row["status"] == "scheduled"
                 else _parents_done_since(replica, row["id"]))
        if since is None or now - since <= GRACE_SECONDS:
            continue
        groups.setdefault(home, []).append({
            "id": row["id"], "title": row["title"], "tenant": row["tenant"],
            "status": row["status"], "since": since,
            "home_evidence": f"held={held}, revoked={revoked}, last Board event={seen}",
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


def sweep(conn, replica, own: str, *, now_ms: int) -> list[str]:
    from hermes_cli import kanban_pickup_stall as kps
    from hermes_cli import kanban_worker_node as wn
    if own != kps.OPERATOR_LANE:
        return []  # one observer; source-authenticated keys cannot impersonate a remote home
    registry = wn.registry()
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
        ids.append(kps._create(
            conn, key=f"{kps.KEY_PREFIX}{own}:home-overdue-{home}:",
            title=f"Home transitions overdue on {home} ({len(cards)} cards)", body=body,
            tenant=cards[0]["tenant"], own=own, now_ms=now_ms, howto=HOWTO))
    return ids
