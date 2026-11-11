"""Atomic event-key custody checks for persisted Kanban notices."""


def existing_notice(conn, session_id, notice_id, keys, notice_by_id_sql):
    from hermes_state_sessions import _LINEAGE_CTE_SQL

    existing = conn.execute(_LINEAGE_CTE_SQL + notice_by_id_sql,
                            (session_id, session_id, notice_id)).fetchone()
    if keys:
        rows = conn.execute(
            _LINEAGE_CTE_SQL + " SELECT m.id, j.value FROM messages m, "
            "json_each(json_extract(m.display_metadata, '$.kanban_event_keys')) j "
            "WHERE m.session_id IN (SELECT id FROM lineage) AND m.display_kind = 'kanban_notice' "
            "AND json_valid(m.display_metadata) AND j.value IN (" + ",".join("?" * len(keys)) + ")",
            (session_id, session_id, *keys)).fetchall()
        covered = {str(row[1]) for row in rows}
        if set(keys) <= covered:
            return rows[0]
        if existing is not None or covered:
            from hermes_state_errors import KanbanNoticeOverlapError
            raise KanbanNoticeOverlapError("Kanban notice partial key overlap; retry missing members")
    return existing
