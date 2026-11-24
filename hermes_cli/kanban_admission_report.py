"""Weekly capacity-class effect report (docs/kanban-hub-admission-v2.md §4). [daveey]

For each ``capacity`` card completed in the window, compare the local ready-wait p50 (claim start minus the start of
that ready interval) of other cards claimed in the 24 h before and after its completion. Fewer than
``MIN_SAMPLES`` claims on either side is ``unknown``: missing evidence is never a success or a failure. An origin
with at least ``REVOKE_AFTER`` measured no-effect cards and none that moved is flagged ``revoke_recommended``; the
report recommends, an operator acts (``hermes kanban edit <id> --class normal``).
"""
from __future__ import annotations

import statistics
import time
from typing import Optional

from hermes_cli.kanban_diagnostics import _READY_WAIT_FLOOR_KINDS
from hermes_cli.kanban_ready_events import conditional_ready_entry_sql

SIDE_S = 86_400
MIN_SAMPLES = 3
MOVED_RATIO = 0.9
REVOKE_AFTER = 2
_CLAIM_KINDS = ("claimed", "spawned")


def _waits(conn, start: int, end: int, exclude: str) -> list[int]:
    kinds = sorted(_READY_WAIT_FLOOR_KINDS - set(_CLAIM_KINDS))
    marks = ",".join("?" for _ in kinds)
    rows = conn.execute(
        "SELECT r.started_at, t.created_at, "
        f"(SELECT MAX(e.created_at) FROM task_events e WHERE e.task_id = r.task_id AND "
        f"(e.kind IN ({marks}) OR {conditional_ready_entry_sql('e.kind', 'e.payload')}) "
        " AND e.created_at <= r.started_at), "
        "(SELECT MAX(p.ended_at) FROM task_runs p WHERE p.task_id = r.task_id AND p.id < r.id) "
        "FROM task_runs r JOIN tasks t ON t.id = r.task_id "
        "WHERE r.started_at >= ? AND r.started_at < ? AND r.task_id != ?",
        (*kinds, start, end, exclude)).fetchall()
    return [max(0, started - max(created or 0, event or 0, ended or 0)) for started, created, event, ended in rows]


def _p50(values: list[int]) -> Optional[float]:
    return statistics.median(values) if len(values) >= MIN_SAMPLES else None


def capacity_report(conn, *, now_s: Optional[int] = None, days: int = 7) -> dict:
    now_s = int(time.time()) if now_s is None else now_s
    cards = []
    for tid, title, frees, completed, origin in conn.execute(
        "SELECT t.id, t.title, t.admission_frees, t.completed_at, "
        "(SELECT r.admission_origin FROM task_runs r WHERE r.task_id = t.id AND r.admission_class = 'capacity' "
        " ORDER BY r.id LIMIT 1) "
        "FROM tasks t WHERE t.admission_class = 'capacity' AND t.status = 'done' AND t.completed_at >= ? "
        "ORDER BY t.completed_at, t.id", (now_s - days * 86_400,)).fetchall():
        before = _p50(_waits(conn, completed - SIDE_S, completed, tid))
        after = _p50(_waits(conn, completed, min(now_s, completed + SIDE_S), tid))
        if before is None or after is None:
            effect = "unknown"
        else:
            effect = "moved" if after <= before * MOVED_RATIO else "no_effect"
        cards.append({"id": tid, "title": title, "frees": frees, "origin": origin or "unknown",
                      "completed_at": completed, "ready_wait_p50_before_s": before,
                      "ready_wait_p50_after_s": after, "effect": effect})
    origins: dict[str, dict] = {}
    for card in cards:
        tally = origins.setdefault(card["origin"], {"moved": 0, "no_effect": 0, "unknown": 0})
        tally[card["effect"]] += 1
    for tally in origins.values():
        tally["revoke_recommended"] = tally["no_effect"] >= REVOKE_AFTER and tally["moved"] == 0
    return {"days": days, "generated_at": now_s, "cards": cards, "origins": origins}


def render(data: dict) -> str:
    def secs(value):
        return "?" if value is None else f"{value:g}s"

    lines = [f"capacity cards completed in the last {data['days']} day(s): {len(data['cards'])}"]
    for card in data["cards"]:
        lines.append(f"  {card['id']}  {card['effect']:9s}  ready-wait p50 {secs(card['ready_wait_p50_before_s'])}"
                     f" -> {secs(card['ready_wait_p50_after_s'])}  {card['origin']}  {card['frees'] or ''}")
    for origin, tally in sorted(data["origins"].items()):
        verdict = " -> remove capacity class from new cards" if tally["revoke_recommended"] else ""
        lines.append(f"{origin}: {tally['moved']} moved, {tally['no_effect']} no effect, "
                     f"{tally['unknown']} unknown{verdict}")
    return "\n".join(lines)
