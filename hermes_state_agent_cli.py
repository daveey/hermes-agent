"""Ungated retirement of explicitly attributed, hidden CLI proof chats."""
import time

from hermes_state_common import _sql_session_last_active


def archive_agent_cli(db):
    """Archive completed proofs or abandoned proofs idle >24h; never infer authorship from hidden alone."""
    rows = db._read_all(f"""SELECT s.id FROM sessions s
        WHERE s.archived = 0 AND s.hidden = 1
          AND s.created_source IN ('cli', 'tui', 'oneshot')
          AND NOT EXISTS (SELECT 1 FROM sessions child WHERE child.parent_session_id = s.id
                          AND s.end_reason = 'compression')
          AND CASE WHEN json_valid(s.model_config) THEN json_extract(s.model_config, '$._agent_cli') END = 1
          AND (s.ended_at IS NOT NULL
               OR CASE WHEN json_valid(s.outcome_json) THEN json_extract(s.outcome_json, '$.kind') END = 'done'
               OR {_sql_session_last_active('s')} < ?)""", (time.time() - 86400,))
    ids = {row['id'] for row in rows}
    if not ids:
        return 0
    with db._read_ctx() as conn:
        ids -= db._guarded_ids(conn, ids)
    return sum(bool(db._auto_archive_lineage(sid)) for sid in sorted(ids))
