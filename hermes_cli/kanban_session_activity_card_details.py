"""Per listed card, what the linked-card rows show (split out of ``kanban_session_activity``). [daveey]"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from hermes_cli.kanban_run_machine import add_run_machines
from hermes_cli.kanban_run_model import add_run_models

_PR_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
_NOTE_CHARS = 200


def _first_line(text: Any) -> str:
    line = str(text or "").strip().split("\n", 1)[0].strip()
    return line[:_NOTE_CHARS]


def _add_card_details(conn: sqlite3.Connection, found: dict[str, dict[str, Any]]) -> None:
    """Per listed card, what the linked-card rows show: the latest event (``activity_kind``/``activity_at``), a
    one-line ``note`` (the newest run summary or comment) and the newest GitHub PR ``pr_url`` found in its
    comments, result or run summaries. One query per table for all listed cards; missing tables are skipped."""
    if not found:
        return
    ids = list(found)
    ph = ",".join("?" for _ in ids)

    def rows(sql: str) -> list[sqlite3.Row]:
        try:
            return conn.execute(sql, ids).fetchall()
        except sqlite3.Error:
            return []

    for r in rows(f"SELECT *, MAX(created_at) AS at FROM task_events WHERE task_id IN ({ph}) GROUP BY task_id"):
        found[r["task_id"]].update(activity_kind=r["kind"], activity_at=r["at"])
        event = dict(r)
        found[r["task_id"]]["_execution_event"] = {
            k: event.get(k) for k in ("id", "run_id", "created_at", "kind")}
    notes: dict[str, tuple[Any, str]] = {}
    prs: dict[str, tuple[Any, str]] = {}

    def offer(task_id: str, at: Any, text: Any) -> None:
        at = at or 0
        line = _first_line(text)
        if line and at >= notes.get(task_id, (-1, ""))[0]:
            notes[task_id] = (at, line)
        for url in _PR_URL_RE.findall(str(text or "")):
            if at >= prs.get(task_id, (-1, ""))[0]:
                prs[task_id] = (at, url)

    for r in rows(f"SELECT task_id, COALESCE(ended_at, started_at) AS at, summary FROM task_runs "
                  f"WHERE task_id IN ({ph}) AND summary IS NOT NULL"):
        offer(r["task_id"], r["at"], r["summary"])
    for r in rows(f"SELECT task_id, created_at AS at, body FROM task_comments WHERE task_id IN ({ph})"):
        offer(r["task_id"], r["at"], r["body"])
    for r in rows(f"SELECT id AS task_id, completed_at AS at, result FROM tasks WHERE id IN ({ph}) AND result IS NOT NULL"):
        for url in _PR_URL_RE.findall(str(r["result"] or "")):
            if (r["at"] or 0) >= prs.get(r["task_id"], (-1, ""))[0]:
                prs[r["task_id"]] = (r["at"] or 0, url)
    for task_id, (_, line) in notes.items():
        found[task_id]["note"] = line
    for task_id, (_, url) in prs.items():
        found[task_id]["pr_url"] = url
    add_run_machines(conn, found)  # where each card runs: the strip's hub/worker mark
    add_run_models(conn, found)  # which model its newest run uses (route class/pin, or attested models)
    _add_blocked_upstream(conn, found)


def _replica_for(conn: sqlite3.Connection) -> sqlite3.Connection | None:
    """The board's sync-shadow replica, or None. Tests may replace this."""
    try:
        from hermes_cli.kanban_db_sync import _db_key, board_slug_for_path
        from hermes_cli.kanban_sync_client import shadow_path
        key = _db_key(conn)
        if not key:
            return None
        path = shadow_path(board_slug_for_path(key))
        if not path.is_file() or Path(key).resolve() == path.resolve():
            return None
        replica = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        replica.row_factory = sqlite3.Row
        return replica
    except (OSError, TypeError, ValueError, sqlite3.Error):
        return None


def _query(src: sqlite3.Connection | None, sql: str, args: tuple, *, required: bool = False) -> list:
    if src is None:
        return []
    try:
        return src.execute(sql, args).fetchall()
    except sqlite3.Error:
        if required:
            raise
        return []


def _task_info(conn: sqlite3.Connection, replica: sqlite3.Connection | None, task_id: str) -> dict[str, Any] | None:
    for src in (conn, replica):
        rows = _query(src, "SELECT id, status, containment_episode FROM tasks WHERE id=?", (task_id,))
        if not rows:
            rows = _query(src, "SELECT id, status FROM tasks WHERE id=?", (task_id,))
        if rows:
            row = rows[0]
            episode = row["containment_episode"] if "containment_episode" in row.keys() else None
            return {"id": row["id"], "status": row["status"], "episode": episode}
    return None


def _parent_ids(conn: sqlite3.Connection, replica: sqlite3.Connection | None, child: str) -> list[str]:
    seen: set[str] = set()
    ids: list[str] = []
    for src, required in ((conn, True), (replica, False)):
        for row in _query(src, "SELECT parent_id FROM task_links WHERE child_id=? ORDER BY parent_id",
                          (child,), required=required):
            parent = row[0]
            if parent not in seen:
                seen.add(parent)
                ids.append(parent)
    return ids


def _authority(conn: sqlite3.Connection, replica: sqlite3.Connection | None,
               task_id: str, episode: str) -> dict[str, str]:
    auth_id = ""
    for src in (conn, replica):
        for row in _query(src, "SELECT payload FROM task_events WHERE task_id=? AND uid=? AND kind='contained'",
                          (task_id, episode)):
            raw = row[0]
            try:
                payload = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict):
                policy = payload.get("policy")
                authority = policy.get("authority") if isinstance(policy, dict) else None
                if isinstance(authority, dict) and authority.get("task_id"):
                    auth_id = str(authority["task_id"])
                    break
        if auth_id:
            break
    status = ""
    if auth_id:
        info = _task_info(conn, replica, auth_id)
        status = str(info["status"]) if info else ""
    return {"id": auth_id, "status": status}


def _blocked_payload(conn: sqlite3.Connection, replica: sqlite3.Connection | None,
                     parent: str, info: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"id": parent, "status": info["status"]}
    episode = info.get("episode")
    if info["status"] == "triage" and episode:
        out["contained"] = {"episode": episode, "authority": _authority(conn, replica, parent, episode)}
    return out


def _nearest_blocked(conn: sqlite3.Connection, task_id: str,
                     replica: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    """Breadth-first, so the first match is nearest; ties sort by id."""
    blocked, _upstream = _walk(conn, task_id, replica)
    return blocked


def _walk(conn: sqlite3.Connection, task_id: str,
          replica: sqlite3.Connection | None) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    seen = {task_id}
    frontier = {task_id}
    nearest_open: dict[str, str] | None = None
    for _ in range(20):
        parents: dict[str, dict[str, Any]] = {}
        for child in sorted(frontier):
            for parent in _parent_ids(conn, replica, child):
                if parent in seen or parent in parents:
                    continue
                info = _task_info(conn, replica, parent)
                if info is not None:
                    parents[parent] = info
        if not parents:
            return None, nearest_open
        for parent in sorted(parents):
            status = parents[parent]["status"]
            if nearest_open is None and status not in {"todo", "done", "archived"}:
                nearest_open = {"id": parent, "status": status}
            if status in {"blocked", "triage"}:
                return _blocked_payload(conn, replica, parent, parents[parent]), nearest_open
        seen.update(parents)
        frontier = set(parents)
    return None, nearest_open


def _add_blocked_upstream(conn: sqlite3.Connection, found: dict[str, dict[str, Any]]) -> None:
    """Optional presentation evidence; old schemas remain readable. Replica fills foreign parents."""
    replica = _replica_for(conn)
    try:
        for task_id, card in found.items():
            card.pop("blocked_upstream", None)
            card.pop("upstream", None)
            if card.get("status") in {"done", "archived", "blocked", "triage"}:
                continue
            try:
                blocked, upstream = _walk(conn, task_id, replica)
            except sqlite3.Error:
                logging.getLogger(__name__).debug("Could not read blocked upstream for %s", task_id, exc_info=True)
                continue
            if blocked is not None:
                card["blocked_upstream"] = blocked
            elif upstream is not None:
                card["upstream"] = upstream
    finally:
        if replica is not None:
            replica.close()


def annotate_remote_upstream(cards: list[dict[str, Any]]) -> None:
    """Walk replica graphs for listed remote rows that local details never saw."""
    pending: dict[str, dict[str, dict[str, Any]]] = {}
    for card in cards:
        if not isinstance(card, dict) or "id" not in card:
            continue
        if card.get("status") in {"done", "archived", "blocked", "triage"}:
            continue
        if "blocked_upstream" in card or "upstream" in card:
            continue
        pending.setdefault(str(card.get("board") or "default"), {})[str(card["id"])] = card
    for board, found in pending.items():
        replica = None
        try:
            from hermes_cli.kanban_sync_client import shadow_path
            path = shadow_path(board)
            if not path.is_file():
                continue
            replica = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            replica.row_factory = sqlite3.Row
            _add_blocked_upstream(replica, found)
        except (OSError, TypeError, ValueError, sqlite3.Error):
            logging.getLogger(__name__).debug("Could not annotate remote upstream for %s", board, exc_info=True)
        finally:
            if replica is not None:
                replica.close()
