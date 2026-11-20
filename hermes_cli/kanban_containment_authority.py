"""Visible, explicitly acknowledged custody of open containment episodes."""
from __future__ import annotations

import json
import logging
import sqlite3

from hermes_cli.kanban_containment_lifecycle import ContainmentRefused

_log = logging.getLogger(__name__)


class AuthorityTerminal(ContainmentRefused):
    """A new episode needs an authority that can still decide its release."""

    code = "authority_terminal"


def require_open(status: str | None) -> None:
    if status in ("done", "archived"):
        raise AuthorityTerminal(
            "Name an open authority card whose later run will decide the release; "
            "to wait for a review use kanban_request_review, or link the review card "
            "as parent and kanban_block(kind='dependency')."
        )


class AuthorityOpen(ValueError):
    """Completion needs a release or an explicit retained-episode handoff."""

    code = "containment_authority_open"

    def __init__(self, episodes: list[dict]):
        self.episodes = episodes
        super().__init__(
            f"{self.code}: open containment episodes {json.dumps(episodes)}. "
            "Call kanban_resume_contained(task_id, episode, kind='authority'), or "
            "complete again with metadata.containment_retained=[<episode uids>] "
            "and explain why the hold is retained in the summary."
        )


def _rows(conn: sqlite3.Connection) -> dict[str, tuple[str, dict]]:
    # Unsynced boards predating portable events cannot contain an episode.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(task_events)")}
    if "uid" not in columns:
        return {}
    rows = {}
    for tid, title, episode, payload in conn.execute(
        "SELECT t.id,t.title,t.containment_episode,e.payload FROM tasks t "
        "LEFT JOIN task_events e ON e.uid=t.containment_episode "
        "AND e.task_id=t.id AND e.kind='contained' WHERE t.containment_episode IS NOT NULL"
    ):
        data = json.loads(payload) if payload else None
        rows[tid] = (episode, {"title": title, "payload": data})
    return rows


def open_episodes(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    """Current markers + their exact stop events, local custody before replica.

    A local cleared marker overrides a stale replica hold; only foreign targets
    fall back to sync-shadow. No network or writes occur during this lookup.
    """
    from hermes_cli.kanban_db_connect import _main_db_file
    from hermes_cli.kanban_foreign_card import open_replica

    local = _rows(conn)
    with open_replica(_main_db_file(conn)) as replica:
        rows = _rows(replica) if replica is not None else {}
    rows = {tid: row for tid, row in rows.items()
            if not conn.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone()}
    rows.update(local)
    found = []
    for tid, (episode, row) in rows.items():
        payload = row["payload"]
        if not episode or not isinstance(payload, dict):
            continue
        policy = payload.get("policy")
        authority = policy.get("authority") if isinstance(policy, dict) else None
        if isinstance(authority, dict) and authority.get("task_id") == task_id:
            found.append({"task_id": tid, "episode": episode, "title": row["title"]})
    return sorted(found, key=lambda item: (item["task_id"], item["episode"]))


def _lookup(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    try:
        return open_episodes(conn, task_id)
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
        _log.warning("Containment authority lookup failed for %s: %s", task_id, exc)
        return []


def require_acknowledgement(conn: sqlite3.Connection, task_id: str, metadata: dict | None) -> None:
    episodes = _lookup(conn, task_id)
    retained = metadata.get("containment_retained") if isinstance(metadata, dict) else None
    acknowledged = set(retained) if isinstance(retained, list) and all(isinstance(e, str) for e in retained) else set()
    missing = [item for item in episodes if item["episode"] not in acknowledged]
    if missing:
        raise AuthorityOpen(missing)


def render_context(conn: sqlite3.Connection, task_id: str) -> str:
    episodes = _lookup(conn, task_id)
    if not episodes:
        return ""
    lines = ["## Containment authority", "You own the release decision for these open episodes:"]
    for item in episodes:
        lines.append(
            f"- {item['task_id']} ({item['title']}), episode {item['episode']}: "
            f"kanban_resume_contained(task_id='{item['task_id']}', episode='{item['episode']}', kind='authority')"
        )
    lines.append("Before completing, resume them or set metadata.containment_retained to the retained episode uids and explain why in the summary.")
    return "\n".join(lines) + "\n"
