"""Explicit Hermes maintenance declarations, never product-title heuristics."""
from __future__ import annotations

import json
import re

REPOSITORIES = frozenset({'hermes-core', 'hermes-state', 'hermes-mods'})
KINDS = frozenset({'sweep', 'test-gap', 'ci-flake', 'guard', 'post-merge'})
EVENT = 'self_work_declared'
_KEY = re.compile(r'[a-z0-9][a-z0-9_.:/-]{0,159}\Z')


class SelfWorkError(ValueError):
    """Invalid declaration or uncertain intake; never fall back to creation."""


def contract(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {'version', 'repository', 'area', 'kind'}:
        raise SelfWorkError('self_work requires version, repository, area and kind')
    if type(value['version']) is not int or value['version'] != 1:
        raise SelfWorkError('self_work requires version 1')
    if not isinstance(value['repository'], str) or value['repository'] not in REPOSITORIES:
        raise SelfWorkError('self_work requires a Hermes repository')
    if not isinstance(value['kind'], str) or value['kind'] not in KINDS:
        raise SelfWorkError('self_work requires a maintenance kind')
    area = value['area']
    if not isinstance(area, str) or not _KEY.fullmatch(area) or any(p in {'', '.', '..'} for p in area.split('/')):
        raise SelfWorkError('self_work area must be one canonical subsystem key')
    return dict(value)


def admitted_contract(conn, value):
    value = contract(value)
    if value is not None:
        from hermes_cli.kanban_selfwork_intake import assert_admitted_creation
        assert_admitted_creation(conn, value)
    return value


def record(conn, task_id, value, *, day):
    from hermes_cli.kanban_db import _append_event
    _append_event(conn, task_id, EVENT, {**value, 'day': day})


def declared(conn, task_id):
    row = conn.execute('SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1',
                       (task_id, EVENT)).fetchone()
    if row is None:
        return None
    value = json.loads(row[0])
    value.pop('day', None)
    return contract(value)
