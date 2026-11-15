"""Same UTC day and area selection, under the intake authority's write lock."""
from dataclasses import dataclass
from datetime import UTC, datetime

from hermes_cli import kanban_selfwork as policy


@dataclass(frozen=True)
class Decision:
    disposition: str
    task_id: str | None = None


def day_key(now):
    return datetime.fromtimestamp(now, UTC).date().isoformat()


def choose(conn, value, *, now, tenant=None):
    value = policy.contract(value)
    if value is None:
        return Decision('create')
    row = conn.execute(
        "SELECT t.id FROM tasks t JOIN task_events e ON e.task_id=t.id "
        "WHERE t.tenant IS ? AND t.status NOT IN ('done','archived') AND e.kind=? "
        "AND json_extract(e.payload,'$.repository')=? AND json_extract(e.payload,'$.area')=? "
        "AND json_extract(e.payload,'$.day')=? ORDER BY t.created_at,t.id LIMIT 1",
        (tenant, policy.EVENT, value['repository'], value['area'], day_key(now)),
    ).fetchone()
    return Decision('append', row['id']) if row else Decision('create')


def append(conn, decision, value, *, request_id, title, body, author):
    from hermes_cli import kanban_db as kb
    if not conn.in_transaction or decision.disposition != 'append' or not decision.task_id:
        raise policy.SelfWorkError('self-work append requires a transactional owner decision')
    task = kb.get_task(conn, decision.task_id)
    if task is None or task.status in {'done', 'archived'}:
        raise policy.SelfWorkError('self-work owner no longer open')
    comment_id = kb.add_comment(conn, task.id, author,
                               f'Self-work finding ({request_id}): {title}\n\n{body or ""}', system=True)
    receipt = dict(request_id=request_id, task_id=task.id, self_work=value,
                   comment_id=comment_id, disposition='append', created=False)
    kb._append_event(conn, task.id, 'self_work_appended', receipt)
    return receipt
