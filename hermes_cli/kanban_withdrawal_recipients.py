"""Archive-time remote receipt custody, independent of later link cleanup."""
from __future__ import annotations

import json


def snapshot(conn, task_id: str) -> list[dict]:
    """Capture identities only, inside the archive write transaction."""
    destinations: set[tuple[str, str, str]] = set()
    released: set[tuple[str, str, str]] = set()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "task_chat_links" in tables:
        for gw, profile, session, release in conn.execute(
                "SELECT chat_gateway,chat_profile,chat_session,released FROM task_chat_links WHERE task_id=?",
                (task_id,)):
            key = (gw, profile or "default", session)
            (released if release else destinations).add(key)
    if "kanban_notify_subs" in tables:
        for (chat,) in conn.execute(
                "SELECT chat_id FROM kanban_notify_subs WHERE task_id=? AND platform='hub'", (task_id,)):
            parts = str(chat).split("/", 2)
            if len(parts) == 3 and all(parts):
                destinations.add((parts[0], parts[1], parts[2]))
    if "origin_creation_intents" in tables:
        for (raw,) in conn.execute("SELECT binding_json FROM origin_creation_intents WHERE task_id=?", (task_id,)):
            try:
                binding = json.loads(raw)
            except (TypeError, ValueError):
                continue
            origin = binding.get("origin") if isinstance(binding, dict) else None
            if not isinstance(origin, dict):
                continue
            gw, profile, session = (origin.get(field) for field in ("gateway", "profile", "session"))
            if all(isinstance(value, str) and value for value in (gw, profile, session)):
                destinations.add((gw, profile, session))
    return [dict(gw=gw, profile=profile, session=session, rel="shepherd")
            for gw, profile, session in sorted(destinations - released)]


def published(conn, event_id: int) -> list[dict] | None:
    """No fallback to current bindings: their custody may have changed."""
    row = conn.execute("SELECT payload FROM task_events WHERE id=?", (event_id,)).fetchone()
    payload = json.loads(row[0])
    return payload.get("recipients")
