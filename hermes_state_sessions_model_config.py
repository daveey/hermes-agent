"""Conversation-wide ``sessions.model_config`` keys: read-modify-write at the compression tip, the
key scan, and default seeding. Bodies of :class:`hermes_state_sessions.SessionSessionsMixin`'s methods
of the same names."""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from hermes_state_common import _sql_json_extract


def update_conversation_model_config_value(db, session_id: str, key: str,
                                           update: Callable[[Any], Any]) -> Optional[str]:
    """Read a conversation-wide ``key`` at *session_id*'s compression tip and write ``update(value)``
    back, in one write transaction: a concurrent writer of the key cannot be overwritten with a stale
    copy, and a rotation cannot publish between finding the tip and writing it (the child would keep
    the old copy). ``update`` may run more than once (lock retry) and must be pure; returning None
    removes the key. Returns the id written, None when the row does not exist."""
    from hermes_state_sessions import _parse_model_config

    def _do(conn) -> Optional[str]:
        tip = db._compression_tip_on(conn, session_id)
        row = conn.execute("SELECT model_config FROM sessions WHERE id = ?", (tip,)).fetchone()
        if row is None:
            return None
        config = _parse_model_config(row[0])
        if (value := update(config.get(key))) is None:
            config.pop(key, None)
        else:
            config[key] = value
        conn.execute("UPDATE sessions SET model_config = ? WHERE id = ?", (json.dumps(config), tip))
        return tip
    return db._execute_write(_do)


def session_ids_with_model_config_key(db, key: str) -> list[str]:
    """Ids of the sessions whose model_config carries ``key``, newest first."""
    with db._read_ctx() as conn:
        rows = conn.execute(
            f"SELECT id FROM sessions WHERE {_sql_json_extract('model_config', '$.' + key)} IS NOT NULL "
            "ORDER BY started_at DESC").fetchall()
    return [row["id"] for row in rows]


def seed_session_model_config(db, session_id: str, defaults: dict[str, Any]) -> None:
    """Add the ``defaults`` keys the row's model_config lacks, atomically; existing keys (including an
    explicit ``None``) always win. For an agent adopting a row another writer created first (the
    gateway's bare SessionStore row): the create upsert would have kept that writer's model_config
    whole, so the agent's init config (``model_routing``, ``max_iterations``) never reached it."""
    from hermes_state_sessions import _parse_model_config

    if not session_id or not isinstance(defaults, dict) or not defaults:
        return

    def _do(conn):
        row = conn.execute("SELECT model_config FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            return
        config = _parse_model_config(row[0])
        missing = {k: v for k, v in defaults.items() if k not in config}
        if not missing:
            return
        config.update(missing)
        conn.execute("UPDATE sessions SET model_config = ? WHERE id = ?", (json.dumps(config), session_id))
    db._execute_write(_do)
