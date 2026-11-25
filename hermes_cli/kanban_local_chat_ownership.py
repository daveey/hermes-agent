"""Read-only local created-chat membership, independent of notification routing."""
from __future__ import annotations

import sqlite3


def created_rows(conn: sqlite3.Connection, fields: str, match: str, params=()):
    """Rows for this gateway/profile's unreleased creating links on matching tasks.

    The caller owns its transaction and task/watch predicate. Missing legacy
    tables are supported; malformed installed schemas must not certify no work.
    """
    from hermes_cli.kanban_table_columns import table_columns
    from hermes_cli.kanban_origin import gateway_names
    from hermes_cli.profiles import get_active_profile_name

    if not table_columns(conn, "task_chat_links"):
        return []
    me = gateway_names()[0]
    if not me and "gateway_id" in table_columns(conn, "kanban_sync_state"):
        row = conn.execute("SELECT gateway_id FROM kanban_sync_state LIMIT 1").fetchone()
        me = row[0] if row else ""
    if not me:
        return []  # unknown identity cannot claim another gateway's chats
    return conn.execute(
        f"SELECT {fields} FROM task_chat_links l JOIN tasks t ON t.id=l.task_id "
        "WHERE l.kind='created' AND l.released=0 AND l.chat_gateway=? "
        "AND COALESCE(NULLIF(l.chat_profile,''),'default')=? AND l.chat_session!='' "
        f"AND ({match})",
        (me, get_active_profile_name(), *params)).fetchall()


def parked_pairs(conn: sqlite3.Connection, *, created_links: bool = True) -> set[tuple[str, str]]:
    """Parked display membership; wake callers opt out of links-only ownership."""
    from hermes_cli.kanban_session_activity import _counted_sub_clause, table_columns, _not_watch

    if "session_id" not in table_columns(conn, "tasks"):
        return set()
    parked = f"(t.status IN ('scheduled', 'blocked', 'triage') AND {_not_watch(conn)})"
    pairs: set[tuple[str, str]] = {
        (str(sid), str(tid)) for sid, tid in conn.execute(
            f"SELECT session_id, id FROM tasks t WHERE session_id IS NOT NULL AND session_id != '' AND {parked}")}
    try:
        pairs.update(
            (str(sid), str(tid)) for sid, tid in conn.execute(
                f"SELECT s.chat_id, t.id FROM kanban_notify_subs s JOIN tasks t ON t.id = s.task_id "
                f"WHERE s.platform = 'tui' AND s.chat_id != '' AND {_counted_sub_clause(conn)} AND {parked}"))
    except sqlite3.Error:  # board predates notify subscriptions
        pass
    if created_links:
        pairs.update((str(sid), str(tid)) for sid, tid in
                     created_rows(conn, "l.chat_session, t.id", parked))
    return pairs
