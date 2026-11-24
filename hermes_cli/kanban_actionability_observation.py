"""Authenticated source discovery for explicit administrative recovery. [daveey]

Fail closed on private stop/renewal provenance: execution inventory deliberately
cannot attest that authority. Canonical ingress still repeats every premise CAS.
No replica IDs, local clock, source-subset selection or new release permission.
"""
from __future__ import annotations

from urllib.parse import quote

from hermes_cli.kanban_actionability import NONTERMINAL
from hermes_cli.kanban_actionability_selection import select

PRIVATE = {'spend_budget_triaged', 'decision_hold_restricted', 'scope_renewed'}


def _invalid():
    return ValueError('complete canonical actionability source unavailable or changed')


def _integer(value, minimum=0):
    return type(value) is int and minimum <= value <= 9007199254740991


def _inventory(hub, board, task_id):
    data = hub._call('GET', f'/v2/boards/{quote(board, safe="")}/tasks/'
                     f'{quote(task_id, safe="")}/execution-evidence')
    if (not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1
            or data.get('board') != board or data.get('task_id') != task_id
            or not _integer(data.get('head')) or data.get('events_complete') is not True
            or data.get('runs_complete') is not True or not isinstance(data.get('runs'), list)
            or not isinstance(data.get('events'), list)):
        raise _invalid()
    task = data.get('task')
    if (not isinstance(task, dict) or task.get('id') != task_id or task.get('uid') != task_id
            or task.get('deleted_at') is not None
            or not {'status', 'assignee', 'current_run_uid', 'containment_episode', 'created_on'} <= task.keys()):
        raise _invalid()
    uids, orders = set(), set()
    for event in data['events']:
        if (not isinstance(event, dict) or not isinstance(event.get('uid'), str) or not event['uid']
                or event['uid'] in uids or event.get('task_id') != task_id
                or not _integer(event.get('ord'), 1) or event['ord'] in orders
                or not _integer(event.get('version'), 1) or not _integer(event.get('created_at'))
                or event.get('deleted_at') is not None or not isinstance(event.get('kind'), str)
                or event['kind'] in PRIVATE):
            raise _invalid()
        uids.add(event['uid'])
        orders.add(event['ord'])
    return data


def _links(hub, board, head):
    """Complete stable-head snapshot, bounded independently of server pagination."""
    after, links = '', []
    for _ in range(16):
        page = hub.snapshot_page(board, 'task_links', after=after, limit=1000)
        if (not isinstance(page, dict) or page.get('table') != 'task_links' or page.get('head') != head
                or not isinstance(page.get('rows'), list) or type(page.get('more')) is not bool):
            raise _invalid()
        cursor = after
        for link in page['rows']:
            if (not isinstance(link, dict) or not isinstance(link.get('uid'), str) or link['uid'] <= cursor
                    or not isinstance(link.get('child_id'), str) or not isinstance(link.get('parent_id'), str)
                    or link.get('deleted_at') is not None):
                raise _invalid()
            cursor = link['uid']
            links.append(link)
        if page.get('next') != cursor or page['more'] and cursor == after:
            raise _invalid()
        if not page['more']:
            return links
        after = cursor
    raise _invalid()


def observe(hub, board, task_id, *, include_source=False):
    """Bounded canonical observation; unavailable authority remains unavailable.

    Every inventory/page shares one head. Busy boards may require a later explicit
    preview; this does not restart, poll or weaken the complete-snapshot contract.
    """
    data = _inventory(hub, board, task_id)
    task, head = data['task'], data['head']
    if task['created_on'] != hub.ident.gateway or task['status'] not in NONTERMINAL:
        raise _invalid()
    episode = task['containment_episode']
    event = select(task['status'], data['events'], episode=episode,
                   order_key=lambda e: (e['created_at'], e['ord'], e['uid']))
    if event is None:
        raise _invalid()  # no invented legacy first-observed clock
    links = _links(hub, board, head)
    parent_ids = sorted(l['parent_id'] for l in links if l['child_id'] == task_id)
    if len(parent_ids) > 128 or len(set(parent_ids)) != len(parent_ids):
        raise _invalid()
    dependencies, owner_task_refs, raw_parent_states = [], [], []
    for pid in parent_ids:
        parent = _inventory(hub, board, pid)
        if parent['head'] != head:
            raise _invalid()
        p = parent['task']
        raw_parent_states.append({'id': pid, 'status': p['status'], 'containment_episode': p['containment_episode']})
        if pid != task_id and p['status'] in NONTERMINAL:
            owner_task_refs.append(pid)
        status = 'triage' if p['status'] in ('done', 'archived') and p['containment_episode'] else p['status']
        dependencies.append({'id': pid, 'status': status})
    result = {'home_gateway': task['created_on'], 'first_observed': event['created_at'],
            'owner_task_refs': owner_task_refs,
            'facts': {'status': task['status'], 'assignee': task['assignee'],
                      'current_run_uid': task['current_run_uid'], 'root_episode': episode,
                      'source_uids': [{'uid': event['uid'], 'version': event['version']}],
                      'dependencies': dependencies}}
    if include_source:
        result.update(source_event=event, source_task=task, raw_parent_states=raw_parent_states)
    return result
