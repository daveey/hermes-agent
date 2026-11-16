"""Logical remote-notice identity survives home moves; physical refs retain custody."""
from __future__ import annotations


def moved_claim(conn, *, session, ref, state, now, notice_key, swept_key, transcript_has):
    """Settle an already delivered home alias, or leave its live custodian alone."""
    from tui_gateway import kanban_remote_notices as notices

    task_id = ref.rsplit('/', 1)[-1]
    rows = conn.execute('SELECT ref, delivered, holder_pid, holder_start, notice_key FROM notices '
                        'WHERE session=? AND state=? AND ref!=?', (session, state, ref)).fetchall()
    aliases = [row for row in rows if row['ref'].rsplit('/', 1)[-1] == task_id]
    written = any(row['notice_key'] and transcript_has(row['notice_key'], '')
                  for row in aliases) if transcript_has is not None else False
    if any(row['delivered'] for row in aliases) or written:
        with conn:
            conn.execute('INSERT OR IGNORE INTO notices '
                         '(session, ref, state, at, delivered, notice_key, legacy_seen, settled) '
                         'VALUES (?,?,?,?,?,?,1,?)',
                         (session, ref, state, now, notices.SETTLED, notice_key, 'moved'))
            notices._swept_row(conn, swept_key, 'moved')
        return 'settled'
    if any(notices._holder_alive(row['holder_pid'], row['holder_start']) for row in aliases):
        return 'skip'
    return None
