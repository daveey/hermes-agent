"""Canonical diagnostic source discovery: no replica coordinates or authority guesses."""
import copy
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_actionability_observation as source


def inventory(tid='t_root', status='ready'):
    return {'version': 1, 'board': 'default', 'task_id': tid, 'head': 42,
            'events_complete': True, 'runs_complete': True, 'runs': [],
            'task': {'id': tid, 'uid': tid, 'created_on': 'home', 'deleted_at': None,
                     'status': status, 'assignee': 'default', 'current_run_uid': None,
                     'containment_episode': None},
            'events': [{'uid': tid + '_created', 'task_id': tid, 'kind': 'created',
                        'version': 1, 'ord': 1, 'created_at': 100, 'deleted_at': None}]}


class Hub:
    ident = SimpleNamespace(gateway='home')

    def __init__(self):
        self.data = {'t_root': inventory(), 't_parent': inventory('t_parent', 'done')}
        self.pages = [{'table': 'task_links', 'head': 42, 'rows': [], 'next': '', 'more': False}]

    def _call(self, method, path):
        assert method == 'GET' and path.endswith('/execution-evidence')
        return copy.deepcopy(self.data[path.split('/')[-2]])

    def snapshot_page(self, board, table, after='', limit=1000):
        assert (board, table) == ('default', 'task_links')
        return copy.deepcopy(self.pages[0 if not after else 1])


def test_source_orders_by_canonical_ord_and_ignores_comments():
    hub = Hub()
    hub.data['t_root']['events'] += [
        dict(hub.data['t_root']['events'][0], uid='newer', ord=2),
        dict(hub.data['t_root']['events'][0], uid='comment', ord=3, kind='commented', created_at=900)]
    value = source.observe(hub, 'default', 't_root')
    assert value['facts'] == {'status': 'ready', 'assignee': 'default', 'current_run_uid': None,
                             'root_episode': None, 'source_uids': [{'uid': 'newer', 'version': 1}],
                             'dependencies': []}
    assert value['first_observed'] == 100 and value['home_gateway'] == 'home'


@pytest.mark.parametrize('mutate', [
    lambda d: d.update(events_complete=False),
    lambda d: d.update(runs_complete=False),
    lambda d: d.update(board='other'),
    lambda d: d['task'].update(created_on='foreign'),
    lambda d: d['task'].update(status='done'),
    lambda d: d['task'].pop('current_run_uid'),
    lambda d: d['events'][0].update(version=True),
    lambda d: d['events'][0].pop('ord'),
    lambda d: d['events'].append(dict(d['events'][0])),
    lambda d: d['events'][0].update(deleted_at=1),
    lambda d: d['events'][0].update(kind='commented'),
    lambda d: d['events'].append(dict(d['events'][0], uid='stop', ord=2, kind='spend_budget_triaged')),
    lambda d: d['task'].update(containment_episode='missing'),
])
def test_incomplete_foreign_private_or_missing_source_refuses(mutate):
    hub = Hub()
    mutate(hub.data['t_root'])
    with pytest.raises(ValueError):
        source.observe(hub, 'default', 't_root')


def test_complete_paginated_links_include_terminal_parent():
    hub = Hub()
    hub.pages = [
        {'table': 'task_links', 'head': 42, 'rows': [
            {'uid': 'link', 'child_id': 't_root', 'parent_id': 't_parent', 'deleted_at': None}],
         'next': 'link', 'more': True},
        {'table': 'task_links', 'head': 42, 'rows': [], 'next': 'link', 'more': False}]
    assert source.observe(hub, 'default', 't_root')['facts']['dependencies'] == [
        {'id': 't_parent', 'status': 'done'}]


@pytest.mark.parametrize('mutate', [
    lambda h: h.pages[0].update(head=43),
    lambda h: h.pages[0].update(more='false'),
    lambda h: h.pages[0].update(more=True),
    lambda h: h.pages[0].update(table='tasks'),
    lambda h: h.pages[0].update(rows=[{'uid': 'l', 'child_id': 't_root', 'parent_id': 'missing'}]),
])
def test_graph_unknown_changed_or_nonprogressing_refuses(mutate):
    hub = Hub()
    mutate(hub)
    with pytest.raises(ValueError):
        source.observe(hub, 'default', 't_root')


def test_source_clock_and_containment_are_exact():
    hub = Hub()
    hub.data['t_root']['task']['containment_episode'] = 't_root_created'
    assert source.observe(hub, 'default', 't_root')['facts']['root_episode'] == 't_root_created'
    assert source.observe(hub, 'default', 't_root')['first_observed'] == 100
