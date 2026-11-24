"""Default-on creation freeze for explicitly declared guard maintenance."""
import logging

from hermes_cli import kanban_selfwork as policy
from hermes_cli import kanban_selfwork_batch as batch

log = logging.getLogger(__name__)


class GuardFrozen(policy.SelfWorkError):
    """Definitive pre-write refusal, safe for courier acknowledgement."""


def refusal(exc):
    import json
    return json.dumps({'ok': False, 'status': 'rejected', 'code': 'self_work_guard_frozen',
                       'handoff_complete': False, 'created': False, 'error': str(exc)})


def frozen():
    from hermes_cli.config import load_config
    section = load_config()
    for key in ('kanban', 'self_work'):
        if not isinstance(section, dict):
            raise policy.SelfWorkError('kanban.self_work.freeze_guards requires object configuration')
        section = section.get(key, {})
    if not isinstance(section, dict):
        raise policy.SelfWorkError('kanban.self_work.freeze_guards requires object configuration')
    value = section.get('freeze_guards', True)
    if type(value) is not bool:
        raise policy.SelfWorkError('kanban.self_work.freeze_guards must be boolean')
    return value


def choose(conn, value, *, now, tenant=None, report=True, excepted=False):
    """Share selection between preflight and admission; caller holds write lock.

    ``excepted``: admission already spent a single-use exception for this exact request in this transaction
    (``kanban_selfwork_exception.spend``). An open owner is still reused; only the no-owner refusal is replaced."""
    if value['kind'] != 'guard' or not frozen():
        return batch.choose(conn, value, now=now, tenant=tenant)
    row = conn.execute(
        "SELECT t.id FROM tasks t JOIN task_events e ON e.task_id=t.id "
        "WHERE t.tenant IS ? AND t.status NOT IN ('done','archived') AND e.kind=? "
        "AND json_extract(e.payload,'$.repository')=? AND json_extract(e.payload,'$.area')=? "
        "AND json_extract(e.payload,'$.kind')='guard' ORDER BY t.created_at,t.id LIMIT 1",
        (tenant, policy.EVENT, value['repository'], value['area']),
    ).fetchone()
    if report or row is None:
        log.info('guard creation frozen: repository=%s area=%s owner=%s',
                 value['repository'], value['area'], row['id'] if row else 'none')
    if row is None and excepted:
        return batch.Decision('create')
    if row is None:
        raise GuardFrozen('kanban.self_work.freeze_guards is on: no open guard owner in this area; '
                                   'add evidence to existing work instead of creating a guard')
    return batch.Decision('append', row['id'])
